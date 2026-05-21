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
import time

import config
import cost_tracker
import supabase_utils
import scraper
from score_jobs import score_unscored_jobs
from send_digest import send_digest, send_alert

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


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
                "The scraper hit an error and pulled 0 new jobs:\n\n"
                f"{scraper.LAST_SCRAPE_ERROR}\n\n"
                "Nothing new entered the pipeline today. A fix is needed.",
            )

    # Step 2: Save new jobs to Supabase
    if new_jobs:
        logging.info(f"\n--- STEP 2: Saving {len(new_jobs)} new jobs to Supabase ---")
        supabase_utils.save_jobs_to_supabase(new_jobs)
    else:
        logging.info("\n--- STEP 2: No new jobs to save ---")

    # Step 3: Score unscored jobs (allow override via SCORE_LIMIT env)
    score_limit_env = os.environ.get("SCORE_LIMIT", "").strip()
    score_limit = int(score_limit_env) if score_limit_env.isdigit() else None
    logging.info(f"\n--- STEP 3: Scoring unscored jobs (limit={score_limit or config.JOBS_TO_SCORE_PER_RUN}) ---")
    scored_jobs = score_unscored_jobs(limit=score_limit)

    # Step 4: Send email digest
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


if __name__ == "__main__":
    # Validate required config
    missing = []
    if not config.ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not config.SUPABASE_URL:
        missing.append("SUPABASE_URL")
    if not config.SUPABASE_SERVICE_ROLE_KEY:
        missing.append("SUPABASE_SERVICE_ROLE_KEY")

    if missing:
        logging.error(f"Missing required environment variables: {', '.join(missing)}")
        logging.error("Set these in .env or your environment before running.")
    else:
        try:
            run_pipeline()
        except Exception as e:
            logging.error(f"Pipeline crashed: {e}", exc_info=True)
            send_alert(
                "Job Scout: pipeline crashed",
                f"The pipeline crashed with an uncaught error:\n\n{e}",
            )
            raise  # surface as a failed GitHub Actions run, no more fake green
