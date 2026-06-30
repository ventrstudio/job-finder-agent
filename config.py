import os
from dotenv import load_dotenv

load_dotenv()

# =================================================================
# 1. CORE SYSTEM CONFIGURATION
# =================================================================
SUPABASE_URL: str = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY: str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_TABLE_NAME: str = "jobs"
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY")  # legacy, unused
OPENROUTER_API_KEY: str = os.environ.get("OPENROUTER_API_KEY")
EMAILIT_API_KEY: str = os.environ.get("EMAILIT_API_KEY")
APIFY_TOKEN: str = os.environ.get("APIFY_TOKEN")
FIRECRAWL_API_KEY: str = os.environ.get("FIRECRAWL_API_KEY", "")  # Tier 2 reputation web search

# Heartbeat monitor (dead-man's-switch). Optional — leave unset and the
# pipeline simply skips the ping. Set it to a healthchecks.io ping URL to
# get alerted by email if the pipeline ever stops running at all.
HEALTHCHECK_URL: str = os.environ.get("HEALTHCHECK_URL", "")

# =================================================================
# 2. SEARCH CONFIGURATION
# =================================================================

# Target roles to search for (from agent_profile).
# STRATEGY (06-19-2026): PRECISION, not width. Each string is one Indeed keyword
# search; Indeed ranks by relevance and we take only the newest N per query
# (APIFY_MAX_ROWS_PER_QUERY) within a short window. So the way to reliably catch
# an exact-fit role WITHOUT spending more is a NARROW query where that role ranks
# #1-5 (always survives the cap), not a broad net where it's buried at rank 50.
# The breadth/long-tail is handled cheaply by the Indeed-alert-email ingestion
# (alerts already did the matching server-side) — NOT by widening the scrape.
#
# A 10/10 role (Redfish, 06-2026, Indeed title "AI Specialist – Claude AI /
# Claude Code / Cowork") was missed purely because no query searched its words.
# The literal "Claude Code" query now catches that exact title for ~zero cost
# (few postings match it). Keep these tight and high-signal.
SEARCH_QUERIES = [
    "Claude Code",                       # bullseye literal — high signal, ~zero competition
    "Claude AI automation",
    "AI automation engineer",
    "AI integration developer",
    "workflow automation developer",
    "n8n Make Zapier developer",
    "no-code automation specialist",
    "Supabase developer",
    "React developer contract remote",
]

# Companies to exclude entirely. Case-insensitive substring match against the
# company name. Blocklisted companies are dropped at scrape time — never saved,
# never scored, never shown in a digest.
COMPANY_BLOCKLIST = [
    "DataAnnotation",
]

# Apify scraper settings (memo23/apify-indeed-cheerio-ppr)
# Swapped off borderline/indeed-scraper 06-30-2026: borderline ran $0.40-1.26/run
# (~$26/mo, ate the whole Apify plan). memo23 is the "bypass 25-cap" Cheerio
# scraper — cost is driven by maxJobs (it pages the full result set then trims to
# maxJobs). Measured cost model: ~$0.0014 per job returned (e.g. maxJobs=80 ≈
# $0.11/run ≈ $3.4/mo), which fits Apify's FREE tier ($5/mo credits). See
# APIFY-COST-NOTES.md for the full measurement.
APIFY_ACTOR_ID = "memo23/apify-indeed-cheerio-ppr"
APIFY_COUNTRY = "us"
APIFY_JOB_TYPES = ["fulltime", "contract", "parttime"]  # Indeed jt() filter values
APIFY_FROM_DAYS = "1"  # last 24h. Precision queries catch a role the day it posts; widening this just adds cost. Breadth = alert-email ingestion.
APIFY_SORT = "date"  # newest first
# maxJobs is the cost dial for memo23 — it caps the crawl, not just the output.
# NOTE: it's a SOFT cap with many startUrls — a live 18-URL CI run at maxJobs=80
# returned ~100 raw items (it finishes each URL's current page batch, ~+25%). So
# real cost ≈ (actual items) × ~$0.0014. 60 → ~75 actual → ~$0.10/run → ~$3.1/mo,
# which keeps comfortable margin under the $5 free-tier credit even with a few
# manual runs. The breadth layer is alert-email ingestion, so the scrape doesn't
# need to be deep. Bump toward 120-150 only if daily volume ever justifies it.
APIFY_MAX_ROWS_GLOBAL = 60  # soft ceiling per run = memo23 maxJobs (cost dial)

