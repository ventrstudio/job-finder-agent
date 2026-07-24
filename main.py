"""
Job Finder System — Main Orchestration Script.

Runs the full pipeline:
1. Scrape job boards for new listings
2. Save new jobs to Supabase
3. Score unscored jobs against the agent profile
4. Send email digest of top matches

Designed to run twice daily via Claude Code Routines (or n8n fallback).
"""

import logging
import os
import sys
import time

import httpx

import config
import cost_tracker
import supabase_utils
import scraper
import alert_ingest
import screen
from score_jobs import score_unscored_jobs
from send_digest import send_digest, send_alert

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def ping_heartbeat(success: bool = True):
    """
    Ping the external heartbeat monitor so it knows the pipeline ran.
    success=False hits the /fail endpoint so the monitor alerts right away
    instead of showing green for a run that exits non-zero. The rule:
    heartbeat success if and only if the process exits 0.
    Dormant until HEALTHCHECK_URL is set — a no-op otherwise, so this is
    safe to ship before the monitor account exists.
    """
    url = config.HEALTHCHECK_URL
    if not url:
        return
    if not success:
        url = url.rstrip("/") + "/fail"
    try:
        httpx.get(url, timeout=10)
        logging.info("Heartbeat ping sent." if success else "Heartbeat FAIL ping sent.")
    except Exception as e:
        logging.warning(f"Heartbeat ping failed: {e}")


def run_pipeline():
    """Execute the full job finder pipeline."""
    cost_tracker.reset()
    start_time = time.time()
    logging.info("=" * 60)
    logging.info("JOB FINDER PIPELINE — Starting")
    logging.info("=" * 60)

    skip_scrape = os.environ.get("SKIP_SCRAPE", "").lower() in ("1", "true", "yes")

    # Step 1: Scrape new jobs
    if skip_scrape:
        logging.info("\n--- STEP 1: SKIPPED (SKIP_SCRAPE=1) ---")
        new_jobs = []
    else:
        logging.info("\n--- STEP 1: Scraping job boards ---")
        new_jobs = scraper.scrape_all_queries()
        if scraper.LAST_SCRAPE_ERROR:
            logging.error(f"Scraper hit a hard failure: {scraper.LAST_SCRAPE_ERROR}")
            send_alert(
                "Job Scout: scraper failed",
                "A scrape source hit an error:\n\n"
                f"{scraper.LAST_SCRAPE_ERROR}\n\n"
                f"{len(new_jobs)} new jobs still came through from the surviving "
                "source(s). If this repeats, a fix is needed.",
            )

        # Step 1b: ingest Indeed job-alert emails (the cheap "breadth" layer —
        # catches exact-fit roles the narrow scrape queries miss). Deduped on
        # job_id, so it never re-fetches a job the scrape already pulled.
        logging.info("\n--- STEP 1b: Ingesting Indeed alert emails ---")
        try:
            alert_jobs = alert_ingest.ingest_alert_jobs()
            if alert_jobs:
                in_batch = {j["job_id"] for j in new_jobs}
                added = [j for j in alert_jobs if j["job_id"] not in in_batch]
                new_jobs = new_jobs + added
                logging.info(f"Alert ingestion added {len(added)} new jobs "
                             f"(deduped {len(alert_jobs) - len(added)} already in this run)")
            if alert_ingest.LAST_INGEST_ERROR:
                send_alert(
                    "Job Scout: alert ingest failed",
                    f"Indeed alert-email ingestion hit an error:\n\n{alert_ingest.LAST_INGEST_ERROR}",
                )
        except Exception as e:
            logging.error(f"Alert ingestion crashed (non-fatal): {e}", exc_info=True)

    # Step 2: Save new jobs to Supabase
    if new_jobs:
        logging.info(f"\n--- STEP 2: Saving {len(new_jobs)} new jobs to Supabase ---")
        supabase_utils.save_jobs_to_supabase(new_jobs)
    else:
        logging.info("\n--- STEP 2: No new jobs to save ---")

    # Step 2.5: Tier 1 legitimacy heuristics on every new job (free, no network).
    if config.SCREEN_ENABLED:
        logging.info("\n--- STEP 2.5: Screening new jobs for scam tells (Tier 1) ---")
        try:
            screen.run_heuristic_screen()
        except Exception as e:
            logging.error(f"Tier 1 screen crashed (non-fatal): {e}", exc_info=True)

    # Step 3: Score unscored jobs (allow override via SCORE_LIMIT env)
    score_limit_env = os.environ.get("SCORE_LIMIT", "").strip()
    score_limit = int(score_limit_env) if score_limit_env.isdigit() else None
    logging.info(f"\n--- STEP 3: Scoring unscored jobs (limit={score_limit or config.JOBS_TO_SCORE_PER_RUN}) ---")
    scored_jobs = score_unscored_jobs(limit=score_limit)

    # Step 3.5: Tier 2 reputation check on the digest-bound jobs only (cached + capped).
    if config.SCREEN_ENABLED and scored_jobs:
        logging.info("\n--- STEP 3.5: Reputation check on top matches (Tier 2) ---")
        try:
            scored_jobs = screen.enrich_with_reputation(scored_jobs)
        except Exception as e:
            logging.error(f"Tier 2 reputation check crashed (non-fatal): {e}", exc_info=True)

    # Step 4: Email digest (full batch) + one-line Telegram nudge
    logging.info("\n--- STEP 4: Sending email digest ---")
    if scored_jobs:
        send_digest(scored_jobs)
    else:
        logging.info("No scored jobs to send in digest.")

    # Summary
    elapsed = time.time() - start_time
    logging.info("\n" + "=" * 60)
    logging.info("JOB FINDER PIPELINE — Complete")
    logging.info(f"  New jobs scraped: {len(new_jobs)}")
    logging.info(f"  Jobs scored: {len(scored_jobs)}")
    logging.info(f"  Total time: {elapsed:.1f}s")
    logging.info(f"\n{cost_tracker.tracker.summary()}")
    logging.info("=" * 60)

    # Tell the heartbeat monitor how this run went. A scraper hard-failure
    # exits non-zero below, so it must ping /fail here — a plain success ping
    # would leave the monitor green while the Actions run shows red.
    ping_heartbeat(success=not scraper.LAST_SCRAPE_ERROR)


if __name__ == "__main__":
    # Validate required config
    missing = []
    if not config.OPENROUTER_API_KEY:
        missing.append("OPENROUTER_API_KEY")
    if not config.SUPABASE_URL:
        missing.append("SUPABASE_URL")
    if not config.SUPABASE_SERVICE_ROLE_KEY:
        missing.append("SUPABASE_SERVICE_ROLE_KEY")

    if missing:
        logging.error(f"Missing required environment variables: {', '.join(missing)}")
        logging.error("Set these in .env or your environment before running.")
        sys.exit(1)  # exit non-zero so a misconfig shows red, not fake green
    else:
        try:
            run_pipeline()
        except Exception as e:
            logging.error(f"Pipeline crashed: {e}", exc_info=True)
            send_alert(
                "Job Scout: pipeline crashed",
                f"The pipeline crashed with an uncaught error:\n\n{e}",
            )
            ping_heartbeat(success=False)  # alert the monitor now, not after the grace window
            raise  # surface as a failed GitHub Actions run, no more fake green

        # Scrape failed but the pipeline still finished its other steps and
        # already sent its alert email. Exit non-zero so the GitHub run also
        # shows red — visible even if the alert email never arrives.
        if scraper.LAST_SCRAPE_ERROR:
            logging.error("Exiting non-zero: the scraper failed earlier this run.")
            sys.exit(1)
