"""
Tier 2 legitimacy screen — company reputation via Firecrawl + LLM verdict.

Expensive (web search + an LLM call), so it runs ONLY on companies attached to
jobs that already cleared the fit threshold — the handful that would actually
reach Otis. Results are cached per company in public.company_reputation, so a
company is checked once and reused forever (and manual_override rows set by hand
are never auto-overwritten).

Flow per company:
  1. Cache hit that's fresh or hand-set  -> return it, no spend.
  2. Else Firecrawl-search "<company> reviews / scam / reddit / glassdoor",
     feed the snippets + the listing text to the LLM, get a verdict, cache it.

Verdict scale: LEGIT < CAUTION < SUSPICIOUS < AVOID  (UNKNOWN = couldn't tell).

    from reputation import check_company
    verdict = check_company("AI Acquisition", listing_text="...")
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import httpx

import config
import supabase_utils
from llm_client import generate

logger = logging.getLogger(__name__)

FIRECRAWL_SEARCH_URL = "https://api.firecrawl.dev/v1/search"

VERDICTS = ("LEGIT", "CAUTION", "SUSPICIOUS", "AVOID", "UNKNOWN")
# Ordering for demotion: higher = worse. UNKNOWN sits just above LEGIT so an
# unverifiable employer ranks below a confirmed-good one but above a flagged one.
_RANK = {"LEGIT": 0, "UNKNOWN": 1, "CAUTION": 2, "SUSPICIOUS": 3, "AVOID": 4}

_SUGGESTED_FLAGS = (
    "info_product_coaching_mill, mlm_or_recruiting_scheme, fake_or_unbranded_recruiter, "
    "contractor_pay_complaints, refund_denial, review_suppression, no_verifiable_product, "
    "no_named_team, no_real_funding, mass_evergreen_posting, upfront_cost_or_pay_to_apply, "
    "title_inflation_repricing, usd_global_arbitrage, ghosting_after_unpaid_work, "
    "lawsuit_or_regulatory, overwhelmingly_negative_reviews"
)

_JUDGE_SYSTEM = """You are a job-scam and employer-legitimacy analyst. Given a company name, the text of a job listing, and web search snippets (Reddit, Glassdoor, reviews, news), decide how much a careful job-seeker should trust this employer before investing real effort (e.g. building a custom application video).

Verdicts:
- LEGIT: real company, real product/team, no serious trust problems.
- CAUTION: probably real but with notable caveats (thin web presence, mixed reviews, contractor-not-employee with weak protections).
- SUSPICIOUS: multiple red flags or a pattern that often precedes wasted effort / non-payment.
- AVOID: make-money-online/coaching funnel, recruiting scheme, documented non-payment or fraud, or no verifiable existence.
- UNKNOWN: genuinely not enough signal to judge.

Weigh first-hand accounts (Reddit/Glassdoor/complaints) heavily. Treat placed PR or the company's own "is X a scam?" page skeptically. Absence of any verifiable product, team, or funding for a company that claims to be large is itself a flag. Be decisive but honest; use UNKNOWN only when evidence is truly absent.

Return ONLY JSON:
{
  "verdict": "LEGIT|CAUTION|SUSPICIOUS|AVOID|UNKNOWN",
  "summary": "<one or two plain sentences a job-seeker can act on>",
  "flags": ["<short_snake_case_code>", ...],
  "confidence": "low|medium|high"
}"""


def normalize_company(name: str) -> str:
    """Lowercased, punctuation-collapsed key. Matches the cache primary key."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def verdict_rank(verdict: Optional[str]) -> int:
    """Higher = worse. Unknown/None ranks just above LEGIT."""
    return _RANK.get((verdict or "UNKNOWN").upper(), 1)


def is_blocking(verdict: Optional[str]) -> bool:
    """True for the verdict we demote + warn loudly on."""
    return (verdict or "").upper() == "AVOID"


