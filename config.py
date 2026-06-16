import os
from dotenv import load_dotenv

load_dotenv()

# =================================================================
# 1. CORE SYSTEM CONFIGURATION
# =================================================================
SUPABASE_URL: str = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY: str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_TABLE_NAME: str = "jobs"
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY")
EMAILIT_API_KEY: str = os.environ.get("EMAILIT_API_KEY")
APIFY_TOKEN: str = os.environ.get("APIFY_TOKEN")

# Heartbeat monitor (dead-man's-switch). Optional — leave unset and the
# pipeline simply skips the ping. Set it to a healthchecks.io ping URL to
# get alerted by email if the pipeline ever stops running at all.
HEALTHCHECK_URL: str = os.environ.get("HEALTHCHECK_URL", "")

# =================================================================
# 2. SEARCH CONFIGURATION
# =================================================================

# Target roles to search for (from agent_profile)
SEARCH_QUERIES = [
    "AI automation engineer",
    "workflow automation developer",
    "n8n Make Zapier developer",
    "no-code automation specialist",
    "AI integration developer",
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
APIFY_FROM_DAYS = "1"  # last 24 hours
APIFY_SORT = "date"  # newest first
APIFY_MAX_ROWS_PER_QUERY = 15  # per Indeed search URL
APIFY_MAX_ROWS_GLOBAL = 150  # hard ceiling per run (cost cap)

# Local search — on-site/hybrid jobs near home base. Each query runs twice:
# once nationwide-remote, once location-bound to this area.
APIFY_LOCAL_LOCATION = "Port St. Lucie, FL"
APIFY_LOCAL_RADIUS = "50"  # miles — covers the Treasure Coast + north Palm Beach County

# =================================================================
# 3. SCORING CONFIGURATION
# =================================================================
SCORING_MODEL = "claude-haiku-4-5-20251001"
SCORING_THRESHOLD = 5  # minimum score to include in digest (out of 10)
JOBS_TO_SCORE_PER_RUN = 150  # matches APIFY_MAX_ROWS_GLOBAL so a day clears same-run

# =================================================================
# 4. DELIVERY CONFIGURATION
# =================================================================
# Telegram is the primary delivery channel (replaces the old EmailIt digest).
# Both vars come from GitHub Actions secrets:
#   TELEGRAM_BOT_TOKEN - from @BotFather
#   TELEGRAM_CHAT_ID   - your numeric chat id (DM the bot once to get it)
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")

# EmailIt digest is retired (Core Industries domain torn down 05-29-2026).
# Kept only so resend_digest.py / send_digest.py still import; not used by main.
EMAILIT_FROM = "Job Scout <notifications@coreindustries.io>"
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
