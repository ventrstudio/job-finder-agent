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
# HARD per-run charge cap for the scraper.py searches. memo23 is pay-per-event, so the
# actor AUTO-ABORTS the run the instant its charges cross this line. This is the only
# deterministic ceiling — maxJobs is a SOFT cap and overshoots badly (measured 08-02-2026:
# maxJobs=200 billed 400 items, 100% over, not the ~25% previously assumed).
#
# Lowered $1.00 -> $0.25 on 08-02-2026. Measured real runs are $0.090 (remote) and $0.051
# (local), so $1.00 left 10-20x of unused rope for no reason — and rope is exactly what the
# 07-25 runaway ($24.81) used. $0.25 still leaves ~3x headroom for a heavy day.
# The alert-enrichment path has its own, much tighter cap: alert_ingest.APIFY_ALERT_MAX_RUN_USD.
APIFY_MAX_RUN_USD = 0.25

# Local search — on-site/hybrid jobs near home base. Each query runs twice:
# once nationwide-remote, once location-bound to this area.
APIFY_LOCAL_LOCATION = "Port St. Lucie, FL"
APIFY_LOCAL_RADIUS = "50"  # miles — covers the Treasure Coast + north Palm Beach County

# LinkedIn jobs source (added 07-14-2026). curious_coder/linkedin-jobs-scraper
# scrapes LinkedIn's PUBLIC guest jobs search — no cookie, no LinkedIn account
# touched. Pay-per-result $0.001/job; `count` is a HARD cap on dataset items
# (verified live 07-14: count=10 returned exactly 10), so cost = count × $0.001
# per run. count=40 daily ≈ $1.2/mo. Total Apify (Indeed ~$3.1 + LinkedIn ~$1.2)
# ≈ $4.3/mo — still under the $5 free-tier credit, but the margin is thin: if
# either dial goes up, something else must come down.
LINKEDIN_ENABLED = True
LINKEDIN_ACTOR_ID = "curious_coder/linkedin-jobs-scraper"
LINKEDIN_MAX_ROWS = 40  # actor `count` (min 10) — the LinkedIn cost dial
LINKEDIN_JOB_TYPES = ["F", "C", "P"]  # LinkedIn f_JT values: Full-time, Contract, Part-time
LINKEDIN_LOCAL_LOCATION = "Port St. Lucie, Florida, United States"

# -----------------------------------------------------------------
# 2b. DEDICATED LOCAL SEARCH BUDGET + COMMUTE GRADING (07-26-2026)
# -----------------------------------------------------------------
# Why this exists: local jobs were getting starved. Remote + local Indeed URLs
# shared ONE memo23 run at maxJobs=60, and the nationwide-remote pass (crawled
# first) ate the whole budget, so the local URLs returned ~0. The fix is a
# DEDICATED local run with its own budget + broader local queries. The narrow
# remote terms ("Claude Code") barely exist in the Treasure Coast market, so
# local uses generic, high-recall titles. Indeed q= is full-text (it matches the
# title AND the description) and the scorer reads the full description anyway, so
# generic titles are fine — the description text is what actually gets judged.
LOCAL_SEARCH_QUERIES = [
    "web developer",
    "software developer",
    "developer",
    "IT",
    "technology",
    "automation",
    "AI",
    "API integration",
    "React",
    "Supabase",
    "no-code",
]

# Dedicated maxJobs for the LOCAL memo23 run — a separate cost dial from the
# remote run's APIFY_MAX_ROWS_GLOBAL. Kept modest; the local market is small.
APIFY_LOCAL_MAX_ROWS = 40

# Dedicated LinkedIn `count` for the LOCAL guest-search run (only used when
# LINKEDIN_ENABLED). Smaller than the remote count — local supply is thin.
LINKEDIN_LOCAL_MAX_ROWS = 20

# --- Commute grading (home base -> job coordinates) ---
# Home base for the commute estimate: zip 34952 (SE Port St. Lucie, FL).
HOME_LAT = 27.2769
HOME_LNG = -80.3006
# Haversine fallback tuning (used only when OSRM routing is unreachable):
# straight-line miles * ROAD_FACTOR / AVG_MPH * 60 = estimated drive minutes.
COMMUTE_ROAD_FACTOR = 1.3   # roads aren't straight lines — pad the crow-flight distance
COMMUTE_AVG_MPH = 42        # blended surface-street + highway average for the area

# --- Location-tier ranking bonus ---
# Added to resume_score (which is score*10) so preferred locations rank higher in
# the digest. Tier order (best -> worst):
#   1 = local + remote     (near home AND remote — ideal)
#   2 = local hybrid       (near home, some in-office)
#   3 = non-local remote   (remote but not tied to the local market)
#   4 = local in-person    (near home, fully on-site)
# The bumped resume_score is capped at 100 downstream so it still fits the column.
LOCATION_TIER_BONUS = {1: 10, 2: 6, 3: 2, 4: 0}

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
