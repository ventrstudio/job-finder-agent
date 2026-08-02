"""
Indeed job-alert email ingestion — the cheap "breadth" layer for the scout.

The daily Apify scrape uses a few high-precision queries (so an exact-fit role
ranks #1-5 and always survives the cap). The long tail is covered here instead of
by widening that scrape: Indeed already did broad matching server-side and emailed
the results, so we just read those emails and pull the jobs out.

Flow:
  1. IMAP-read recent Indeed job-alert emails (FROM donotreply@jobalert.indeed.com)
     that aren't already labeled processed.
  2. Parse each listing's jk (job key == our job_id) + title + company.
  3. Dedup jk against jobs already in Supabase (job_id is the upsert key, so a job
     seen by BOTH the scrape and an alert is stored once — no double-dip).
  4. For the NEW jks, fetch the FULL description by running a targeted Apify search
     (Indeed job-detail URLs are auth-walled, but search URLs return descriptionText)
     and filtering results to the target jobKeys. Reuses scraper._map_apify_item.
  5. Return the new jobs in our schema for the pipeline to save + score.
  6. Label the processed emails (Gmail X-GM-LABELS) so they're never parsed again.

Idempotent two ways: job-level (job_id) and email-level (processed label).

Headless-safe: reads GMAIL_USER / GMAIL_APP_PASSWORD / GMAIL_IMAP_HOST from env.
If GMAIL_APP_PASSWORD is unset, ingest is a no-op (safe to ship before the secret
exists). Run `python alert_ingest.py` for a dry run (parse + dedup + fetch report,
no DB writes, no email labeling).
"""

import datetime
import email
import imaplib
import logging
import os
import re
from decimal import Decimal
from email.message import Message
from urllib.parse import quote_plus

import config
import supabase_utils
from scraper import _get_client, _map_apify_item

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

IMAP_HOST = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

ALERT_FROM = "donotreply@jobalert.indeed.com"
PROCESSED_LABEL = "JobScout/Processed"
# Indeed alerts auto-archive out of INBOX, so search Gmail's All Mail.
ALL_MAIL = '"[Gmail]/All Mail"'
LOOKBACK_DAYS = 3             # how far back to scan for unprocessed alerts (daily run)
MAX_EMAILS_PER_RUN = 8        # cap emails processed per run so a backlog can't blow up Apify cost
APIFY_FETCH_ROWS_PER_QUERY = 20  # results per company search when fetching full JDs
# SOFT crawl ceiling for this path. memo23's maxJobs caps the CRAWL (maxRows only
# trims OUTPUT), but it is NOT a hard cap — it finishes each startUrl's current page
# batch, so it overshoots. Measured 08-02-2026: maxJobs=200 billed 400 items (100%
# over, not the ~25% previously assumed). Leaving it at the actor default (20000) +
# expandToCities default (true) is what let a 14-company run crawl 17,847 jobs =
# $24.81 on 07-25-2026, 85% of the monthly plan in ONE run.
#
# Because maxJobs is soft, it is NOT the money wall — APIFY_ALERT_MAX_RUN_USD is.
# Treat this as a hint that keeps normal runs small.
APIFY_ALERT_MAX_JOBS_CEILING = 200

# THE money wall for this path. Apify AUTO-ABORTS the run the instant its charges
# cross this number, so it is deterministic in a way no item cap is. Deliberately
# far tighter than config.APIFY_MAX_RUN_USD ($1.00, used by scraper.py's broader
# searches): enrichment is a nice-to-have that tops up alert-email stubs, so it
# gets a hard $0.05/day ceiling (~$1.50/mo) instead of an open-ended crawl.
#
# If the cap aborts the run we keep whatever partial dataset was written and stub
# the rest — bounded-cost enrichment, never an unbounded one. Raise this ONLY
# after checking the month's remaining headroom.
APIFY_ALERT_MAX_RUN_USD = 0.05

