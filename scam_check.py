"""
Tier 1 legitimacy screen — cheap, deterministic, runs on every new job.

No network calls. Pattern-matches the listing text + comp fields for the
textual tells of a scam / make-money-online funnel / fake recruiter, and
returns a 0-100 risk score plus named flag codes. This is the fast first
gate; the expensive web-reputation check (reputation.py) only runs on jobs
that clear the fit threshold, so Tier 1 is what keeps that gate cheap.

Returns are stored on jobs.scam_risk_score / jobs.scam_risk_flags and shown
as warnings in the digest. The authoritative AVOID/LEGIT verdict comes from
Tier 2; Tier 1 is a signal, not a sentence.

    from scam_check import screen_job
    result = screen_job(job)   # {"scam_risk_score": int, "flags": [...], "reasoning": str}
"""

from __future__ import annotations

import re
from typing import Optional


# Each rule: (flag_code, weight, compiled pattern). Weights sum, capped at 100.
# Weights are tuned so one strong tell (e.g. pay-to-apply, wire-fraud) is enough
# to flag loudly, while soft signals only add up when several co-occur.
_RULES: list[tuple[str, int, re.Pattern]] = [
    # Strongest, near-certain fraud
    ("wire_or_check_fraud", 55, re.compile(
        r"\b(wire transfer|cashier'?s? check|money order|reship|re-?ship|"
        r"process payments on (our|the company'?s) behalf|"
        r"package (handling|forwarding)|payment processing agent)\b", re.I)),
    ("pay_to_apply_or_upfront", 50, re.compile(
        r"\b(registration fee|application fee|starter kit|enrollment fee|"
        r"pay a (small )?fee|upfront (cost|fee|payment|investment)|"
        r"deposit required|buy (our|the) (course|program|kit)|"
        r"investment of \$|one-?time fee)\b", re.I)),

    # Make-money-online / coaching-funnel tells (the AI Acquisition pattern)
    ("info_product_coaching_mill", 35, re.compile(
        r"\b(agency (incubator|accelerator|launchpad)|"
        r"make money online|passive income|financial freedom|be your own boss|"
        r"income challenge|our (members|students)|coaching (program|business)|"
        r"mentorship program|scale your (own )?agency|"
        r"\$\d{3,}\+? ?(/| per )?(hour|hr)|earn \$\d|"
        r"build a life-?changing business|brokering ai tools)\b", re.I)),
    ("crypto_mlm", 35, re.compile(
        r"\b(network marketing|multi-?level marketing|\bmlm\b|downline|"
        r"recruit (others|your|new members)|web3 trading|forex trading|"
        r"crypto trading|binary options)\b", re.I)),

    # Process / contact tells
    ("apply_off_platform", 28, re.compile(
        r"\b(apply (via|on|through) (telegram|whatsapp|signal)|"
        r"(message|text|contact) (us|me) on (telegram|whatsapp|signal)|"
        r"dm (us|me) on|add (us|me) on telegram)\b", re.I)),
    ("personal_email_contact", 22, re.compile(
        r"@(gmail|yahoo|hotmail|outlook|aol|proton(mail)?|icloud)\.com", re.I)),
    ("urgency_or_no_experience", 18, re.compile(
        r"\b(no experience (needed|necessary|required)|"
        r"hiring (immediately|asap|now, no)|immediate start|start (today|immediately)|"
        r"quick money|easy money|guaranteed (income|pay|salary|earnings))\b", re.I)),

    # Softer signals — only meaningful when stacked with others
    ("unpaid_trial_or_spec_build", 15, re.compile(
        r"\b(unpaid (trial|test|take-?home)|spec work|"
        r"build something (small )?(for us )?live|free (sample|trial) (project|build)|"
        r"complete (a|this) (project|task) (for us )?(before|to) (we )?(decide|hire))\b", re.I)),
    ("usd_global_arbitrage", 12, re.compile(
        r"\b(we pay for (the )?talent,? not (the|your) (postal code|location|zip)|"
        r"regardless of (where you|your) (live|location)|"
        r"strong usd (monthly )?rate|a strong usd|pay in usd regardless)\b", re.I)),
    ("contractor_dressed_as_job", 10, re.compile(
        r"\b(a paid engagement,? not (employment|a job)|"
        r"this is not (employment|a traditional job)|not a w-?2 (role|position))\b", re.I)),
]

