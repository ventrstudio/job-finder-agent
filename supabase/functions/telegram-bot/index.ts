// Telegram bot for the Job Scout — the interactive AI layer.
//
// Locked to a single Telegram user. Abilities, routed by one LLM call:
//   1. Q&A about listings   (read jobs, answer)
//   2. Cover letters         (Otis's proven proof-forward style, Sonnet + caching)
//   3. Edit profile          (update agent_profile fields)
//   4. Feedback              (append a rule to agent_profile.anti_patterns)
//   5. Resume                (READ-ONLY: view active / list versions — resume_versions table)
//   /cost  — LLM spend     /rubric — view+edit the scout's scoring brain
//
// Resume is version-controlled in resume_versions (one active row), maintained
// via Claude Code (PDF + HTML + plaintext records over time) — NOT via Telegram.
// The bot is READ-ONLY for the resume: it reads the active version's plaintext
// for scoring (score_jobs.py) and cover letters, but never changes it.
//
// Conversation memory: last ~8 turns within 45 min (windowed + expiring), so
// context never grows unbounded. Voice/cover-letter rules live in prompt_assets,
// injected only for letters. Hybrid model: cheap auto for routing/chat, Sonnet
// for letters (with prompt caching on the static rules).
//
// Security: webhook secret header + from.id must equal allowed_user_id.
// Secrets are function env vars. SUPABASE_URL / SERVICE_ROLE_KEY auto-injected.

import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const SB_URL = Deno.env.get("SUPABASE_URL")!;
const SB_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";
const MODEL = "google/gemini-2.5-flash-lite";          // routing / Q&A — cheap + deterministic
const MODEL_LETTER = "anthropic/claude-sonnet-4.6";    // voice-critical letters
const SCORE_MATCH = 50;                                // 0-100; 50 == 5/10
const HISTORY_LIMIT = 8;                               // turns kept in context
const HISTORY_MINUTES = 45;                            // older turns expire