# Master switch for the Apify JD-enrichment crawl on this path.
#
# HISTORY (why this flag exists): from 06-30-2026 (the borderline -> memo23 actor
# swap) to 08-02-2026 this crawl was 100% waste. borderline returned the Indeed key
# as `jobKey`; memo23 returns it as `jobId`. The match below was never updated, so
# it hit zero every single day — verified live on run yYdF4oTzvi7ZjCzMl: 398/398
# items carried `jobId`, 0 carried `jobKey`. Every target fell through to the
# alert-email stub while the crawl still billed $0.285-0.563/day (~$8.50-17/mo).
# Nothing caught it for 33 days because every guard asked "did this cost too much?"
# and none asked "did this buy anything?" — see the yield check below, which is the
# actual fix for that class of bug.
APIFY_ALERT_ENRICH_ENABLED = True

# Yield guard. A paid run that returns zero usable rows is a BUG, not a quiet day.
# If a run costs more than this and maps nothing, we raise it as a hard ingest error
# so main.py emails about it, instead of degrading silently into stubs for a month.
APIFY_ALERT_MIN_COST_TO_ALERT_ON_ZERO_YIELD = 0.01

# Set when the last ingest hit a hard failure (vs a clean "no new jobs" run);
# main.py can read this to alert, mirroring scraper.LAST_SCRAPE_ERROR.
LAST_INGEST_ERROR = None

