"""
Telegram delivery for the job digest.

Replaces the old EmailIt digest. Posts the top-scored jobs from the current run
to a Telegram chat: one header message, then one message per job with a
"View listing" link button. URL buttons need no webhook, so this works standalone.

Interactive Apply/Skip buttons + chat queries are handled separately by the
Supabase edge function `telegram-bot` (the webhook side).

Requires two env vars (set as GitHub Actions secrets):
  TELEGRAM_BOT_TOKEN  - from @BotFather
  TELEGRAM_CHAT_ID    - your numeric chat id (the bot must have messaged you once)
"""

from __future__ import annotations

import html
import logging
import time

import httpx

import config

API_BASE = "https://api.telegram.org/bot{token}/{method}"
_MAX_LEN = 4000  # Telegram hard limit is 4096; leave headroom

# Tier 2 legitimacy verdict display. AVOID is demoted + warned, never hidden.
_VERDICT_BADGE = {
    "AVOID": "\U0001F6D1 AVOID",
    "SUSPICIOUS": "⚠️ SUSPICIOUS",
    "CAUTION": "⚠️ CAUTION",
    "LEGIT": "✅ Legit",
    "UNKNOWN": "❔ Unverified",
}


def _verdict_of(job: dict) -> str:
    return str(job.get("legitimacy_verdict") or "").upper()


def _is_avoid(job: dict) -> bool:
    return _verdict_of(job) == "AVOID"


def _enabled() -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logging.error(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set. Cannot send Telegram digest."
        )
        return False
    return True


def _post(method: str, payload: dict) -> bool:
    url = API_BASE.format(token=config.TELEGRAM_BOT_TOKEN, method=method)
    try:
        r = httpx.post(url, json=payload, timeout=20)
        if r.status_code == 200 and r.json().get("ok"):
            return True
        logging.error(f"Telegram {method} error: {r.status_code} -- {r.text[:200]}")
        return False
    except Exception as e:  # noqa: BLE001
        logging.error(f"Telegram {method} request failed: {e}")
        return False


def _send_message(text: str, buttons: list | None = None) -> bool:
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text[:_MAX_LEN],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return _post("sendMessage", payload)


def _score_emoji(score: int) -> str:
    if score >= 8:
        return "\U0001F7E2"  # green
    if score >= 6:
        return "\U0001F7E1"  # yellow
    return "\U0001F7E0"  # orange


def _format_salary(job: dict) -> str:
    lo, hi = job.get("salary_min"), job.get("salary_max")
    interval = job.get("salary_interval", "") or ""
    suffix = {"yearly": "/yr", "monthly": "/mo", "hourly": "/hr"}.get(interval, "")

    def fmt(n):
        try:
            n = float(n)
        except (TypeError, ValueError):
            return None
        return f"${n/1000:.0f}k" if n >= 1000 else f"${n:.0f}"

    a, b = fmt(lo), fmt(hi)
    if a and b:
        return f"{a} - {b}{suffix}"
    if a:
        return f"{a}+{suffix}"
    if b:
        return f"Up to {b}{suffix}"
    return ""


def _format_job(job: dict) -> tuple[str, list]:
    score = int(job.get("score", 0) or 0)
    title = html.escape(str(job.get("job_title") or "Unknown role"))
    company = str(job.get("company") or "")
    if company in ("", "nan"):
        company = "Not listed"
    company = html.escape(company)

    location = job.get("location") or "Not specified"
    if job.get("is_remote") and location not in ("Remote", "Not specified"):
        location = f"{location} (remote ok)"
    location = html.escape(str(location))

    salary = _format_salary(job)
    tldr = html.escape(str(job.get("tldr") or job.get("reason") or "").strip())

    lines = [
        f"{_score_emoji(score)} <b>{score}/10</b>  <b>{title}</b>",
        f"\U0001F3E2 {company}  ·  \U0001F4CD {location}",
    ]
    if salary:
        lines.append(f"\U0001F4B0 {salary}")
    if tldr:
        lines.append("")
        lines.append(tldr[:600])

    pros = job.get("pros") or []
    cons = job.get("cons") or []
    if pros:
        lines.append("")
        lines.append("✅ " + html.escape("; ".join(str(p) for p in pros[:3])))
    if cons:
        lines.append("⚠️ " + html.escape("; ".join(str(c) for c in cons[:3])))

    # Legitimacy verdict (Tier 2). Loud line for anything not clearly legit.
    verdict = _verdict_of(job)
    if verdict:
        badge = _VERDICT_BADGE.get(verdict, verdict)
        lines.append("")
        lines.append(f"\U0001F50E Legitimacy: <b>{badge}</b>")
        if verdict != "LEGIT":
            summ = html.escape(str(job.get("legitimacy_summary") or "").strip())
            if summ:
                lines.append(summ[:400])

    url = job.get("job_url_direct") or ""
    if not url:
        jid = str(job.get("job_id") or "")
        url = jid if jid.startswith("http") else ""

    buttons = []
    if url:
        buttons = [[{"text": "\U0001F517 View listing", "url": url}]]
    return "\n".join(lines), buttons


