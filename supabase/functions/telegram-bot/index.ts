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
    apify_token: Deno.env.get("APIFY_TOKEN") || "",
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
    "job_title,company,location,is_remote,job_type,salary_min,salary_max,salary_interval,resume_score,score_tldr,job_url_direct,date_posted,legitimacy_verdict,legitimacy_summary,scam_risk_score";
  const rows: any[] = await sb(
    `jobs?select=${cols}&is_active=eq.true&resume_score=gte.${SCORE_MATCH}` +
      `&order=resume_score.desc,scraped_at.desc&limit=${limit}`,
  );
  for (const j of rows) j.posted = agePosted(j.date_posted) || "posting date unknown";
  return rows;
}

// Generic words that match too many unrelated listings. A lone generic word was
// pulling a RANDOM high-scored job (e.g. "production" -> a 90-scored unrelated
// role with its own URL). Never use one as a standalone lookup key.
const FINDJOB_STOP = new Set([
  "role", "job", "position", "posting", "listing", "the", "for", "at", "a", "an", "cover", "letter", "one",
  "software", "engineer", "developer", "developers", "ai", "automation", "specialist", "senior", "staff",
  "lead", "principal", "remote", "hybrid", "onsite", "production", "productions", "company", "llc", "inc",
  "technologies", "solutions", "services", "team", "manager", "fulltime", "parttime", "contract", "about",
]);

async function findJob(hint: string): Promise<any | null> {
  const cols = "job_title,company,location,description,job_url_direct,date_posted,resume_score,legitimacy_verdict,legitimacy_summary,scam_risk_score";
  const full = hint.trim();
  // Distinctive single words only: length >= 4 and not in the generic stoplist.
  const words = full.toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length >= 4 && !FINDJOB_STOP.has(w));
  // Match TITLE/COMPANY ONLY — never the description body. A body match let a
  // stray word (e.g. "websites" from "Head of Websites") hit a random unrelated
  // high-scored listing and attach ITS url (the wrong-job bug: an Indeed link
  // came back as an unrelated cyber-security role). Title/company is the only
  // trustworthy anchor; a hint that lives only in a body is not worth a guess.
  const queries: string[] = [];
  if (full) queries.push(full);
  for (const w of words) queries.push(w);
  for (const q of queries) {
    const enc = encodeURIComponent(`*${q}*`);
    const rows = await sb(
      `jobs?select=${cols}&is_active=eq.true&or=(job_title.ilike.${enc},company.ilike.${enc})&order=resume_score.desc&limit=1`,
    );
    if (rows?.length) return rows[0];
  }
  return null;
}

// Detect a pasted job description (Otis dumps the whole listing into chat). When
// present, the pasted text IS the job — skip findJob entirely (that was the
// source of the wrong-job/random-URL replies).
function pastedJob(text: string): string | null {
  const t = (text || "").trim();
  if (t.length >= 600) return t;
  if (
    t.length >= 280 &&
    /responsibilit|requirement|qualification|about (the|us|our)|position summary|what you.?ll do|core competenc|per hour|\$\s?\d|pay:|benefits|compensation|core values/i
      .test(t)
  ) {
    return t;
  }
  return null;
}

// A job-board LINK (Indeed/LinkedIn/etc.). The bot has NO web fetch — it only
// knows jobs already scraped into the DB. A link to a job we don't have must
// NEVER silently resolve to a random DB row. When a message carries such a link
// and Otis did NOT also paste the full listing text, we refuse to guess and ask
// him to paste the description instead.
const JOB_URL_RE =
  /https?:\/\/[^\s]*(indeed\.|linkedin\.com\/jobs|glassdoor\.|ziprecruiter\.|greenhouse\.io|lever\.co|ashbyhq\.|workable\.|wellfound\.|builtin\.|\/viewjob|[?&]jk=)/i;
function hasJobUrl(text: string): boolean {
  return JOB_URL_RE.test(text || "");
}

// Pull an Indeed job key (jk) out of a pasted link. Indeed keys are hex, ~16
// chars. Handles both `?jk=...`/`&jk=...` and the `/viewjob/<jk>` path form.
function extractJk(text: string): string | null {
  const t = text || "";
  const m = t.match(/[?&]jk=([a-f0-9]{8,24})/i) || t.match(/\/viewjob\/([a-f0-9]{8,24})/i);
  return m ? m[1].toLowerCase() : null;
}

