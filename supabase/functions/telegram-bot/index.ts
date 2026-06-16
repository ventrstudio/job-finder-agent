// Telegram bot for the Job Scout — the interactive AI layer.
//
// Locked to a single Telegram user. Four abilities, routed by one LLM call:
//   1. Q&A about listings   (read jobs, answer)
//   2. Generate cover letter (resume_text + job description -> text to paste)
//   3. Edit profile          (update agent_profile fields)
//   4. Give feedback         (append a rule to agent_profile.anti_patterns)
//
// Security:
//   - Telegram webhook secret header must match (set via setWebhook secret_token).
//   - message.from.id must equal allowed_user_id. Anyone else is ignored silently.
//
// Secrets are function env vars (set via `supabase secrets set`):
//   TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, ALLOWED_USER_ID, OPENROUTER_API_KEY
// SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are auto-injected by the platform.

import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const SB_URL = Deno.env.get("SUPABASE_URL")!;
const SB_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";
const MODEL = "openrouter/auto";
const SCORE_MATCH = 50; // 0-100 scale; 50 == 5/10 threshold

// ---------- tiny Supabase REST helpers (service role) ----------
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

async function getProfile(): Promise<any> {
  const rows = await sb("agent_profile?select=*&limit=1");
  return rows?.[0] || {};
}

// Human age from a YYYY-MM-DD date string, e.g. "posted 3 days ago".
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

async function getRecentMatches(limit = 15): Promise<any[]> {
  const cols =
    "job_id,job_title,company,location,is_remote,job_type,salary_min,salary_max,salary_interval,resume_score,score_tldr,job_url_direct,date_posted,scraped_at";
  const rows: any[] = await sb(
    `jobs?select=${cols}&is_active=eq.true&resume_score=gte.${SCORE_MATCH}` +
      `&order=resume_score.desc,scraped_at.desc&limit=${limit}`,
  );
  for (const j of rows) j.posted = agePosted(j.date_posted) || "posting date unknown";
  return rows;
}

