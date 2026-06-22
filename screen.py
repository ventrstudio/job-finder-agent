"""
Legitimacy screen orchestration — ties Tier 1 + Tier 2 into the pipeline.

run_heuristic_screen()      -> Tier 1 on every new (unscreened) job. Free.
enrich_with_reputation()    -> Tier 2 on the digest-bound jobs only. Cheap +
                               capped: a web/LLM check per *new* company, cache
                               hits are free, hard cap per run.

An AVOID verdict is attached to the job dict so the digest demotes + warns. It
never deletes a job — Otis keeps the final call (false-positive safety).
"""

from __future__ import annotations

import logging

import config
import scam_check
import reputation
import supabase_utils


def run_heuristic_screen(limit: int = 500) -> int:
    """Tier 1: screen every unscreened active job. Returns how many were flagged."""
    jobs = supabase_utils.get_unscreened_jobs(limit)
    if not jobs:
        logging.info("Tier 1 screen: no unscreened jobs.")
        return 0

    flagged = 0
    for job in jobs:
        result = scam_check.screen_job(job)
        supabase_utils.update_job_screen(
            job["job_id"], result["scam_risk_score"], result["flags"]
        )
        if result["scam_risk_score"] >= config.SCAM_RISK_WARN_THRESHOLD:
            flagged += 1
            logging.info(
                f"  Tier1 flag {result['scam_risk_score']}/100: "
                f"{job.get('job_title')} @ {job.get('company')} -> {result['flags']}"
            )
    logging.info(f"Tier 1 screen: {len(jobs)} screened, {flagged} above warn threshold.")
    return flagged


def _needs_fresh_check(cached: dict | None) -> bool:
    if not cached:
        return True
    if cached.get("manual_override"):
        return False
    return not supabase_utils.reputation_is_fresh(
        cached.get("checked_at"), config.LEGITIMACY_CACHE_DAYS
    )


def enrich_with_reputation(scored_jobs: list) -> list:
    """
    Tier 2: attach a company legitimacy verdict to each digest-bound job
    (score >= threshold), using the cache and a hard per-run cap on fresh checks.
    Mutates + returns scored_jobs.
    """
    matches = [
        j for j in scored_jobs
        if int(j.get("score", 0) or 0) >= config.SCORING_THRESHOLD
    ]
    if not matches:
        return scored_jobs

    # One representative (display name + listing text) per distinct company.
    companies: dict[str, dict] = {}
    for j in matches:
        norm = reputation.normalize_company(str(j.get("company") or ""))
        if not norm:
            continue
        companies.setdefault(norm, {
            "display": str(j.get("company") or ""),
            "listing": str(j.get("description") or "")[:2500],
        })

    # Decide which companies need a paid fresh check, honoring the run cap.
    verdicts: dict[str, dict] = {}
    fresh_budget = config.LEGITIMACY_MAX_COMPANIES_PER_RUN
    for norm, info in companies.items():
        cached = supabase_utils.get_company_reputation(norm)
        if not _needs_fresh_check(cached):
            verdicts[norm] = {
                "verdict": cached.get("verdict", "UNKNOWN"),
                "summary": cached.get("summary", ""),
                "flags": cached.get("flags") or [],
                "sources": cached.get("sources") or [],
            }
            continue
        if fresh_budget <= 0:
            # Cap hit — fall back to stale cache or UNKNOWN, no spend. Logged so
            # a capped run never reads as "everything checked clean".
            logging.warning(
                f"Tier 2 cap ({config.LEGITIMACY_MAX_COMPANIES_PER_RUN}) reached — "
                f"skipping fresh reputation check for {info['display']!r}."
            )
            verdicts[norm] = {
                "verdict": cached.get("verdict", "UNKNOWN") if cached else "UNKNOWN",
                "summary": cached.get("summary", "") if cached else "Not checked (run cap reached).",
                "flags": cached.get("flags") or [] if cached else [],
                "sources": cached.get("sources") or [] if cached else [],
            }
            continue
        res = reputation.check_company(info["display"], listing_text=info["listing"])
        fresh_budget -= 1
        verdicts[norm] = res
        logging.info(
            f"  Tier2 {res['verdict']}: {info['display']} "
            f"({'cache' if res.get('from_cache') else 'fresh'}) — {res.get('summary','')[:90]}"
        )

    # Attach to every match + persist on the job row, plus Tier 1 flags for display.
    for j in matches:
        norm = reputation.normalize_company(str(j.get("company") or ""))
        v = verdicts.get(norm, {"verdict": "UNKNOWN", "summary": "", "flags": [], "sources": []})
        heur = scam_check.screen_job(j)
        j["legitimacy_verdict"] = v["verdict"]
        j["legitimacy_summary"] = v["summary"]
        j["legitimacy_flags"] = v["flags"]
        j["scam_risk_score"] = heur["scam_risk_score"]
        j["scam_risk_flags"] = heur["flags"]
        supabase_utils.update_job_legitimacy(
            j["job_id"], v["verdict"], v["summary"], v["flags"], v.get("sources") or []
        )

    return scored_jobs
