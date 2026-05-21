"""
Job scraper using Apify borderline/indeed-scraper actor.

Scrapes Indeed for both nationwide remote roles and on-site/hybrid roles near
the agent's home base, across part-time, contract, and full-time job types.
Replaces JobSpy (which got blocked from datacenter IPs).

Single actor run per pipeline. All queries packed into urls[] input.
"""

import logging
import hashlib
from urllib.parse import quote_plus

from apify_client import ApifyClient

import config
import supabase_utils

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

_apify_client = None

# Set to an error string when the last scrape hit a hard failure (vs a genuine
# "no new jobs" day). main.py reads this to fire a failure alert email.
LAST_SCRAPE_ERROR = None


def _get_client() -> ApifyClient:
    global _apify_client
    if _apify_client is None:
        if not config.APIFY_TOKEN:
            raise ValueError("APIFY_TOKEN not set in environment")
        _apify_client = ApifyClient(config.APIFY_TOKEN)
    return _apify_client


def _make_dedup_key(title: str, company: str) -> str:
    """Create a normalized deduplication key from title + company."""
    normalized = f"{(title or '').strip().lower()}|{(company or '').strip().lower()}"
    return hashlib.md5(normalized.encode()).hexdigest()


def _extract_level(attributes: list) -> str:
    """Pull seniority level out of borderline's attributes array."""
    if not attributes:
        return None
    level_map = {
        "entry level": "entry",
        "mid level": "mid",
        "senior level": "senior",
    }
    for attr in attributes:
        key = str(attr).strip().lower()
        if key in level_map:
            return level_map[key]
    return None


def _format_location(loc: dict) -> str:
    """Render raw location from borderline output (HQ city/state)."""
    if not loc:
        return None
    return (
        loc.get("formattedAddressShort")
        or loc.get("fullAddress")
        or loc.get("formattedAddressLong")
    )


def _map_apify_item(item: dict) -> dict:
    """Translate borderline output → our job schema."""
    job_key = item.get("jobKey")
    title = item.get("title")
    company = item.get("companyName")
    description = item.get("descriptionText") or ""

    if not title or not job_key:
        return None

    job_id = str(job_key)

    job_type_arr = item.get("jobType") or []
    job_type = job_type_arr[0].lower() if job_type_arr else None

    is_remote = bool(item.get("isRemote"))
    location_str = _format_location(item.get("location") or {})

    salary = item.get("salary") or {}
    salary_min = salary.get("min") or salary.get("salaryMin")
    salary_max = salary.get("max") or salary.get("salaryMax")
    salary_interval = salary.get("unit") or salary.get("salaryType")
    salary_currency = salary.get("currency") or salary.get("salaryCurrency")

    level = _extract_level(item.get("attributes") or [])
    job_url = item.get("jobUrl")
    date_posted = item.get("datePublished")

    return {
        "job_id": job_id,
        "company": company,
        "job_title": title,
        "location": location_str,
        "description": description,
        "provider": "indeed",
        "level": level,
        "job_type": job_type,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_interval": salary_interval,
        "salary_currency": salary_currency,
        "is_remote": is_remote,
        "job_url_direct": job_url,
        "date_posted": date_posted,
    }


def _build_indeed_url(query: str, *, remote: bool) -> str:
    """
    Build an Indeed search URL with multi-jobType filter and a 24hr filter.
    Indeed's compound filter syntax `sc=0kf:jt(parttime),jt(contract);` accepts
    multiple job types in one URL.

    remote=True  -> nationwide remote-only listings (Indeed remotejob filter).
    remote=False -> on-site/hybrid listings near the local base (l + radius).
    """
    q = quote_plus(query)
    types = ",".join(f"jt({t})" for t in config.APIFY_JOB_TYPES)
    sc = quote_plus(f"0kf:{types};")
    base = (
        f"https://www.indeed.com/jobs?q={q}"
        f"&sc={sc}"
        f"&fromage={config.APIFY_FROM_DAYS}"
        f"&sort={config.APIFY_SORT}"
    )
    if remote:
        return base + "&remotejob=032b3046-06a3-4876-8dfd-474eb5e7ed11"
    loc = quote_plus(config.APIFY_LOCAL_LOCATION)
    return base + f"&l={loc}&radius={config.APIFY_LOCAL_RADIUS}"


def scrape_all_queries() -> list:
    """
    Single Apify run, all queries packed as urls[]. Dedupe against DB + within run.
    Hard cap via APIFY_MAX_ROWS_GLOBAL.
    """
    global LAST_SCRAPE_ERROR
    LAST_SCRAPE_ERROR = None

    logging.info("--- Starting Job Scraping (Apify borderline, single run) ---")

    existing_ids, existing_company_title_pairs = supabase_utils.get_existing_jobs_from_supabase()
    logging.info(f"Found {len(existing_ids)} existing jobs in database")

    client = _get_client()
    remote_urls = [_build_indeed_url(q, remote=True) for q in config.SEARCH_QUERIES]
    local_urls = [_build_indeed_url(q, remote=False) for q in config.SEARCH_QUERIES]
    urls = remote_urls + local_urls
    logging.info(
        f"Built {len(urls)} Indeed URLs for one actor run "
        f"({len(remote_urls)} remote + {len(local_urls)} local near {config.APIFY_LOCAL_LOCATION})"
    )

    run_input = {
        "urls": urls,
        "country": config.APIFY_COUNTRY,
        "maxRowsPerUrl": config.APIFY_MAX_ROWS_PER_QUERY,
        "maxRows": config.APIFY_MAX_ROWS_GLOBAL,
        "enableUniqueJobs": True,
        "includeSimilarJobs": False,
    }

    raw_items = []
    try:
        run = client.actor(config.APIFY_ACTOR_ID).call(run_input=run_input)
        # apify-client 3.x returns a Run model object, not a dict.
        # The dataset id is the `default_dataset_id` attribute.
        dataset_id = run.default_dataset_id if run else None
        if dataset_id:
            for item in client.dataset(dataset_id).iterate_items():
                raw_items.append(item)
        logging.info(f"Actor returned {len(raw_items)} raw items")
    except Exception as e:
        LAST_SCRAPE_ERROR = str(e)
        logging.error(f"Apify scrape failed: {e}")
        return []

    all_new_jobs = []
    seen_ids = set()
    seen_company_title = set()

    blocklist = [b.lower() for b in config.COMPANY_BLOCKLIST]

    for item in raw_items:
        job = _map_apify_item(item)
        if not job:
            continue

        company_l = (job.get("company") or "").lower()
        if company_l and any(b in company_l for b in blocklist):
            continue

        job_id = job["job_id"]
        if job_id in existing_ids or job_id in seen_ids:
            continue

        if job.get("company") and job.get("job_title"):
            key = (
                job["company"].strip().lower(),
                job["job_title"].strip().lower(),
            )
            if key in existing_company_title_pairs or key in seen_company_title:
                continue
            seen_company_title.add(key)

        desc = job.get("description") or ""
        if len(desc) < 50:
            continue

        seen_ids.add(job_id)
        all_new_jobs.append(job)

    logging.info(f"--- Scraping complete: {len(all_new_jobs)} new unique jobs ---")
    return all_new_jobs


if __name__ == "__main__":
    new_jobs = scrape_all_queries()
    if new_jobs:
        logging.info(f"Saving {len(new_jobs)} new jobs to Supabase...")
        supabase_utils.save_jobs_to_supabase(new_jobs)
    else:
        logging.info("No new jobs to save.")