def _firecrawl_search(query: str, limit: int) -> list[dict]:
    """Run one Firecrawl web search. Returns [{title,url,snippet}]. Never raises."""
    if not config.FIRECRAWL_API_KEY:
        logger.warning("FIRECRAWL_API_KEY not set — skipping web reputation search.")
        return []
    try:
        resp = httpx.post(
            FIRECRAWL_SEARCH_URL,
            headers={
                "Authorization": f"Bearer {config.FIRECRAWL_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"query": query, "limit": limit},
            timeout=45,
        )
        resp.raise_for_status()
        data = resp.json().get("data", []) or []
        out = []
        for item in data:
            out.append({
                "title": (item.get("title") or "")[:200],
                "url": item.get("url") or "",
                "snippet": (item.get("description") or item.get("markdown") or "")[:600],
            })
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Firecrawl search failed for {query!r}: {e}")
        return []


def _gather_evidence(company: str) -> list[dict]:
    """Search the angles that surface employer trust signals. Cheap: few calls."""
    queries = [
        f'"{company}" reviews scam complaints reddit glassdoor',
    ]
    if config.LEGITIMACY_EXTRA_SEARCH:
        queries.append(f'"{company}" company official site careers funding')

    results: list[dict] = []
    seen_urls = set()
    for q in queries:
        for r in _firecrawl_search(q, config.LEGITIMACY_SEARCH_LIMIT):
            if r["url"] and r["url"] in seen_urls:
                continue
            seen_urls.add(r["url"])
            results.append(r)
    return results


def _judge(company: str, listing_text: str, evidence: list[dict]) -> dict:
    """LLM verdict over the listing + search snippets. Returns a verdict dict."""
    if evidence:
        ev_block = "\n".join(
            f"- {e['title']} ({e['url']})\n  {e['snippet']}" for e in evidence
        )
    else:
        ev_block = "(No web search results found. A real, active employer usually leaves some trace.)"

    prompt = f"""## COMPANY
{company}

## JOB LISTING TEXT (what they wrote about themselves)
{(listing_text or 'Not provided')[:2500]}

## WEB SEARCH SNIPPETS
{ev_block}

## SUGGESTED FLAG CODES (use these where they fit; add others as needed)
{_SUGGESTED_FLAGS}

Judge this employer. Return only the JSON object."""

    try:
        raw = generate(
            prompt=prompt,
            system_prompt=_JUDGE_SYSTEM,
            temperature=0,
            max_tokens=500,
            model=config.LEGITIMACY_MODEL,
            response_format={"type": "json_object"},
            source="reputation_check",
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Reputation LLM call failed for {company!r}: {e}")
        return {"verdict": "UNKNOWN", "summary": "Reputation check failed to run.",
                "flags": [], "confidence": "low"}

    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3].strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return {"verdict": "UNKNOWN", "summary": "Could not parse reputation verdict.",
                    "flags": [], "confidence": "low"}
        result = json.loads(m.group(0))

    verdict = str(result.get("verdict", "UNKNOWN")).upper()
    if verdict not in VERDICTS:
        verdict = "UNKNOWN"
    return {
        "verdict": verdict,
        "summary": str(result.get("summary", "")).strip(),
        "flags": [str(f) for f in (result.get("flags") or [])][:8],
        "confidence": str(result.get("confidence", "low")).lower(),
    }


def check_company(company: str, listing_text: str = "", force: bool = False) -> dict:
    """
    Resolve a company's legitimacy verdict, using the cache when possible.

    Returns:
        {"verdict", "summary", "flags", "sources", "from_cache", "normalized"}
    """
    normalized = normalize_company(company)
    if not normalized:
        return {"verdict": "UNKNOWN", "summary": "No company name to check.",
                "flags": [], "sources": [], "from_cache": False, "normalized": ""}

    cached = supabase_utils.get_company_reputation(normalized)
    if cached and not force:
        # Hand-set verdicts are authoritative; auto verdicts are reused while fresh.
        if cached.get("manual_override") or supabase_utils.reputation_is_fresh(
            cached.get("checked_at"), config.LEGITIMACY_CACHE_DAYS
        ):
            return {
                "verdict": cached.get("verdict", "UNKNOWN"),
                "summary": cached.get("summary", ""),
                "flags": cached.get("flags") or [],
                "sources": cached.get("sources") or [],
                "from_cache": True,
                "normalized": normalized,
            }

    # Never auto-overwrite a hand-set verdict.
    if cached and cached.get("manual_override"):
        return {
            "verdict": cached.get("verdict", "UNKNOWN"),
            "summary": cached.get("summary", ""),
            "flags": cached.get("flags") or [],
            "sources": cached.get("sources") or [],
            "from_cache": True,
            "normalized": normalized,
        }

    evidence = _gather_evidence(company)
    judged = _judge(company, listing_text, evidence)
    sources = [{"title": e["title"], "url": e["url"]} for e in evidence if e["url"]][:6]

    supabase_utils.upsert_company_reputation(
        normalized_name=normalized,
        display_name=company,
        verdict=judged["verdict"],
        summary=judged["summary"],
        flags=judged["flags"],
        sources=sources,
    )

    return {
        "verdict": judged["verdict"],
        "summary": judged["summary"],
        "flags": judged["flags"],
        "sources": sources,
        "from_cache": False,
        "normalized": normalized,
    }
