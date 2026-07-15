"""
Job scraper using Apify actors: Indeed (memo23/apify-indeed-cheerio-ppr) +
LinkedIn (curious_coder/linkedin-jobs-scraper, added 07-14-2026).

Both sources scrape nationwide remote roles and on-site/hybrid roles near the
agent's home base, across part-time, contract, and full-time job types.
Replaces JobSpy (which got blocked from datacenter IPs).

One actor run per source per pipeline; all queries packed into each run's
urls input. Per-source mappers translate output shapes to our job schema.

Actor swapped off borderline/indeed-scraper on 06-30-2026 (cost: borderline
~$26/mo vs memo23 ~$3/mo on the free tier). LinkedIn actor is pay-per-result
($0.001/job, no cookie — never touches a real LinkedIn account).
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


def _extract_level(item: dict) -> str:
    """
    Best-effort seniority level from memo23 output. memo23 has no clean level
    field (borderline had an attributes array), so we sniff the title + the
    requirements/skills blob. Returns None when nothing matches — level is a
    nice-to-have for scoring, not a hard requirement.
    """
    blob = " ".join(
        str(item.get(k) or "")
        for k in ("positionName", "normTitle", "requirements")
    ).lower()
    if not blob.strip():
        return None
    if "senior" in blob or "sr." in blob or "staff " in blob or "principal" in blob:
        return "senior"
    if "entry level" in blob or "entry-level" in blob or "junior" in blob or "jr." in blob:
        return "entry"
    if "mid level" in blob or "mid-level" in blob:
        return "mid"
    return None


def _map_apify_item(item: dict) -> dict:
    """Translate memo23/apify-indeed-cheerio-ppr output → our job schema."""
    job_key = item.get("jobId")
    title = item.get("positionName")
    company = item.get("company")
    description = item.get("jobDescription") or ""

    if not title or not job_key:
        return None

    job_id = str(job_key)

    # memo23 jobType is a comma-joined string e.g. "Full-time, Contract".
    # Normalize the first type to borderline's style: "full-time" -> "fulltime".
    raw_type = item.get("jobType") or ""
    first_type = raw_type.split(",")[0].strip().lower() if raw_type else ""
    job_type = first_type.replace("-", "").replace(" ", "") or None

    remote_val = item.get("remote")
    is_remote = bool(remote_val) if remote_val is not None else False
    location_str = item.get("location") or item.get("fullAddress")

    salary_min = item.get("salaryMin")
    salary_max = item.get("salaryMax")
    salary_interval = (item.get("salaryType") or "").lower() or None
    salary_currency = item.get("currency")

    level = _extract_level(item)
    job_url = item.get("jobUrl") or item.get("url")
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


def _map_linkedin_item(item: dict) -> dict:
    """
    Translate curious_coder/linkedin-jobs-scraper output → our job schema.
    Shape verified on a live run 07-14-2026: id, link, title, companyName,
    location, postedAt (YYYY-MM-DD), descriptionText, seniorityLevel,
    employmentType, salary (display string, usually empty), inputUrl.
    """
    job_key = item.get("id")
    title = item.get("title")
    company = item.get("companyName")
    description = item.get("descriptionText") or ""

    if not title or not job_key:
        return None

    raw_type = item.get("employmentType") or ""
    job_type = raw_type.strip().lower().replace("-", "").replace(" ", "") or None

    # Guest search results carry no remote flag. The remote-scoped search URLs
    # embed f_WT=2, and inputUrl tells us which search produced the item.
    input_url = item.get("inputUrl") or ""
    blob = f"{title} {item.get('location') or ''}".lower()
    is_remote = "f_WT=2" in input_url or "remote" in blob

    seniority = (item.get("seniorityLevel") or "").lower()
    if any(s in seniority for s in ("senior", "director", "executive")):
        level = "senior"
    elif any(s in seniority for s in ("entry", "internship")):
        level = "entry"
    elif any(s in seniority for s in ("mid", "associate")):
        level = "mid"
    else:
        level = None

    # link carries refId/trackingId params — strip them so the URL is stable.
    job_url = (item.get("link") or "").split("?")[0] or None

    return {
        # Prefix guards against ID collision with Indeed's job key space.
        "job_id": f"li-{job_key}",
        "company": company,
        "job_title": title,
        "location": item.get("location"),
        "description": description,
        "provider": "linkedin",
        "level": level,
        "job_type": job_type,
        # salary is a display string ("$100,000.00/yr - ...") too inconsistent
        # to parse reliably; scoring reads the description text anyway.
        "salary_min": None,
        "salary_max": None,
        "salary_interval": None,
        "salary_currency": None,
        "is_remote": is_remote,
        "job_url_direct": job_url,
        "date_posted": item.get("postedAt"),
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


def _build_linkedin_url(query: str, *, remote: bool) -> str:
    """
    Build a LinkedIn public (guest) jobs search URL. Mirrors the Indeed scope
    split: remote=True -> nationwide remote-only (f_WT=2), remote=False ->
    location-bound near home base. f_TPR=r86400 = last 24h (same window as
    APIFY_FROM_DAYS=1), sortBy=DD = newest first.
    """
    q = quote_plus(query)
    jt = quote_plus(",".join(config.LINKEDIN_JOB_TYPES))
    base = (
        f"https://www.linkedin.com/jobs/search/?keywords={q}"
        f"&f_TPR=r86400"
        f"&f_JT={jt}"
        f"&sortBy=DD"
    )
    if remote:
        return base + "&f_WT=2&location=" + quote_plus("United States")
    return (
        base
        + "&location=" + quote_plus(config.LINKEDIN_LOCAL_LOCATION)
        + f"&distance={config.APIFY_LOCAL_RADIUS}"
    )


def _run_actor(actor_id: str, run_input: dict, source_name: str) -> list:
    """
    Call one Apify actor and drain its default dataset. Returns raw items;
    on failure logs, appends to LAST_SCRAPE_ERROR, and returns [] so the
    other source's results still flow.
    """
    global LAST_SCRAPE_ERROR
    client = _get_client()
    raw_items = []
    try:
        run = client.actor(actor_id).call(run_input=run_input)
        # apify-client 3.x returns a Run model object; 2.x returns a dict.
        dataset_id = getattr(run, "default_dataset_id", None) or (
            run.get("defaultDatasetId") if isinstance(run, dict) else None
        )
        if dataset_id:
            for item in client.dataset(dataset_id).iterate_items():
                raw_items.append(item)
        logging.info(f"[{source_name}] actor returned {len(raw_items)} raw items")
    except Exception as e:
        err = f"[{source_name}] {e}"
        LAST_SCRAPE_ERROR = f"{LAST_SCRAPE_ERROR}; {err}" if LAST_SCRAPE_ERROR else err
        logging.error(f"Apify scrape failed: {err}")
    return raw_items


def scrape_all_queries() -> list:
    """
    One Apify run per source (Indeed memo23 + LinkedIn guest search), all
    queries packed as urls[]. Dedupe against DB + within run (cross-source:
    the company|title key catches the same role posted on both boards).
    A single source failing alerts (LAST_SCRAPE_ERROR) but does not drop the
    other source's results.
    """
    global LAST_SCRAPE_ERROR
    LAST_SCRAPE_ERROR = None

    logging.info("--- Starting Job Scraping (Apify: Indeed + LinkedIn) ---")

    existing_ids, existing_company_title_pairs = supabase_utils.get_existing_jobs_from_supabase()
    logging.info(f"Found {len(existing_ids)} existing jobs in database")

    remote_urls = [_build_indeed_url(q, remote=True) for q in config.SEARCH_QUERIES]
    local_urls = [_build_indeed_url(q, remote=False) for q in config.SEARCH_QUERIES]
    urls = remote_urls + local_urls
    logging.info(
        f"Built {len(urls)} Indeed URLs for one actor run "
        f"({len(remote_urls)} remote + {len(local_urls)} local near {config.APIFY_LOCAL_LOCATION})"
    )

    # memo23 schema: startUrls is an array of {url} objects. maxJobs caps the
    # crawl (the cost dial). expandToCities=False stops it fanning a country/region
    # URL into per-city crawls (our URLs are already scoped). flattenOutput gives
    # us a flat dict per job (the shape _map_apify_item expects). RESIDENTIAL proxy
    # is what gets past Indeed's bot wall.
    indeed_input = {
        "startUrls": [{"url": u} for u in urls],
        "maxJobs": config.APIFY_MAX_ROWS_GLOBAL,
        "expandToCities": False,
        "flattenOutput": True,
        "includeCompanyDetails": False,
        "strictMatch": False,
        "proxy": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
    }
    # (source_name, mapper, raw_items) per source. Indeed first — it's the
    # richer source (salary fields), so on a cross-source duplicate the Indeed
    # record wins the dedup race.
    sources = [
        ("indeed", _map_apify_item, _run_actor(config.APIFY_ACTOR_ID, indeed_input, "indeed")),
    ]

    if config.LINKEDIN_ENABLED:
        li_urls = [_build_linkedin_url(q, remote=True) for q in config.SEARCH_QUERIES] + [
            _build_linkedin_url(q, remote=False) for q in config.SEARCH_QUERIES
        ]
        logging.info(f"Built {len(li_urls)} LinkedIn URLs for one actor run")
        linkedin_input = {
            "urls": li_urls,
            "count": config.LINKEDIN_MAX_ROWS,
            "scrapeCompany": False,
        }
        sources.append(
            ("linkedin", _map_linkedin_item, _run_actor(config.LINKEDIN_ACTOR_ID, linkedin_input, "linkedin"))
        )

    all_new_jobs = []
    seen_ids = set()
    seen_company_title = set()

    blocklist = [b.lower() for b in config.COMPANY_BLOCKLIST]

    raw_items = [(mapper, item) for _, mapper, items in sources for item in items]

    for mapper, item in raw_items:
        job = mapper(item)
        if not job:
            continue

        company_l = (job.get("company") or "").lower()
        if company_l and any(b in company_l for b in blocklist):
            continue

        job_id = job["job_id"]
        if job_id in existing_ids or job_id in seen_ids:
            continue

        if job.get("company") and job.get("job_title"):
            key = supabase_utils.normalize_key(job["company"], job["job_title"])
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