// Fetch ONE Indeed job by key via the memo23 cheerio actor (single-URL "direct
// job" scrape; ~$0.008/fetch, returns in seconds). The bot has no other way to
// open a link — this is the fetch path. Maps memo23's raw Indeed shape into the
// same job object the cover-letter / lookup branches already expect. Returns
// null on any failure so callers can fall back to "paste the text".
async function fetchIndeedJob(jk: string, apifyToken: string): Promise<any | null> {
  if (!apifyToken) { console.error("APIFY_TOKEN not set — cannot fetch Indeed job"); return null; }
  const jobUrl = `https://www.indeed.com/viewjob?jk=${jk}`;
  const endpoint =
    `https://api.apify.com/v2/acts/memo23~apify-indeed-cheerio-ppr/run-sync-get-dataset-items?token=${apifyToken}`;
  let data: any;
  try {
    const r = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ startUrls: [{ url: jobUrl }], maxJobs: 1 }),
    });
    if (!r.ok) { console.error(`apify ${r.status}: ${(await r.text()).slice(0, 200)}`); return null; }
    data = await r.json();
  } catch (e) {
    console.error("apify fetch failed", e);
    return null;
  }
  const it = Array.isArray(data) ? data[0] : null;
  if (!it || !it.title) return null;
  if (it.expired === true) return null; // dead listing — treat as not found
  const company = it.sourceEmployerName || it?.source?.name || it?.employer?.name || "the company";
  const description = it.jobDescription || it.sanitizedJobDescription || "";
  const loc = it.location || {};
  const location =
    [loc.city, loc.admin1Name, loc.countryCode].filter(Boolean).join(", ") ||
    (it.isRemote ? "Remote" : "");
  const jt = Array.isArray(it.jobTypes) && it.jobTypes[0]
    ? String(it.jobTypes[0].label || "").toLowerCase()
    : null;
  let date_posted: string | null = null;
  if (typeof it.datePublished === "number") {
    const d = new Date(it.datePublished);
    if (!isNaN(d.getTime())) date_posted = d.toISOString().slice(0, 10);
  }
  return {
    job_title: it.title,
    company,
    location,
    description,
    job_type: jt,
    job_url_direct: jobUrl,
    date_posted,
    resume_score: null,
  };
}

// Cover-letter generation, shared by the inline path and the Indeed-fetch
// background path so the voice/caching rules stay identical.
async function genCoverLetter(
  orKey: string, jobBlock: string, instruction: string, userText: string,
  history: Array<{ role: string; content: string }>,
): Promise<string> {
  const [asset, resume] = await Promise.all([getAsset("cover_letter_system"), getResumeText()]);
  return await callLLM(orKey, {
    model: MODEL_LETTER,
    system: `${asset}\n\n=== OTIS'S RESUME (for detail; do not contradict the facts above) ===\n${resume.slice(0, 3500)}`,
    user: `${jobBlock}\n\nINSTRUCTION: ${instruction}\nUser's exact words: ${userText}`,
    history: history.slice(-6),
    cacheSystem: true,
    maxTokens: 900,
    source: "bot_cover_letter",
  });
}

// Q&A about a specific job, shared by the inline and Indeed-fetch paths.
async function genLookupAnswer(orKey: string, jobBlock: string, question: string): Promise<string> {
  return await callLLM(orKey, {
    model: MODEL,
    system: "You are Otis's job-scout assistant. Answer his question about the job/platform concisely in plain text + light Markdown (**bold**, - bullets). Otis works almost entirely through Claude Code (AI-assisted automation and dev) and cares whether a role lets him leverage it — call that out when relevant. Ground your answer in the job data if provided; otherwise use general knowledge.",
    user: `${jobBlock}\n\nOtis asks: ${question}`,
    maxTokens: 700,
    source: "bot_lookup",
  });
}