# Local search — on-site/hybrid jobs near home base. Each query runs twice:
# once nationwide-remote, once location-bound to this area.
APIFY_LOCAL_LOCATION = "Port St. Lucie, FL"
APIFY_LOCAL_RADIUS = "50"  # miles — covers the Treasure Coast + north Palm Beach County

# =================================================================
# 3. SCORING CONFIGURATION
# =================================================================
# Pinned to a cheap, deterministic model for predictable cost + stable scores
# run-to-run. Bump to "anthropic/claude-haiku-4.5" or "openrouter/auto" if
# scoring nuance ever feels off.
SCORING_MODEL = "google/gemini-2.5-flash-lite"
SCORING_THRESHOLD = 5  # minimum score to include in digest (out of 10)
JOBS_TO_SCORE_PER_RUN = 150  # matches APIFY_MAX_ROWS_GLOBAL so a day clears same-run

# =================================================================
# 3b. LEGITIMACY / SCAM SCREEN
# =================================================================
# Two-tier "is this real?" gate that runs alongside fit scoring:
#   Tier 1 (scam_check.py): free heuristic on every new job -> scam_risk_score.
#   Tier 2 (reputation.py): Firecrawl web search + LLM verdict, cached per
#   company, run ONLY on jobs that clear SCORING_THRESHOLD (the digest set), so
#   the web spend stays tiny. An AVOID verdict demotes + warns; it never silently
#   deletes a job (false-positive safety — Otis keeps the final call).
SCREEN_ENABLED = True
LEGITIMACY_MODEL = "google/gemini-2.5-flash"   # judges reputation snippets (cheap, capable)
LEGITIMACY_SEARCH_LIMIT = 8                      # Firecrawl results per query
LEGITIMACY_EXTRA_SEARCH = False                  # add a 2nd "official site/funding" query (more credits)
LEGITIMACY_CACHE_DAYS = 45                       # re-check a company only after this many days
LEGITIMACY_MAX_COMPANIES_PER_RUN = 25            # hard cap on Tier 2 checks per run (cost ceiling)
SCAM_RISK_WARN_THRESHOLD = 35                    # heuristic score that surfaces a warning in the digest

# =================================================================
# 4. DELIVERY CONFIGURATION
# =================================================================
# Telegram is the primary delivery channel (replaces the old EmailIt digest).
# Both vars come from GitHub Actions secrets:
#   TELEGRAM_BOT_TOKEN - from @BotFather
#   TELEGRAM_CHAT_ID   - your numeric chat id (DM the bot once to get it)
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")

# Email is the primary digest channel. Sender must be on the verified
# mail.ventr.studio domain (the old coreindustries.io sender died in the
# 05-29-2026 mail migration). Telegram carries a one-line nudge + the chat bot.
EMAILIT_FROM = "Job Scout <alerts@mail.ventr.studio>"
EMAILIT_TO = "otis@ventr.studio"
EMAILIT_API_URL = "https://api.emailit.com/v1/emails"

# =================================================================
# 5. PROCESSING LIMITS
# =================================================================
# =================================================================
# 6. FEEDBACK WEBHOOK
# =================================================================
N8N_FEEDBACK_WEBHOOK_URL = "https://coreindustries.app.n8n.cloud/webhook/job-feedback"

JOB_EXPIRY_DAYS = 30
JOB_DELETION_DAYS = 60