// ---------- Supabase REST (service role) ----------
async function sb(path: string, init: RequestInit = {}): Promise<any> {
  const r = await fetch(`${SB_URL}/rest/v1/${path}`, {
    ...init,
    headers: {
      apikey: SB_KEY,
      Authorization: `Bearer ${SB_KEY}`,
      "Content-Type": "application/json",
      ...(init.headers || {}),
    },
  });
  const text = await r.text();
  if (!r.ok) throw new Error(`Supabase ${r.status}: ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}

function getConfig(): Record<string, string> {
  return {
    telegram_bot_token: Deno.env.get("TELEGRAM_BOT_TOKEN") || "",
    telegram_webhook_secret: Deno.env.get("TELEGRAM_WEBHOOK_SECRET") || "",
    allowed_user_id: Deno.env.get("ALLOWED_USER_ID") || "",
    openrouter_api_key: Deno.env.get("OPENROUTER_API_KEY") || "",
  };
}

// Slim profile for routing/Q&A — excludes resume_text (big; only letters need it).
async function getSlimProfile(): Promise<any> {
  const cols =
    "id,target_roles,skills,job_types,location_preference,zip_code,salary_notes,anti_patterns,custom_prompt";
  const rows = await sb(`agent_profile?select=${cols}&limit=1`);
  return rows?.[0] || {};
}

async function getResumeText(): Promise<string> {
  const rows = await sb("agent_profile?select=resume_text&limit=1");
  return rows?.[0]?.resume_text || "";
}

// ---------- resume versioning (READ-ONLY from the bot) ----------
// resume_versions is maintained via Claude Code (PDF/HTML/plaintext records);
// the bot only reads it. Resume edits never happen through Telegram.
async function listResumeVersions(): Promise<any[]> {
  return await sb("resume_versions?select=version_no,note,is_active,created_at&order=version_no.desc&limit=20");
}

async function getAsset(key: string): Promise<string> {
  const rows = await sb(`prompt_assets?select=content&key=eq.${key}&limit=1`);
  return rows?.[0]?.content || "";
}

function agePosted(d: string | null): string | null {
  if (!d) return null;
  const posted = new Date(`${d}T00:00:00Z`);
  if (isNaN(posted.getTime())) return null;
  const now = new Date();
  const days = Math.floor(
    (Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) -
      Date.UTC(posted.getUTCFullYear(), posted.getUTCMonth(), posted.getUTCDate())) / 864e5,
  );
  if (days <= 0) return "posted today";
  if (days === 1) return "posted yesterday";
  return `posted ${days} days ago`;
}

async function getRecentMatches(limit = 12): Promise<any[]> {
  const cols =
    "job_title,company,location,is_remote,job_type,salary_min,salary_max,salary_interval,resume_score,score_tldr,job_url_direct,date_posted";
  const rows: any[] = await sb(
    `jobs?select=${cols}&is_active=eq.true&resume_score=gte.${SCORE_MATCH}` +
      `&order=resume_score.desc,scraped_at.desc&limit=${limit}`,
  );
  for (const j of rows) j.posted = agePosted(j.date_posted) || "posting date unknown";
  return rows;
}

async function findJob(hint: string): Promise<any | null> {
  const cols = "job_title,company,location,description,job_url_direct,date_posted,resume_score";
  // Try the whole hint first, then distinctive words (drop filler), matching
  // either title or company. Picks the highest-scored hit.
  const stop = new Set(["role", "job", "position", "posting", "listing", "the", "for", "at", "a", "an", "cover", "letter", "one"]);
  const words = hint.toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length >= 3 && !stop.has(w));
  const tries = [hint.trim(), ...words];
  for (const t of tries) {
    if (!t) continue;
    const enc = encodeURIComponent(`*${t}*`);
    // match title, company, OR description — tools like "zingtree" often live
    // only in the body, not the title/company.
    const rows = await sb(
      `jobs?select=${cols}&is_active=eq.true&or=(job_title.ilike.${enc},company.ilike.${enc},description.ilike.${enc})` +
        `&order=resume_score.desc&limit=1`,
    );
    if (rows?.length) return rows[0];
  }
  return null;
}

// ---------- conversation memory ----------
async function loadHistory(chatId: string): Promise<Array<{ role: string; content: string }>> {
  const since = new Date(Date.now() - HISTORY_MINUTES * 60000).toISOString();
  const rows: any[] = await sb(
    `bot_conversations?select=role,content&chat_id=eq.${encodeURIComponent(chatId)}` +
      `&created_at=gte.${since}&order=created_at.desc&limit=${HISTORY_LIMIT}`,
  );
  const hist = rows.reverse().map((r) => ({ role: r.role, content: r.content }));
  while (hist.length && hist[0].role === "assistant") hist.shift(); // must start with user
  return hist;
}

async function saveTurn(chatId: string, role: string, content: string): Promise<void> {
  try {
    await sb("bot_conversations", {
      method: "POST",
      headers: { Prefer: "return=minimal" },
      body: JSON.stringify({ chat_id: chatId, role, content: String(content).slice(0, 8000) }),
    });
  } catch (e) {
    console.error("saveTurn failed", e);
  }
}

// ---------- OpenRouter ----------
async function logCost(source: string, model: string, usage: any): Promise<void> {
  try {
    await sb("llm_costs", {
      method: "POST",
      headers: { Prefer: "return=minimal" },
      body: JSON.stringify({
        source,
        model,
        prompt_tokens: usage?.prompt_tokens ?? 0,
        completion_tokens: usage?.completion_tokens ?? 0,
        total_tokens: usage?.total_tokens ?? 0,
        cost_usd: usage?.cost ?? 0,
      }),
    });
  } catch (e) {
    console.error("cost log failed", e);
  }
}

interface CallOpts {
  model?: string;
  system: string;
  user: string;
  history?: Array<{ role: string; content: string }>;
  json?: boolean;
  maxTokens?: number;
  cacheSystem?: boolean; // mark static system for Anthropic prompt caching
  source?: string;
}

async function callLLM(key: string, o: CallOpts): Promise<string> {
  const sysContent = o.cacheSystem
    ? [{ type: "text", text: o.system, cache_control: { type: "ephemeral" } }]
    : o.system;
  const messages: any[] = [{ role: "system", content: sysContent }];
  for (const h of o.history || []) messages.push({ role: h.role, content: h.content });
  messages.push({ role: "user", content: o.user });

  const body: any = {
    model: o.model || MODEL,
    messages,
    temperature: 0.3,
    max_tokens: o.maxTokens ?? 900,
    usage: { include: true },
  };
  if (o.json) body.response_format = { type: "json_object" };

  // flash-lite intermittently returns an empty completion under JSON mode —
  // retry once on empty before giving up.
  let content = "";
  for (let attempt = 0; attempt < 2; attempt++) {
    const r = await fetch(OPENROUTER_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${key}`,
        "Content-Type": "application/json",
        "HTTP-Referer": "https://ventr.studio",
        "X-Title": "VENTR Job Scout Bot",
      },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(`OpenRouter ${r.status}: ${JSON.stringify(data).slice(0, 300)}`);
    await logCost(o.source || "bot_router", data?.model || o.model || MODEL, data?.usage);
    content = (data?.choices?.[0]?.message?.content || "").trim();
    if (content) break;
    console.error(`empty LLM content (attempt ${attempt + 1}) source=${o.source}`);
  }
  return content;
}