# Generic / placeholder employer names — a real listing names the company.
_GENERIC_COMPANY = re.compile(
    r"^\s*(confidential|private|undisclosed|company confidential|"
    r"hiring company|recruiter|staffing|n/?a|nan|tbd|stealth)\s*$", re.I)

VERDICT_BANDS = {  # informational mapping of the heuristic score
    0: "low",
    35: "elevated",
    60: "high",
}


def _comp_flags(job: dict) -> list[tuple[str, int]]:
    """Comp-shaped tells that need the structured salary fields, not regex."""
    flags: list[tuple[str, int]] = []
    interval = (job.get("salary_interval") or "").lower()
    try:
        hi = float(job.get("salary_max") or job.get("salary_min") or 0)
    except (TypeError, ValueError):
        hi = 0.0

    # Implausibly high pay for the band is a classic bait. Monthly >= $9k or
    # yearly >= $400k with no seniority signal. Tunable; intentionally soft.
    if interval in ("monthly", "month") and hi >= 9000:
        flags.append(("too_good_to_be_true_comp", 18))
    elif interval in ("yearly", "year", "annual") and hi >= 400000:
        flags.append(("too_good_to_be_true_comp", 18))
    return flags


def screen_job(job: dict) -> dict:
    """
    Screen one job for scam tells. Pure function, no I/O.

    Returns:
        {"scam_risk_score": 0-100, "flags": [codes], "reasoning": "..."}
    """
    title = str(job.get("job_title") or "")
    company = str(job.get("company") or "")
    description = str(job.get("description") or "")
    blob = f"{title}\n{company}\n{description}"

    hits: list[tuple[str, int]] = []
    for code, weight, pattern in _RULES:
        if pattern.search(blob):
            hits.append((code, weight))

    if not company.strip() or _GENERIC_COMPANY.match(company):
        hits.append(("generic_or_missing_employer", 15))

    hits.extend(_comp_flags(job))

    # De-dup by code (keep highest weight) and sum, capped at 100.
    best: dict[str, int] = {}
    for code, weight in hits:
        best[code] = max(best.get(code, 0), weight)
    score = min(100, sum(best.values()))
    flags = sorted(best.keys(), key=lambda c: best[c], reverse=True)

    band = "low"
    for threshold in sorted(VERDICT_BANDS):
        if score >= threshold:
            band = VERDICT_BANDS[threshold]

    if flags:
        reasoning = f"Heuristic risk {score}/100 ({band}): " + ", ".join(flags)
    else:
        reasoning = "Heuristic risk 0/100: no scam tells in the listing text."

    return {"scam_risk_score": score, "flags": flags, "reasoning": reasoning}


if __name__ == "__main__":
    # Quick self-test against the AI Acquisition pattern + a clean listing.
    scam = {
        "job_title": "AI Systems Engineer - Forward-Deployed Builder",
        "company": "AI Acquisition",
        "salary_interval": "monthly",
        "salary_max": 22000,
        "description": (
            "This is a paid engagement, not employment. We pay for the talent "
            "not the postal code; a strong USD monthly rate goes a long way. "
            "Final step: we'll have you build something small live. We empower "
            "our members to earn $500+ per hour through our AI Agency Incubator."
        ),
    }
    clean = {
        "job_title": "Senior Automation Engineer",
        "company": "Zapier",
        "salary_interval": "yearly",
        "salary_max": 180000,
        "description": (
            "Build and maintain internal automation workflows. Remote, W-2, "
            "full benefits. You'll work with our platform team on n8n-style "
            "orchestration and API integrations."
        ),
    }
    for label, j in (("SCAM-LIKE", scam), ("CLEAN", clean)):
        r = screen_job(j)
        print(f"[{label}] {r['scam_risk_score']}/100 -> {r['flags']}")