def send_telegram_digest(scored_jobs: list) -> bool:
    """Post the top-scored jobs (>= threshold) to Telegram, highest first."""
    if not _enabled():
        return False

    matches = [
        j for j in scored_jobs
        if int(j.get("score", 0) or 0) >= config.SCORING_THRESHOLD
    ]
    # Demote AVOID to the bottom, otherwise highest score first.
    matches.sort(key=lambda j: (_is_avoid(j), -int(j.get("score", 0) or 0)))

    if not matches:
        logging.info("No jobs cleared the threshold; sending a short heads-up.")
        _send_message(
            "\U0001F50D <b>Job Scout</b>\nNo new matches today above the score cutoff."
        )
        return True

    top = max(int(j.get("score", 0) or 0) for j in matches)
    header = (
        f"\U0001F50D <b>Job Scout</b> — {len(matches)} new "
        f"{'match' if len(matches) == 1 else 'matches'} "
        f"(top {top}/10)"
    )
    _send_message(header)

    sent = 0
    for job in matches:
        text, buttons = _format_job(job)
        if _send_message(text, buttons):
            sent += 1
        time.sleep(0.4)  # stay under Telegram's ~30 msg/sec, be polite

    logging.info(f"Telegram digest: sent {sent}/{len(matches)} job messages.")
    return sent > 0


def send_telegram_nudge(scored_jobs: list) -> bool:
    """
    Post ONE short message: how many new matches, the single best one, and a
    pointer to the email digest + the chat bot. No per-job spam.
    """
    if not _enabled():
        return False

    matches = [
        j for j in scored_jobs
        if int(j.get("score", 0) or 0) >= config.SCORING_THRESHOLD
    ]
    if not matches:
        return _send_message(
            "\U0001F50D <b>Job Scout</b>\nNo new matches today above the score cutoff."
        )

    # Demote AVOID-flagged jobs so a scam can't be the headline pick; within
    # that, highest score first. AVOID jobs stay in the digest, just not on top.
    matches.sort(key=lambda j: (_is_avoid(j), -int(j.get("score", 0) or 0)))
    best = matches[0]
    bscore = int(best.get("score", 0) or 0)
    btitle = html.escape(str(best.get("job_title") or "a role"))
    bco = str(best.get("company") or "")
    bco = "" if bco in ("", "nan") else f" @ {html.escape(bco)}"
    n = len(matches)

    # Count risky employers so the headline can't read "all clear" when it isn't.
    avoid_n = sum(1 for j in matches if _is_avoid(j))
    susp_n = sum(1 for j in matches if _verdict_of(j) in ("SUSPICIOUS", "CAUTION"))

    best_badge = ""
    if _verdict_of(best) and _verdict_of(best) != "LEGIT":
        best_badge = f"  ({_VERDICT_BADGE.get(_verdict_of(best), '')})"

    text = (
        f"\U0001F50D <b>Job Scout</b> — {n} new "
        f"{'match' if n == 1 else 'matches'} today.\n"
        f"\U0001F947 Top {bscore}/10: <b>{btitle}</b>{bco}{best_badge}\n"
    )
    if avoid_n or susp_n:
        bits = []
        if avoid_n:
            bits.append(f"\U0001F6D1 {avoid_n} AVOID")
        if susp_n:
            bits.append(f"⚠️ {susp_n} to vet")
        text += f"\U0001F6E1 Legitimacy: {', '.join(bits)} (demoted, not hidden).\n"
    text += (
        f"\n\U0001F4E7 Full digest in your email.\n"
        f"\U0001F4AC Message me to dig in: ask about a listing, draft a cover "
        f"letter, tweak your profile, or give feedback."
    )
    return _send_message(text)


def send_telegram_alert(subject: str, body: str) -> bool:
    """Push a failure alert to Telegram."""
    if not _enabled():
        return False
    text = f"\U0001F6A8 <b>{html.escape(subject)}</b>\n\n{html.escape(body)[:2000]}"
    return _send_message(text)
