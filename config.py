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

# Heartbeat monitor (dead-man's-switch). Optional — leave unset and the
# pipeline simply skips the ping. Set it to a healthchecks.io ping URL to
# get alerted by email if the pipeline ever stops running at all.
HEALTHCHECK_URL: str = os.environ.get("HEALTHCHECK_URL", "")

# =================================================================
# 2. SEARCH CONFIGURATION
# =================================================================

# Target roles to search for (from agent_profile).
# NOTE: each string is one Indeed keyword search. Indeed ranks by relevance, and
# we only take the newest N per query (APIFY_MAX_ROWS_PER_QUERY) inside a short
# recency window — so a NARROW query can miss a perfect role that ranks low for
# it. Keep a few BROAD nets ("automation specialist", "AI engineer") plus the
# bullseye literal "Claude Code" (high signal, almost nobody else searches it).
# A 10/10 Claude-Code role (Redfish, 06-2026) was missed because none of the old
# 8 narrow queries surfaced it; these broader nets are the fix.
SEARCH_QUERIES = [
    # bullseye / high-signal
    "Claude Code",
    "Claude AI automation",
    "AI automation engineer",
    "AI engineer",
    "AI implementation specialist",
    "AI integration developer",
    # broad automation nets (these catch the relevance-ranked long tail)
    "automation specialist",
    "workflow automation developer",
    "operations automation",
    "no-code automation specialist",
    "n8n Make Zapier developer",
    # dev / web
    "React developer contract remote",
    "Supabase developer",
    "freelance web developer AI",
]

# Companies to exclude entirely. Case-insensitive substring match against the
# company name. Blocklisted companies are dropped at scrape time — never saved,
# never scored, never shown in a digest.
COMPANY_BLOCKLIST = [
    "DataAnnotation",
]

# Apify scraper settings (borderline/indeed-scraper)
APIFY_ACTOR_ID = "borderline/indeed-scraper"
APIFY_COUNTRY = "us"
APIFY_JOB_TYPES = ["fulltime", "contract", "parttime"]  # Indeed jt() filter values
APIFY_FROM_DAYS = "3"  # last 3 days — was 1; a job missed on day-1 (cap/relevance) gets more shots. Dedup collapses repeats.
APIFY_SORT = "date"  # newest first
APIFY_MAX_ROWS_PER_QUERY = 25  # per Indeed search URL — was 15; deeper so a good role ranked low for a query still gets pulled
APIFY_MAX_ROWS_GLOBAL = 300  # hard ceiling per run (cost cap) — was 150; raised to give the added queries room

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
JOBS_TO_SCORE_PER_RUN = 300  # matches APIFY_MAX_ROWS_GLOBAL so a day clears same-run

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