async function costSummary(): Promise<string> {
  const rows: any[] = await sb(
    "llm_costs?select=created_at,source,cost_usd&order=created_at.desc&limit=2000",
  );
  const now = Date.now();
  const day = 864e5;
  let today = 0, week = 0, month = 0, all = 0, cToday = 0, cAll = 0;
  const bySource: Record<string, number> = {};
  for (const r of rows) {
    const c = Number(r.cost_usd) || 0;
    const age = now - new Date(r.created_at).getTime();
    all += c; cAll++;
    bySource[r.source] = (bySource[r.source] || 0) + c;
    if (age <= day) { today += c; cToday++; }
    if (age <= 7 * day) week += c;
    if (age <= 30 * day) month += c;
  }
  const m = (n: number) => "$" + n.toFixed(n < 0.01 ? 5 : 4);
  const srcLines = Object.entries(bySource).sort((a, b) => b[1] - a[1])
    .map(([s, c]) => `  • ${s}: ${m(c)}`).join("\n");
  return [
    "💸 <b>LLM cost</b>",
    `Today: ${m(today)} (${cToday} calls)`,
    `Last 7d: ${m(week)}`,
    `Last 30d: ${m(month)}`,
    `All time: ${m(all)} (${cAll} calls)`,
    "", "By source (all time):", srcLines || "  (none yet)",
  ].join("\n");
}

async function rubricSummary(): Promise<string> {
  const p = await getSlimProfile();
  const arr = (x: any) => (Array.isArray(x) && x.length ? x.join(", ") : "(none)");
  const txt = (x: any) => (x ? String(x) : "(none)");
  return [
    "🧠 <b>Scout's scoring brain</b>",
    "",
    `<b>Target roles:</b> ${esc(arr(p.target_roles))}`,
    `<b>Skills:</b> ${esc(arr(p.skills))}`,
    `<b>Job types:</b> ${esc(arr(p.job_types))}`,
    `<b>Location pref:</b> ${esc(txt(p.location_preference))}`,
    `<b>Salary notes:</b> ${esc(txt(p.salary_notes))}`,
    `<b>Avoid (anti-patterns):</b> ${esc(arr(p.anti_patterns))}`,
    `<b>Custom scoring rules:</b> ${esc(txt(p.custom_prompt))}`,
    "",
    "Edit any of it by just telling me — e.g. “add Solutions Engineer to my target roles”, “stop ranking DevOps high”, “weight automation roles up”. Takes effect on the next scoring run.",
  ].join("\n");
}

function parseJson(text: string): any {
  let t = text.trim();
  if (t.startsWith("```")) t = t.replace(/^```[a-z]*\n?/i, "").replace(/```$/, "").trim();
  try {
    return JSON.parse(t);
  } catch {
    const m = t.match(/\{[\s\S]*\}/);
    if (m) return JSON.parse(m[0]);
    throw new Error("no JSON");
  }
}