// ---------- active job (per-chat working context) ----------
// The job the chat is currently working on. A FETCHED Indeed job lives nowhere
// else (not in the DB), so without this a follow-up ("make it tighter") had
// nothing to anchor to and findJob() grabbed a random DB row — the wrong-job
// revert. We stash the resolved job here and reuse it on revisions instead of
// searching again.
async function setActiveJob(chatId: string, job: any, source: string): Promise<void> {
  try {
    await sb("bot_active_job", {
      method: "POST",
      headers: { Prefer: "resolution=merge-duplicates,return=minimal" },
      body: JSON.stringify({ chat_id: chatId, job, source, updated_at: new Date().toISOString() }),
    });
  } catch (e) {
    console.error("setActiveJob failed", e);
  }
}

async function getActiveJob(chatId: string): Promise<any | null> {
  try {
    const since = new Date(Date.now() - HISTORY_MINUTES * 60000).toISOString();
    const rows = await sb(
      `bot_active_job?select=job,updated_at&chat_id=eq.${encodeURIComponent(chatId)}` +
        `&updated_at=gte.${since}&limit=1`,
    );
    return rows?.[0]?.job || null;
  } catch (e) {
    console.error("getActiveJob failed", e);
    return null;
  }
}

// Is the router's job_hint actually grounded in what Otis TYPED? The router
// can't see the fetched/active job, so on a revision it sometimes invents a
// hint pointing at a random DB role (e.g. it returned "Frontend AI Engineer"
// when he only said "speak on my behalf"). We only honor a hint — i.e. only
// switch jobs — when the hint (or all its distinctive words) appears in his
// message. Otherwise the active job stays put.
function hintInText(text: string, hint: string): boolean {
  const t = (text || "").toLowerCase();
  const h = (hint || "").toLowerCase().trim();
  if (!h) return false;
  if (h.length >= 4 && t.includes(h)) return true;
  const words = h.split(/[^a-z0-9]+/).filter((w) => w.length >= 4 && !FINDJOB_STOP.has(w));
  return words.length > 0 && words.every((w) => t.includes(w));
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
- Cover letter (new OR a revision of one already in the conversation): action="cover_letter", args={"job_hint":"<JUST the company name or one distinctive title word — e.g. 'ShipBob', NOT 'the ShipBob role'>", "instruction":"<what he wants, e.g. 'tighter', 'lead with bilingual', or 'standard'>"}. One short line in reply like "On it.".
- CRITICAL — REVISIONS: if he is adjusting the letter already in the conversation ("too long", "make it tighter", "more casual", "focus it on web design", "make this my default", any feedback on the current letter), set job_hint to "" (EMPTY) and put the change in instruction. Do NOT put a job name, title word, or anything from his feedback into job_hint. Only set job_hint when he clearly names a DIFFERENT, new job to switch to. When in doubt on a follow-up, leave job_hint empty.
- PASTED LISTING: if Otis pastes a full/long job description (a wall of listing text) and wants a letter or a question answered, do NOT try to extract a job_hint from it and do NOT match it to his saved listings. Use action="cover_letter" (or "lookup_job" if he's just asking about it) and LEAVE job_hint EMPTY ("") — the system uses the pasted text itself as the job. Reply one short line like "On it.".
- Edit profile / scoring brain — ONLY when he names a specific field to SET and gives the new value (e.g. "add Solutions Engineer to my target roles", "set my salary floor to 6k", "change my location to remote-only", "add Python to my skills"): action="update_profile", args={"field":"<target_roles|skills|job_types|location_preference|zip_code|salary_notes|custom_prompt>", "value": <COMPLETE new value; for arrays return the full updated array>}. Confirm in reply.
- Record a DISLIKE / exclusion / scoring preference that is NOT a specific field edit — "stop showing me X jobs", "don't show me Y", "I don't want Z", "avoid roles that...", "stop ranking W so high", "no more commission-only": action="feedback", args={"note":"<short rule>"}. Confirm in reply. TIEBREAKER: for ANY "stop / don't / avoid / no more / quit showing ..." phrasing, choose feedback, NOT update_profile.
- READ his resume (display only — the bot is READ-ONLY for the resume): action="resume_view", args={"which":"active"|"list"|"<version number>"}. "show / see / read / view / display / pull up my resume" -> "active"; "list resume versions" / "resume history" -> "list"; "show resume v2" -> "2". reply = one short line (the resume text is attached by the system).
- The bot CANNOT change the resume. If he asks to edit / update / add to / rewrite / replace his resume (or his PDF), action="none" and tell him resume edits are done in Claude Code, not here — offer to show the current one instead.
- LINKS: if his message contains a job URL and he wants a cover letter or a question answered, route to cover_letter or lookup_job and leave job_hint EMPTY (""). The system fetches Indeed links automatically, and for links it can't open it asks him to paste the text — either way, NEVER guess a job_hint from a URL or the words around it.
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

    // ---- Indeed link → real fetch ----
    // If he pasted an Indeed job link (not the full text) and wants a letter or
    // a question answered, fetch the actual listing via Apify and use IT. This
    // is the only path that opens a link. Heavy work runs AFTER an immediate ack
    // via EdgeRuntime.waitUntil, so the webhook returns fast and Telegram never
    // retries (which would double-process). Non-Indeed links (no jk) fall
    // through to the per-branch "paste the text" guard below.
    const indeedJk = pastedJob(text) ? null : extractJk(text);
    if (indeedJk && (action === "cover_letter" || action === "lookup_job")) {
      await reply(token, chatId, "🔎 Pulling that listing from Indeed…");
      const bg = (async () => {
        try {
          const job = await fetchIndeedJob(indeedJk, cfg.apify_token);
          if (!job) {
            const m = "I couldn't pull that listing — it may be expired, or it's a board I can't open. Paste the full job text and I'll work from it.";
            await reply(token, chatId, m);
            await saveTurn(chatKey, "user", text);
            await saveTurn(chatKey, "assistant", m);
            return;
          }
          let bgOut: string;
          if (action === "cover_letter") {
            const jobBlock =
              `JOB: ${job.job_title} at ${job.company}\n${job.location || ""}\n\nDESCRIPTION:\n${(job.description || "").slice(0, 6000)}`;
            const letter = await genCoverLetter(
              orKey, jobBlock, String(args.instruction || "standard"), text, history,
            );
            const age = agePosted(job.date_posted);
            bgOut = `📄 <b>${esc(job.job_title)}</b> — ${esc(job.company)}\n` +
              (age ? `🗓 ${esc(age)}\n` : "") + `${esc(job.job_url_direct)}\n\n` + mdToHtml(letter);
          } else {
            const jobBlock =
              `JOB: ${job.job_title} at ${job.company}\nLocation: ${job.location || "?"}\n\nDESCRIPTION:\n${(job.description || "").slice(0, 5000)}`;
            const ans = await genLookupAnswer(orKey, jobBlock, String(args.question || text));
            bgOut = `📄 <b>${esc(job.job_title)}</b> — ${esc(job.company)}\n\n` + mdToHtml(ans);
          }
          // Stash the fetched job so a follow-up ("make it tighter") reuses it
          // instead of falling into findJob and grabbing a random DB role.
          await setActiveJob(chatKey, job, "indeed_fetch");
          await reply(token, chatId, bgOut);
          await saveTurn(chatKey, "user", text);
          await saveTurn(chatKey, "assistant", bgOut);
        } catch (e) {
          console.error("indeed fetch bg error", e);
          await reply(token, chatId, "⚠️ Hit a snag pulling that listing. Paste the job text and I'll do it.");
        }
      })();
      // @ts-ignore — EdgeRuntime is provided by the Supabase edge runtime
      EdgeRuntime.waitUntil(bg);
      return new Response("ok");
    }

    if (action === "lookup_job") {
      // Search ALL active jobs (not just the top matches) for a specific listing,
      // then answer his question grounded in it (or from general knowledge if missing).
      const pasted = pastedJob(text);
      if (!pasted && hasJobUrl(text)) {
        // A link to a job we can't open and no pasted text → never guess a DB row.
        out = "I can't open links — I only know listings I've already scraped into your matches. Paste the full job text (copy the whole description) and I'll break it down.";
        await reply(token, chatId, out);
        await saveTurn(chatKey, "user", text);
        await saveTurn(chatKey, "assistant", out);
        return new Response("ok");
      }
      const hint = String(args.job_hint || "").trim();
      const active = await getActiveJob(chatKey);
      // Same sticky rule as letters: a follow-up ("is it remote?") stays on the
      // active job; only switch when his own words name a different one.
      let job: any = pasted ? null : active;
      if (!pasted) {
        if (hint && hintInText(text, hint)) job = (await findJob(hint)) || active;
        else if (!active && hint) job = await findJob(hint);
      }
      const question = String(args.question || text);
      const legitLine = (j: any) =>
        j && j.legitimacy_verdict && j.legitimacy_verdict !== "LEGIT"
          ? `\nLEGITIMACY FLAG: ${j.legitimacy_verdict}${j.legitimacy_summary ? ` — ${j.legitimacy_summary}` : ""}. If asked anything about this employer, lead with this warning.`
          : "";
      const jobBlock = pasted
        ? `JOB (pasted by Otis — answer about THIS listing, do not look one up):\n${pasted.slice(0, 5000)}`
        : job
        ? `JOB: ${job.job_title} at ${job.company}\nLocation: ${job.location || "?"}\nScore: ${job.resume_score ?? "?"}/100${legitLine(job)}\n\nDESCRIPTION:\n${(job.description || "").slice(0, 5000)}`
        : hint
        ? `(No job matching "${hint}" was found in the scraped listings — it may have aged out or scored low. Answer from general knowledge and say it wasn't in his current listings.)`
        : `(No specific job in context. Answer from general knowledge.)`;
      const ans = await genLookupAnswer(orKey, jobBlock, question);
      const vBadge: Record<string, string> = {
        AVOID: " · 🛑 AVOID", SUSPICIOUS: " · ⚠️ suspicious", CAUTION: " · ⚠️ caution",
      };
      const head = job
        ? `📄 <b>${esc(job.job_title)}</b> — ${esc(job.company)}` +
          (job.resume_score != null ? ` · ${Math.round(job.resume_score / 10)}/10` : "") +
          (vBadge[String(job.legitimacy_verdict || "").toUpperCase()] || "") + "\n\n"
        : "";
      out = head + mdToHtml(ans);
      if (pasted) {
        await setActiveJob(chatKey, {
          job_title: "the pasted listing", company: "", location: "",
          description: pasted, job_url_direct: "", date_posted: null,
        }, "pasted");
      } else if (job) {
        await setActiveJob(chatKey, job, "lookup_job");
      }
    } else if (action === "cover_letter") {
      const pasted = pastedJob(text);
      if (!pasted && hasJobUrl(text)) {
        // A link to a job we can't open and no pasted text → never write a letter
        // for a random DB row. Ask Otis to paste the listing first.
        out = "I can't open job links yet — I only know listings I've already scraped. Paste the full job text (copy the whole description) and I'll write the letter.";
        await reply(token, chatId, out);
        await saveTurn(chatKey, "user", text);
        await saveTurn(chatKey, "assistant", out);
        return new Response("ok");
      }
      const hint = String(args.job_hint || "").trim();
      const active = await getActiveJob(chatKey);
      // STICKY job: default to the active job (the one we're working on). Only
      // switch when his OWN words name a different job — a grounded hint — or
      // when there's no active job yet. This stops the router from inventing a
      // DB role on a revision and yanking the letter onto the wrong job.
      let job: any = pasted ? null : active;
      if (!pasted) {
        if (hint && hintInText(text, hint)) job = (await findJob(hint)) || active;
        else if (!active && hint) job = await findJob(hint);
      }
      const jobBlock = pasted
        ? `JOB (pasted by Otis — write the letter for THIS listing, do not look one up):\n${pasted.slice(0, 6000)}`
        : job
        ? `JOB: ${job.job_title} at ${job.company}\n${job.location || ""}\n\nDESCRIPTION:\n${(job.description || "").slice(0, 6000)}`
        : "(Revise the cover letter already in this conversation — the job is in the history above.)";
      const instruction = String(args.instruction || "standard");
      const letter = await genCoverLetter(orKey, jobBlock, instruction, text, history);
      const age = job ? agePosted(job.date_posted) : null;
      out = (job ? `📄 <b>${esc(job.job_title)}</b> — ${esc(job.company)}\n` + (age ? `🗓 ${esc(age)}\n` : "") +
        (job.job_url_direct ? `${esc(job.job_url_direct)}\n` : "") + "\n" : "") + mdToHtml(letter);
      // Remember what we worked on so the next revision reuses it (not findJob).
      if (pasted) {
        await setActiveJob(chatKey, {
          job_title: "the pasted listing", company: "", location: "",
          description: pasted, job_url_direct: "", date_posted: null,
        }, "pasted");
      } else if (job) {
        await setActiveJob(chatKey, job, "cover_letter");
      }
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