async function findJob(hint: string): Promise<any | null> {
  // Try title match, then company match. ilike with wildcards.
  const enc = encodeURIComponent(`%${hint.trim()}%`);
  const cols = "job_id,job_title,company,location,description,job_url_direct,date_posted,resume_score";
  let rows = await sb(
    `jobs?select=${cols}&job_title=ilike.${enc}&order=resume_score.desc&limit=1`,
  );
  if (!rows?.length) {
    rows = await sb(
      `jobs?select=${cols}&company=ilike.${enc}&order=resume_score.desc&limit=1`,
    );
  }
  return rows?.[0] || null;
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

async function llm(
  key: string,
  system: string,
  user: string,
  opts: { json?: boolean; maxTokens?: number; source?: string } = {},
): Promise<string> {
  const body: any = {
    model: MODEL,
    messages: [
      { role: "system", content: system },
      { role: "user", content: user },
    ],
    temperature: 0.3,
    max_tokens: opts.maxTokens ?? 900,
    usage: { include: true }, // OpenRouter returns real USD cost in usage.cost
  };
  if (opts.json) body.response_format = { type: "json_object" };

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
  await logCost(opts.source || "bot_router", data?.model || MODEL, data?.usage);
  return (data?.choices?.[0]?.message?.content || "").trim();
}

async function costSummary(): Promise<string> {
  // Reads the rollup; returns a short report. On-demand only (never auto-sent).
  const rows: any[] = await sb(
    "llm_costs?select=created_at,source,cost_usd,total_tokens&order=created_at.desc&limit=2000",
  );
  const now = Date.now();
  const day = 864e5;
  let today = 0, week = 0, month = 0, all = 0;
  let cToday = 0, cAll = 0;
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
  const srcLines = Object.entries(bySource)
    .sort((a, b) => b[1] - a[1])
    .map(([s, c]) => `  • ${s}: ${m(c)}`)
    .join("\n");
  return [
    "💸 <b>LLM cost</b>",
    `Today: ${m(today)} (${cToday} calls)`,
    `Last 7d: ${m(week)}`,
    `Last 30d: ${m(month)}`,
    `All time: ${m(all)} (${cAll} calls)`,
    "",
    "By source (all time):",
    srcLines || "  (none yet)",
  ].join("\n");
}

function parseJson(text: string): any {
  let t = text.trim();
  if (t.startsWith("```")) {
    t = t.replace(/^```[a-z]*\n?/i, "").replace(/```$/, "").trim();
  }
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
  // Telegram caps at 4096 chars; chunk on paragraph boundaries.
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

const HELP = [
  "🔍 <b>Job Scout</b> — I’m your job assistant. Talk to me normally:",
  "",
  "• <b>Ask about listings</b> — “show today’s top 5 remote roles”, “any contract gigs?”, “tell me about the Stack AV one”",
  "• <b>Cover letter</b> — “write a cover letter for the Gottlieb automation role”",
  "• <b>Edit your profile</b> — “add ‘no on-call’ to my dealbreakers”, “bump my target roles to include Solutions Engineer”",
  "• <b>Feedback</b> — “stop showing me teaching jobs”, “I’m not interested in pure DevOps”",
  "• <b>/cost</b> — LLM token spend (today / 7d / 30d / all time)",
  "",
  "Your full digest still comes by email each morning.",
].join("\n");

const ROUTER_SYSTEM = `You are the Job Scout assistant for Otis, a job seeker. You help him triage job listings, write cover letters, edit his job-search profile, and record feedback. You are given his PROFILE and a list of RECENT MATCHING JOBS as JSON.

Reply ONLY with a JSON object:
{
  "reply": "<your message to Otis, plain text or light HTML (<b>,<i>) — used for Q&A, lists, confirmations>",
  "action": "none" | "cover_letter" | "update_profile" | "feedback",
  "args": { ... }
}

Rules:
- For questions, summaries, or listing jobs: action="none", put the answer in "reply". Use the RECENT MATCHING JOBS data. Be concise. Scores in the data are 0-100; divide by 10 for display. ALWAYS include how long ago each job was posted — use each job's "posted" field verbatim (e.g. "posted 3 days ago"). Format each listed job as: "<b>Title</b> — Company · score/10 · posted X days ago" then the link on the next line.
- For a cover letter request: action="cover_letter", args={"job_hint":"<key words from the title or company he named>"}. Put a one-line "On it…" in reply.
- To change his profile: action="update_profile", args={"field":"<one of: target_roles, skills, job_types, location_preference, zip_code, salary_notes, custom_prompt>", "value": <the COMPLETE new value — for array fields return the full updated array including existing items>}. Confirm what you changed in reply.
- To record a dislike/preference that should affect future scoring: action="feedback", args={"note":"<short rule, e.g. 'Not interested in DevOps or pure infrastructure roles'>"}. Confirm in reply.
- Never invent jobs that aren't in the data. If asked about a job not present, say you don't see it in the recent matches.`;

const COVER_SYSTEM = `You write short, sharp, proof-forward cover letters / application notes for Otis. Use his RESUME and the JOB DESCRIPTION. 180-260 words. Specific, no fluff, no clichés ("I am excited to", "fast-paced"). Lead with a concrete reason he fits. Plain text, ready to paste. No placeholders — if a detail is unknown, leave it out.`;

Deno.serve(async (req: Request): Promise<Response> => {
  // Always 200 to Telegram (so it doesn't retry); do work inside.
  const cfg = getConfig();
  const token = cfg.telegram_bot_token;
  const secret = cfg.telegram_webhook_secret;
  const allowed = String(cfg.allowed_user_id || "");
  const orKey = cfg.openrouter_api_key;

  // 1) webhook secret check
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

  // 2) hard user lock
  if (!msg || fromId !== allowed) {
    console.warn(`ignored update from ${fromId} (allowed ${allowed})`);
    return new Response("ok");
  }
  if (!text) return new Response("ok");

  if (text === "/start" || text === "/help") {
    await reply(token, chatId, HELP);
    return new Response("ok");
  }

  if (text === "/cost" || /\bhow much.*(spent|cost)|llm cost|token cost|my spend\b/i.test(text)) {
    try {
      await reply(token, chatId, await costSummary());
    } catch (e) {
      console.error("cost summary failed", e);
      await reply(token, chatId, "⚠️ Couldn't pull cost data right now.");
    }
    return new Response("ok");
  }

  try {
    const [profile, jobs] = await Promise.all([getProfile(), getRecentMatches()]);

    const ctx =
      `PROFILE:\n${JSON.stringify(profile)}\n\n` +
      `RECENT MATCHING JOBS (newest/highest first):\n${JSON.stringify(jobs)}\n\n` +
      `OTIS SAID:\n${text}`;

    const routed = parseJson(await llm(orKey, ROUTER_SYSTEM, ctx, { json: true, maxTokens: 900 }));
    const action = routed.action || "none";
    const args = routed.args || {};
    let out = routed.reply || "";

    if (action === "cover_letter") {
      const job = await findJob(args.job_hint || text);
      if (!job) {
        out = `I couldn't find a job matching "${esc(args.job_hint || text)}" in your recent matches. Try the exact title or company.`;
      } else {
        const letter = await llm(
          orKey,
          COVER_SYSTEM,
          `RESUME:\n${profile.resume_text || "(no resume on file)"}\n\nJOB: ${job.job_title} at ${job.company}\n${job.location || ""}\n\nDESCRIPTION:\n${(job.description || "").slice(0, 6000)}`,
          { maxTokens: 800, source: "bot_cover_letter" },
        );
        const age = agePosted(job.date_posted);
        out =
          `📄 <b>${esc(job.job_title)}</b> — ${esc(job.company)}\n` +
          (age ? `🗓 ${esc(age)}\n` : "") +
          (job.job_url_direct ? `${esc(job.job_url_direct)}\n` : "") +
          `\n${esc(letter)}`;
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
        const patch: any = { [field]: args.value, updated_at: new Date().toISOString() };
        await sb(`agent_profile?id=eq.${profile.id}`, {
          method: "PATCH",
          headers: { Prefer: "return=minimal" },
          body: JSON.stringify(patch),
        });
        out = out || `✅ Updated <b>${esc(field)}</b>.`;
      }
    } else if (action === "feedback") {
      const note = String(args.note || text);
      const cur: string[] = Array.isArray(profile.anti_patterns) ? profile.anti_patterns : [];
      const next = [...cur, note];
      await sb(`agent_profile?id=eq.${profile.id}`, {
        method: "PATCH",
        headers: { Prefer: "return=minimal" },
        body: JSON.stringify({ anti_patterns: next, updated_at: new Date().toISOString() }),
      });
      out = out || `✅ Noted: future scoring will weigh this. "${esc(note)}"`;
    }

    await reply(token, chatId, out || "Done.");
  } catch (e) {
    console.error("handler error", e);
    await reply(token, chatId, "⚠️ Hit an error handling that. Try rephrasing, or ask again in a moment.");
  }

  return new Response("ok");
});