# Indeed alert links look like /rc/clk/dl?jk=<16 hex>&... — jk is the job_id.
_JK_RE = re.compile(r"[?&]jk=([0-9a-f]{16})", re.I)
# A bare job key on its own (what memo23 puts in `jobId`), used to sanity-check a
# field before trusting it as the key — so a renamed/wrong field reads as "no key"
# rather than silently matching nothing.
_JK_VALUE_RE = re.compile(r"[0-9a-f]{16}", re.I)
# Job anchor in the HTML part: <a href="...jk=KEY..." class="strong-text-link">TITLE</a>
_ANCHOR_RE = re.compile(
    r'<a\s+href="[^"]*?[?&]jk=([0-9a-f]{16})[^"]*?"[^>]*?>(.*?)</a>',
    re.I | re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _decode_parts(msg: Message):
    """Return (text_plain, text_html) of an email, MIME-decoded (handles QP/base64)."""
    plain, html = "", ""
    for part in msg.walk():
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            text = payload.decode("utf-8", errors="replace")
        if ct == "text/plain":
            plain += text + "\n"
        else:
            html += text + "\n"
    return plain, html


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def parse_alert(plain: str, html: str) -> list:
    """
    Extract [{jk, title, company}] from one alert email. Prefers the HTML anchors
    (ordered jk+title pairs); fills company from the cell that follows each anchor.
    Falls back to plaintext-only jk extraction if the HTML structure changes.
    """
    jobs = []
    seen = set()

    if html:
        anchors = list(_ANCHOR_RE.finditer(html))
        for i, m in enumerate(anchors):
            jk = m.group(1).lower()
            if jk in seen:
                continue
            seen.add(jk)
            title = _clean(_TAG_RE.sub("", m.group(2)))
            # company = first non-empty <td> after this anchor, before the next anchor
            start = m.end()
            end = anchors[i + 1].start() if i + 1 < len(anchors) else len(html)
            company = None
            for cell in re.finditer(r"<td[^>]*>(.*?)</td>", html[start:end], re.I | re.S):
                txt = _clean(_TAG_RE.sub("", cell.group(1)))
                if txt and not txt.startswith("$") and txt.lower() not in ("remote", "easily apply"):
                    company = txt
                    break
            jobs.append({"jk": jk, "title": title or None, "company": company})

    # Fallback / safety net: any jk in either part that the anchor pass missed.
    for m in _JK_RE.finditer(plain + "\n" + html):
        jk = m.group(1).lower()
        if jk not in seen:
            seen.add(jk)
            jobs.append({"jk": jk, "title": None, "company": None})

    return jobs


def _run_field(run, attr_name: str, dict_key: str):
    """
    Read one field off an Apify run. apify-client 3.x returns a Run model object,
    2.x returns a plain dict — scraper._run_actor already handles both, so mirror it
    here rather than assuming a shape and dying inside a broad `except`.
    """
    if run is None:
        return None
    val = getattr(run, attr_name, None)
    if val is None and isinstance(run, dict):
        val = run.get(dict_key)
    return val


def _item_job_key(item: dict) -> str:
    """
    Pull the Indeed job key (the 16-hex `jk`) out of a scraper item.

    THE bug this exists to prevent: the key's field name is actor-specific and has
    already moved once. borderline/indeed-scraper called it `jobKey`; the current
    memo23/apify-indeed-cheerio-ppr calls it `jobId` (verified live: 398/398 items
    carried `jobId`, 0 carried `jobKey`). Reading one hard-coded name silently
    matched nothing for 33 days. So try every known name, then fall back to parsing
    ?jk= out of the item URL, which is the one place Indeed itself always puts it.
    """
    for field in ("jobId", "jobKey", "jobkey", "id"):
        val = str(item.get(field) or "").strip().lower()
        if _JK_VALUE_RE.fullmatch(val):
            return val
    for field in ("url", "jobUrl", "applyUrl"):
        m = _JK_RE.search(str(item.get(field) or ""))
        if m:
            return m.group(1).lower()
    return ""


def _imap_search(M: imaplib.IMAP4_SSL) -> list:
    """UID-search recent unprocessed Indeed alert emails. Returns a list of UIDs (bytes)."""
    # Gmail search syntax via X-GM-RAW handles the processed-label exclusion cleanly.
    raw = f'from:{ALERT_FROM} newer_than:{LOOKBACK_DAYS}d -label:{PROCESSED_LABEL}'
    typ, data = M.uid("SEARCH", "X-GM-RAW", f'"{raw}"')
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def _fetch_full_jobs(targets: dict) -> list:
    """
    targets: {jk: {"title":..., "company":...}} for the NEW jobs.
    Run one Apify search (by company, falling back to title) and keep only the
    items whose Indeed job key is a target. Returns mapped job dicts (scraper schema).
    Any target not surfaced falls back to a minimal row built from the alert data
    so the job still enters the pipeline and gets scored on what we have.
    """
    if not targets:
        return []

    # Build de-duplicated search URLs (one per distinct company/title query).
    queries = {}
    for jk, info in targets.items():
        q = (info.get("company") or info.get("title") or "").strip()
        if not q:
            continue
        # cleaned, capped query for a tight result set
        q = re.sub(r"[^A-Za-z0-9 ]+", " ", q)
        q = " ".join(q.split()[:6])
        if q:
            queries.setdefault(q.lower(), q)

    mapped_by_jk = {}
    if queries and not APIFY_ALERT_ENRICH_ENABLED:
        logging.info(
            f"alert ingest: enrichment disabled (APIFY_ALERT_ENRICH_ENABLED=False) — "
            f"skipping Apify crawl of {len(queries)} company queries, using alert stubs."
        )
    elif queries:
        urls = [
            f"https://www.indeed.com/jobs?q={quote_plus(q)}&sort=date"
            for q in queries.values()
        ]
        try:
            client = _get_client()
            # COST MODEL (see the constants at the top of this file for the full history):
            #   - maxJobs is a SOFT cap. It overshoots (measured 200 -> 400 billed items).
            #     It keeps normal runs small; it is NOT the money wall.
            #   - max_total_charge_usd IS the money wall. Apify auto-aborts the run the
            #     instant charges cross it, so the ceiling is deterministic.
            #   - expandToCities MUST stay False. True is what turned a 14-company run into
            #     a 17,847-job / $24.81 crawl on 07-25-2026.
            max_jobs = min(APIFY_FETCH_ROWS_PER_QUERY * max(1, len(urls)), APIFY_ALERT_MAX_JOBS_CEILING)
            run = client.actor(config.APIFY_ACTOR_ID).call(run_input={
                "startUrls": [{"url": u} for u in urls],
                "maxJobs": max_jobs,               # soft crawl hint — never omit
                "expandToCities": False,           # never fan a search into per-city crawls
                "flattenOutput": True,
                "includeCompanyDetails": False,
                "strictMatch": False,
                "enableUniqueJobs": True,
                "includeSimilarJobs": False,
                "proxy": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
            }, max_total_charge_usd=Decimal(str(APIFY_ALERT_MAX_RUN_USD)))

            # An ABORTED run is the charge cap doing its job, not a failure: whatever the
            # crawl already wrote is still in the dataset and still useful. Read it either
            # way and let unmatched targets fall through to stubs below.
            dataset_id = _run_field(run, "default_dataset_id", "defaultDatasetId")
            run_id = _run_field(run, "id", "id")
            run_status = _run_field(run, "status", "status")
            run_cost = float(_run_field(run, "usage_total_usd", "usageTotalUsd") or 0.0)
            if run_status == "ABORTED":
                logging.warning(
                    f"alert ingest: run hit the ${APIFY_ALERT_MAX_RUN_USD} charge cap and "
                    f"aborted — keeping the partial dataset, stubbing the rest."
                )

            scanned = 0
            if dataset_id:
                for item in client.dataset(dataset_id).iterate_items():
                    scanned += 1
                    jk = _item_job_key(item)
                    if jk and jk in targets:
                        job = _map_apify_item(item)
                        if job:
                            mapped_by_jk[jk] = job

            logging.info(
                f"alert ingest: enrichment run {run_id} status={run_status} "
                f"cost=${run_cost:.4f} scanned={scanned} matched={len(mapped_by_jk)}/{len(targets)}"
            )

            # YIELD GUARD — the check that would have caught the 06-30 -> 08-02 waste on
            # day one. Cost guards only ask "was this too expensive?"; this asks "did it
            # buy anything?" A paid run that maps nothing is a bug (renamed field, changed
            # schema, dead selector), so surface it loudly instead of degrading to stubs.
            if run_cost > APIFY_ALERT_MIN_COST_TO_ALERT_ON_ZERO_YIELD and not mapped_by_jk:
                global LAST_INGEST_ERROR
                LAST_INGEST_ERROR = (
                    f"Apify enrichment billed ${run_cost:.4f} across {scanned} scanned items "
                    f"but matched 0 of {len(targets)} target jobs. That is a bug, not a slow "
                    f"day — the actor's key field has probably been renamed again (it went "
                    f"jobKey -> jobId in the 06-30-2026 borderline->memo23 swap, which cost "
                    f"33 days of silent waste). Check the item schema of run {run_id} "
                    f"before this runs again."
                )
                logging.error(f"alert ingest: {LAST_INGEST_ERROR}")
        except Exception as e:
            logging.error(f"alert ingest: Apify JD fetch failed: {e}")

    # Assemble results; minimal fallback for any target not surfaced by search.
    out = []
    for jk, info in targets.items():
        if jk in mapped_by_jk:
            out.append(mapped_by_jk[jk])
        else:
            logging.info(f"alert ingest: jk {jk} not surfaced by search; using alert stub.")
            out.append({
                "job_id": jk,
                "company": info.get("company"),
                "job_title": info.get("title") or "(title unavailable)",
                "location": None,
                "description": (info.get("title") or "") + " — sourced from Indeed job alert; "
                               "full description not retrieved.",
                "provider": "indeed_alert",
                "level": None,
                "job_type": None,
                "salary_min": None, "salary_max": None,
                "salary_interval": None, "salary_currency": None,
                "is_remote": None,
                "job_url_direct": f"https://www.indeed.com/viewjob?jk={jk}",
                "date_posted": None,
            })
    return out


def _label_processed(M: imaplib.IMAP4_SSL, uids: list) -> None:
    """Tag processed alert emails with the Gmail label so they're never re-parsed."""
    if not uids:
        return
    try:
        M.uid("STORE", b",".join(uids), "+X-GM-LABELS", f"({PROCESSED_LABEL})")
    except Exception as e:
        logging.warning(f"alert ingest: could not label processed emails: {e}")


def ingest_alert_jobs(dry_run: bool = False, fetch: bool = True) -> list:
    """
    Main entry point. Returns NEW jobs (scraper schema) for the pipeline to save.
    dry_run=True: no DB writes, no email labeling (still fetches unless fetch=False).
    fetch=False: skip the Apify JD fetch (cheap parse + dedup check only).
    """
    global LAST_INGEST_ERROR
    LAST_INGEST_ERROR = None

    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        logging.info("alert ingest: GMAIL_USER/GMAIL_APP_PASSWORD not set — skipping.")
        return []

    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST)
        M.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    except Exception as e:
        LAST_INGEST_ERROR = f"IMAP login failed: {e}"
        logging.error(LAST_INGEST_ERROR)
        return []

    try:
        M.select(ALL_MAIL)
        uids = _imap_search(M)
        logging.info(f"alert ingest: {len(uids)} unprocessed alert email(s) in the last {LOOKBACK_DAYS}d")
        if not uids:
            return []
        # Process the most recent N per run; the cap bounds Apify cost on a backlog.
        if len(uids) > MAX_EMAILS_PER_RUN:
            logging.info(f"alert ingest: capping to {MAX_EMAILS_PER_RUN} most-recent emails this run")
            uids = uids[-MAX_EMAILS_PER_RUN:]

        parsed = []   # [{jk, title, company}]
        seen_jk = set()
        for uid in uids:
            typ, data = M.uid("FETCH", uid, "(RFC822)")
            if typ != "OK" or not data or not data[0]:
                continue
            msg = email.message_from_bytes(data[0][1])
            plain, html = _decode_parts(msg)
            for job in parse_alert(plain, html):
                if job["jk"] not in seen_jk:
                    seen_jk.add(job["jk"])
                    parsed.append(job)
        logging.info(f"alert ingest: parsed {len(parsed)} distinct listings across emails")

        # Dedup against jobs already in Supabase (job_id == jk).
        existing_ids, _ = supabase_utils.get_existing_jobs_from_supabase()
        targets = {j["jk"]: j for j in parsed if j["jk"] not in existing_ids}
        logging.info(f"alert ingest: {len(targets)} NEW (not already in DB), "
                     f"{len(parsed) - len(targets)} already present")

        if not fetch:
            logging.info("alert ingest: fetch=False — skipping Apify JD fetch.")
            for jk, info in targets.items():
                logging.info(f"  NEW jk={jk} | {info.get('company')} | {(info.get('title') or '')[:60]}")
            return []

        new_jobs = _fetch_full_jobs(targets) if targets else []

        if dry_run:
            logging.info("alert ingest: DRY RUN — no DB writes, no email labeling.")
            for j in new_jobs:
                logging.info(f"  NEW: {j.get('job_id')} | {j.get('company')} | "
                             f"{(j.get('job_title') or '')[:60]} | desc={len(j.get('description') or '')}")
            return new_jobs

        _label_processed(M, uids)
        return new_jobs

    except Exception as e:
        LAST_INGEST_ERROR = f"ingest failed: {e}"
        logging.error(LAST_INGEST_ERROR, exc_info=True)
        return []
    finally:
        try:
            M.logout()
        except Exception:
            pass


if __name__ == "__main__":
    jobs = ingest_alert_jobs(dry_run=True)
    print(f"\n=== DRY RUN: {len(jobs)} new alert job(s) would be added ===")
    for j in jobs:
        print(f"  {j.get('job_id')} | {j.get('company')} | {(j.get('job_title') or '')[:60]} "
              f"| desc={len(j.get('description') or '')} | src={j.get('provider')}")