// ---------- Telegram ----------
async function tg(token: string, method: string, payload: any): Promise<void> {
  await fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

async function reply(token: string, chatId: number, text: string): Promise<void> {
  const LIMIT = 3800;
  for (let i = 0; i < text.length; i += LIMIT) {
    await tg(token, "sendMessage", {
      chat_id: chatId,
      text: text.slice(i, i + LIMIT),
      parse_mode: "HTML",
      disable_web_page_preview: true,
    });
  }
}

function esc(s: string): string {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Convert LLM Markdown into the small HTML subset Telegram renders.
// Handles fenced code, inline code, **bold**, *italic*, ~~strike~~, # headers,
// - bullets, and [text](url). Escapes all other </>/& so prose can't break parsing.
function mdToHtml(raw: string): string {
  let s = String(raw ?? "");
  // 1) pull fenced code blocks out so their contents aren't touched
  const blocks: string[] = [];
  s = s.replace(/```(?:\w+)?\n?([\s\S]*?)```/g, (_m, code) => {
    blocks.push(String(code).replace(/\n$/, ""));
    return `@B${blocks.length - 1}@`;
  });
  // 2) escape HTML specials (markdown tokens are not specials, so they survive)
  s = s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  // 3) line-level: headers -> bold, bullet markers -> •
  s = s.split("\n").map((ln) => {
    const h = ln.match(/^\s*#{1,6}\s+(.*)$/);
    if (h) return `<b>${h[1].trim()}</b>`;
    const b = ln.match(/^(\s*)[-*+]\s+(.*)$/);
    if (b) return `${b[1]}• ${b[2]}`;
    return ln;
  }).join("\n");
  // 4) inline code, then bold, then single-* italic (underscores left alone to
  //    avoid eating snake_case), then strike, then links
  s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<i>$2</i>");
  s = s.replace(/~~([^~\n]+)~~/g, "<s>$1</s>");
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2">$1</a>');
  // 5) restore code blocks (escaped)
  s = s.replace(/@B(\d+)@/g, (_m, i) => `<pre>${esc(blocks[Number(i)])}</pre>`);
  return s;
}

const HELP = [
  "🔍 <b>Job Scout</b> — I’m your job assistant. Talk to me normally, I remember the last few messages:",
  "",
  "• <b>Ask about listings</b> — “show today’s top 5 remote roles”, “tell me about the ShipBob one”",
  "• <b>Cover letter</b> — “write a cover letter for the ShipBob role”, then “make it tighter” / “lead with the bilingual angle”",
  "• <b>Edit your profile</b> — “add ‘no on-call’ to my dealbreakers”",
  "• <b>Feedback</b> — “stop showing me teaching jobs”",
  "• <b>Resume</b> (read-only) — “show my resume”, “list resume versions”. Resume edits happen in Claude Code, not here.",
  "• <b>/rubric</b> — view + edit what the scout looks for",
  "• <b>/cost</b> — LLM spend (today / 7d / 30d / all)",
  "",
  "Your full digest still comes by email each morning.",
].join("\n");

const ROUTER_SYSTEM = `You are the Job Scout assistant for Otis, a job seeker, chatting with him in Telegram. You can answer questions about his job matches, kick off cover letters, edit his search profile, record feedback, and let him READ his version-controlled resume (read-only — resume edits happen in Claude Code, never here). You have his PROFILE and RECENT MATCHING JOBS (in this system message) plus the recent conversation (prior turns). Use the conversation for context — if he says "make it tighter" or "that one", resolve it from the history.

Reply ONLY with a JSON object:
{
  "reply": "<message to Otis in plain text + Markdown only (**bold**, *italic*, - bullets). NO HTML tags.>",
  "action": "none" | "lookup_job" | "cover_letter" | "update_profile" | "feedback" | "resume_view",
  "args": { }
}

Rules:
- Listing his matches / anything answerable from RECENT MATCHING JOBS: action="none", answer in "reply". Scores are 0-100; show score/10. ALWAYS include each job's "posted" field (e.g. "posted 3 days ago"). One job per line: "**Title** — Company · score/10 · posted X days ago".
- General knowledge (what is X platform/tool/company, industry questions, "can I use Claude Code with Y", career advice): action="none", answer DIRECTLY from your own knowledge. You are NOT limited to the job list for general questions.
- A SPECIFIC job or company that may NOT be in RECENT MATCHING JOBS (e.g. "the zingtree job", "tell me about the Acme role"): action="lookup_job", args={"job_hint":"<company or ONE distinctive word, e.g. 'zingtree'>", "question":"<exactly what he is asking>"}. reply = one short line like "Looking that up.".
- Cover letter (new OR a revision of one already in the conversation): action="cover_letter", args={"job_hint":"<JUST the company name or one distinctive title word — e.g. 'ShipBob', NOT 'the ShipBob role'; reuse the same job if revising>", "instruction":"<what he wants, e.g. 'tighter', 'lead with bilingual', or 'standard'>"}. One short line in reply like "On it.".
- Edit profile / scoring brain — ONLY when he names a specific field to SET and gives the new value (e.g. "add Solutions Engineer to my target roles", "set my salary floor to 6k", "change my location to remote-only", "add Python to my skills"): action="update_profile", args={"field":"<target_roles|skills|job_types|location_preference|zip_code|salary_notes|custom_prompt>", "value": <COMPLETE new value; for arrays return the full updated array>}. Confirm in reply.
- Record a DISLIKE / exclusion / scoring preference that is NOT a specific field edit — "stop showing me X jobs", "don't show me Y", "I don't want Z", "avoid roles that...", "stop ranking W so high", "no more commission-only": action="feedback", args={"note":"<short rule>"}. Confirm in reply. TIEBREAKER: for ANY "stop / don't / avoid / no more / quit showing ..." phrasing, choose feedback, NOT update_profile.
- READ his resume (display only — the bot is READ-ONLY for the resume): action="resume_view", args={"which":"active"|"list"|"<version number>"}. "show / see / read / view / display / pull up my resume" -> "active"; "list resume versions" / "resume history" -> "list"; "show resume v2" -> "2". reply = one short line (the resume text is attached by the system).
- The bot CANNOT change the resume. If he asks to edit / update / add to / rewrite / replace his resume (or his PDF), action="none" and tell him resume edits are done in Claude Code, not here — offer to show the current one instead.
- Never invent jobs not in the data.
- REVIEW-ONLY: if Otis asks you to review, propose, suggest, think about, or consider changes WITHOUT explicitly telling you to apply/save/make/do them, use action="none" and give your analysis only. Do NOT update_profile or feedback until he clearly says to apply it (e.g. "do it", "apply that", "go ahead", "save it"). resume_view is read-only and always allowed.
- NEVER claim you updated, saved, changed, or adjusted his profile/settings/rubric unless action is update_profile or feedback. With action="none" you must not say you changed anything — only answer or give analysis.`;

Deno.serve(async (req: Request): Promise<Response> => {
  const cfg = getConfig();
  const token = cfg.telegram_bot_token;
  const secret = cfg.telegram_webhook_secret;
  const allowed = String(cfg.allowed_user_id || "");
  const orKey = cfg.openrouter_api_key;

  if (secret && req.headers.get("x-telegram-bot-api-secret-token") !== secret) {
    return new Response("forbidden", { status: 403 });
  }

  let update: any;
  try {
    update = await req.json();
  } catch {
    return new Response("ok");
  }

  const msg = update?.message || update?.edited_message;
  const fromId = String(msg?.from?.id ?? "");
  const chatId = msg?.chat?.id;
  const text = (msg?.text || "").trim();

  if (!msg || fromId !== allowed) {
    console.warn(`ignored update from ${fromId} (allowed ${allowed})`);
    return new Response("ok");
  }
  if (!text) return new Response("ok");

  if (text === "/start" || text === "/help") {
    await reply(token, chatId, HELP);
    return new Response("ok");
  }
  if (text === "/rubric") {
    try { await reply(token, chatId, await rubricSummary()); }
    catch (e) { console.error("rubric failed", e); await reply(token, chatId, "⚠️ Couldn't load the rubric."); }
    return new Response("ok");
  }
  if (text === "/cost" || /\bhow much.*(spent|cost)|llm cost|token cost|my spend\b/i.test(text)) {
    try { await reply(token, chatId, await costSummary()); }
    catch (e) { console.error("cost failed", e); await reply(token, chatId, "⚠️ Couldn't pull cost data."); }
    return new Response("ok");
  }

  const chatKey = String(chatId);
  try {
    const [profile, jobs, history] = await Promise.all([
      getSlimProfile(),
      getRecentMatches(),
      loadHistory(chatKey),
    ]);

    const routerSystem =
      ROUTER_SYSTEM +
      `\n\n=== PROFILE ===\n${JSON.stringify(profile)}` +
      `\n\n=== RECENT MATCHING JOBS ===\n${JSON.stringify(jobs)}`;

    const rawRouted = await callLLM(orKey, {
      model: MODEL, system: routerSystem, user: text, history,
      json: true, maxTokens: 1200, source: "bot_router",
    });
    // If the router didn't return clean JSON (truncation, prose), don't error —
    // fall back to treating the raw text as a plain answer.
    let routed: any;
    try {
      routed = parseJson(rawRouted);
    } catch {
      console.error("router parse failed; raw:", rawRouted.slice(0, 400));
      // Salvage the "reply" field if present, else strip any trailing JSON fragment.
      const mr = rawRouted.match(/"reply"\s*:\s*"((?:[^"\\]|\\.)*)"/);
      let salvaged = "";
      if (mr) { try { salvaged = JSON.parse('"' + mr[1] + '"'); } catch { salvaged = mr[1]; } }
      if (!salvaged) salvaged = rawRouted.replace(/\{[\s\S]*$/, "").trim();
      routed = { action: "none", reply: salvaged || "Sorry, I blanked on that — try again." };
    }
    const action = routed.action || "none";
    const args = routed.args || {};
    let out = mdToHtml(routed.reply || "");

    if (action === "lookup_job") {
      // Search ALL active jobs (not just the top matches) for a specific listing,
      // then answer his question grounded in it (or from general knowledge if missing).
      const hint = String(args.job_hint || "").trim();
      const question = String(args.question || text);
      const job = hint ? await findJob(hint) : null;
      const jobBlock = job
        ? `JOB: ${job.job_title} at ${job.company}\nLocation: ${job.location || "?"}\nScore: ${job.resume_score ?? "?"}/100\n\nDESCRIPTION:\n${(job.description || "").slice(0, 5000)}`
        : `(No job matching "${hint}" was found in the scraped listings — it may have aged out or scored low. Answer from general knowledge and say it wasn't in his current listings.)`;
      const ans = await callLLM(orKey, {
        model: MODEL,
        system: "You are Otis's job-scout assistant. Answer his question about the job/platform concisely in plain text + light Markdown (**bold**, - bullets). Otis works almost entirely through Claude Code (AI-assisted automation and dev) and cares whether a role lets him leverage it — call that out when relevant. Ground your answer in the job data if provided; otherwise use general knowledge.",
        user: `${jobBlock}\n\nOtis asks: ${question}`,
        maxTokens: 700,
        source: "bot_lookup",
      });
      const head = job
        ? `📄 <b>${esc(job.job_title)}</b> — ${esc(job.company)}` +
          (job.resume_score != null ? ` · ${Math.round(job.resume_score / 10)}/10` : "") + "\n\n"
        : "";
      out = head + mdToHtml(ans);
    } else if (action === "cover_letter") {
      const [asset, resume] = await Promise.all([getAsset("cover_letter_system"), getResumeText()]);
      const hint = String(args.job_hint || "").trim();
      const job = hint ? await findJob(hint) : null;
      const jobBlock = job
        ? `JOB: ${job.job_title} at ${job.company}\n${job.location || ""}\n\nDESCRIPTION:\n${(job.description || "").slice(0, 6000)}`
        : "(Revise the cover letter already in this conversation — the job is in the history above.)";
      const instruction = String(args.instruction || "standard");
      const letter = await callLLM(orKey, {
        model: MODEL_LETTER,
        system: `${asset}\n\n=== OTIS'S RESUME (for detail; do not contradict the facts above) ===\n${resume.slice(0, 3500)}`,
        user: `${jobBlock}\n\nINSTRUCTION: ${instruction}\nUser's exact words: ${text}`,
        history: history.slice(-6),
        cacheSystem: true,
        maxTokens: 900,
        source: "bot_cover_letter",
      });
      const age = job ? agePosted(job.date_posted) : null;
      out = (job ? `📄 <b>${esc(job.job_title)}</b> — ${esc(job.company)}\n` + (age ? `🗓 ${esc(age)}\n` : "") +
        (job.job_url_direct ? `${esc(job.job_url_direct)}\n` : "") + "\n" : "") + mdToHtml(letter);
    } else if (action === "update_profile") {
      const field = String(args.field || "");
      const allowedFields = [
        "target_roles", "skills", "job_types", "location_preference",
        "zip_code", "salary_notes", "custom_prompt",
      ];
      if (!allowedFields.includes(field)) {
        out = `I can't edit "${esc(field)}". Editable: ${allowedFields.join(", ")}.`;
      } else {
        await sb(`agent_profile?id=eq.${profile.id}`, {
          method: "PATCH",
          headers: { Prefer: "return=minimal" },
          body: JSON.stringify({ [field]: args.value, updated_at: new Date().toISOString() }),
        });
        out = out || `✅ Updated <b>${esc(field)}</b>.`;
      }
    } else if (action === "feedback") {
      const note = String(args.note || text);
      const cur: string[] = Array.isArray(profile.anti_patterns) ? profile.anti_patterns : [];
      await sb(`agent_profile?id=eq.${profile.id}`, {
        method: "PATCH",
        headers: { Prefer: "return=minimal" },
        body: JSON.stringify({ anti_patterns: [...cur, note], updated_at: new Date().toISOString() }),
      });
      out = out || `✅ Noted: future scoring will weigh this. "${esc(note)}"`;
    } else if (action === "resume_view") {
      const which = String(args.which || "active").toLowerCase().replace(/^v/, "");
      if (which === "list") {
        const vs = await listResumeVersions();
        const lines = vs.map((v) =>
          `${v.is_active ? "✅" : "  "} <b>v${v.version_no}</b> · ${String(v.created_at).slice(0, 10)}` +
          (v.note ? ` · ${esc(v.note)}` : ""));
        out = "🗂 <b>Resume versions</b>\n" + (lines.join("\n") || "(none yet)") +
          "\n\nSay “show resume v2” to read any version. New versions + edits are done in Claude Code, not here.";
      } else {
        const rows = /^\d+$/.test(which)
          ? await sb(`resume_versions?select=content,version_no,note,is_active,created_at&version_no=eq.${which}&limit=1`)
          : await sb("resume_versions?select=content,version_no,note,is_active,created_at&is_active=eq.true&limit=1");
        const row = rows?.[0];
        out = row
          ? `📄 <b>Resume v${row.version_no}</b>${row.is_active ? " (active)" : ""}` +
            (row.note ? ` · ${esc(row.note)}` : "") + "\n\n" + esc(String(row.content))
          : "No such resume version. Say “list resume versions” to see what exists.";
      }
    }

    out = out || "Done.";
    await reply(token, chatId, out);
    await saveTurn(chatKey, "user", text);
    await saveTurn(chatKey, "assistant", out);
  } catch (e) {
    console.error("handler error", e);
    const detail = esc(String((e as any)?.message || e)).slice(0, 350);
    await reply(token, chatId, `⚠️ Hit an error handling that. Try again in a moment.\n\n<code>${detail}</code>`);
  }

  return new Response("ok");
});
