"""
Email digest sender using Emailit API.

Sends a formatted HTML email with the top-scored job matches from the current run.
Each job includes: score, type, pay, TLDR summary, pros/cons.
"""

import json
import logging
from typing import Optional

import httpx

import config
import supabase_utils

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def _format_salary(job: dict) -> str:
    """Format salary range for display."""
    sal_min = job.get("salary_min")
    sal_max = job.get("salary_max")
    interval = job.get("salary_interval", "")

    if not sal_min and not sal_max:
        return "Not listed"

    # Format numbers
    def fmt(n):
        if n is None:
            return "?"
        n = float(n)
        if n >= 1000:
            return f"${n:,.0f}"
        return f"${n:.0f}"

    if sal_min and sal_max:
        salary = f"{fmt(sal_min)} - {fmt(sal_max)}"
    elif sal_min:
        salary = f"{fmt(sal_min)}+"
    else:
        salary = f"Up to {fmt(sal_max)}"

    if interval:
        interval_labels = {
            "yearly": "/yr",
            "monthly": "/mo",
            "weekly": "/wk",
            "daily": "/day",
            "hourly": "/hr",
        }
        salary += interval_labels.get(interval, f" ({interval})")

    return salary


def _format_job_type(job: dict) -> str:
    """Format job type badge."""
    jt = job.get("job_type", "")
    is_remote = job.get("is_remote")

    labels = {
        "parttime": "Part-Time",
        "contract": "Contract",
        "fulltime": "Full-Time",
        "temporary": "Temporary",
        "internship": "Internship",
        "contractor": "Contract",
    }
    type_label = labels.get(jt, jt.replace("_", " ").title() if jt else "Unknown")

    if is_remote:
        type_label += " / Remote"

    return type_label


def _badge_color(job_type: str) -> str:
    """Color for job type badge."""
    if "part" in job_type.lower():
        return "#16a34a"  # green
    elif "contract" in job_type.lower():
        return "#2563eb"  # blue
    elif "full" in job_type.lower():
        return "#f59e0b"  # amber (warning — not what we want)
    return "#6b7280"  # gray


def _score_color(score: int) -> str:
    if score >= 8:
        return "#16a34a"
    elif score >= 6:
        return "#2563eb"
    elif score >= 5:
        return "#f59e0b"
    return "#6b7280"


_VERDICT_STYLE = {
    "AVOID":      ("#991b1b", "#fef2f2", "🛑 AVOID"),
    "SUSPICIOUS": ("#92400e", "#fffbeb", "⚠️ SUSPICIOUS"),
    "CAUTION":    ("#92400e", "#fffbeb", "⚠️ CAUTION"),
    "LEGIT":      ("#166534", "#dcfce7", "✅ Legit"),
    "UNKNOWN":    ("#374151", "#f3f4f6", "❔ Unverified"),
}

# Source pill: the real brand favicon + a text label so the board is obvious
# at a glance. Icon is pulled from Google's favicon service (returns a PNG,
# renders reliably in Gmail — unlike inline SVG, which Gmail strips). The text
# label is the fallback if a client blocks remote images.
_SOURCE_STYLE = {
    "linkedin": ("linkedin.com", "LinkedIn", "#0a66c2"),
    "indeed":   ("indeed.com", "Indeed", "#2557a7"),
}


def _source_badge(job: dict) -> str:
    provider = (job.get("provider") or "").lower()
    style = _SOURCE_STYLE.get(provider)
    if not style:
        return ""
    domain, label, color = style
    icon = (
        f'<img src="https://www.google.com/s2/favicons?domain={domain}&sz=64" '
        f'width="14" height="14" alt="{label}" '
        f'style="vertical-align:-2px;border:0;border-radius:3px;margin-right:5px;">'
    )
    return (
        f'<span style="display:inline-block;background:#f3f4f6;color:{color};'
        f'padding:2px 9px;border-radius:4px;font-weight:600;font-size:12px;'
        f'margin-left:8px;">{icon}{label}</span>'
    )


def _render_legitimacy(job: dict) -> str:
    """Legitimacy verdict banner for the email row (Tier 2)."""
    verdict = str(job.get("legitimacy_verdict") or "").upper()
    if not verdict:
        return ""
    color, bg, label = _VERDICT_STYLE.get(verdict, _VERDICT_STYLE["UNKNOWN"])
    summary = job.get("legitimacy_summary") or ""
    badge = (f'<span style="display:inline-block;background:{bg};color:{color};'
             f'padding:2px 10px;border-radius:4px;font-weight:700;font-size:12px;">'
             f'Legitimacy: {label}</span>')
    if verdict == "LEGIT" or not summary:
        return f'<div style="margin-top:8px;">{badge}</div>'
    safe = str(summary).replace("<", "&lt;").replace(">", "&gt;")
    return (f'<div style="margin-top:8px;">{badge}'
            f'<div style="margin-top:4px;font-size:12px;color:{color};line-height:1.4;">{safe}</div></div>')


def _render_pros_cons(pros: list, cons: list) -> str:
    """Render pros/cons as inline HTML."""
    html = ""
    if pros:
        items = "".join(f'<span style="display:inline-block;background:#dcfce7;color:#166534;padding:2px 8px;border-radius:4px;font-size:12px;margin:2px 4px 2px 0;">+ {p}</span>' for p in pros)
        html += f'<div style="margin-top:6px;">{items}</div>'
    if cons:
        items = "".join(f'<span style="display:inline-block;background:#fef2f2;color:#991b1b;padding:2px 8px;border-radius:4px;font-size:12px;margin:2px 4px 2px 0;">- {c}</span>' for c in cons)
        html += f'<div style="margin-top:4px;">{items}</div>'
    return html


def _format_posted(job: dict) -> str:
    """Human posting age from the date_posted field, e.g. 'Posted 3 days ago'."""
    from datetime import date, datetime

    raw = job.get("date_posted")
    if not raw:
        return ""
    try:
        d = datetime.fromisoformat(str(raw)[:10]).date()
    except (ValueError, TypeError):
        return ""
    days = (date.today() - d).days
    if days <= 0:
        label = "Posted today"
    elif days == 1:
        label = "Posted yesterday"
    else:
        label = f"Posted {days} days ago"
    return f"{label} ({d.strftime('%b %-d')})"


def build_email_html(scored_jobs: list) -> str:
    """Build an HTML email body from scored jobs."""

    if not scored_jobs:
        return "<p>No new job matches found in this run.</p>"

    top_score = max(job.get("score", 0) for job in scored_jobs)

    rows = ""
    for job in scored_jobs:
        score = job.get("score", 0)
        title = job.get("job_title", "Unknown Role")
        company = job.get("company", "Unknown")
        if company == "nan" or not company:
            company = "Not listed"
        location = job.get("location") or "Not specified"
        if job.get("is_remote") and location not in ("Remote", "Not specified"):
            location = f"Remote ({location})"
        tldr = job.get("tldr", job.get("reason", ""))
        job_id = job.get("job_id", "")

        # Parse pros/cons (might be JSON strings or lists)
        pros = job.get("pros", [])
        cons = job.get("cons", [])
        if isinstance(pros, str):
            try:
                pros = json.loads(pros)
            except Exception:
                pros = []
        if isinstance(cons, str):
            try:
                cons = json.loads(cons)
            except Exception:
                cons = []

        salary = _format_salary(job)
        posted = _format_posted(job)
        job_type = _format_job_type(job)
        type_color = _badge_color(job_type)
        score_bg = _score_color(score)

        # Build listing link (job_id from Apify is short jobKey, not URL — use job_url_direct)
        listing_url = job.get("job_url_direct") or (job_id if job_id.startswith("http") else "")
        listing_link = ""
        if listing_url:
            listing_link = f'<a href="{listing_url}" style="color:#2563eb;text-decoration:none;font-size:13px;">View Listing &rarr;</a>'

        # The job title itself links to the listing — clicking the title is the
        # natural move. Falls back to plain text when no URL is available.
        if listing_url:
            title_html = f'<a href="{listing_url}" style="color:#111827;text-decoration:none;">{title}</a>'
        else:
            title_html = title

        pros_cons_html = _render_pros_cons(pros, cons)

        rows += f"""
        <tr>
            <td style="padding:20px 0;border-bottom:1px solid #e5e7eb;">
                <table style="width:100%;border-collapse:collapse;">
                    <tr>
                        <td style="vertical-align:top;">
                            <div style="font-size:17px;font-weight:600;color:#111827;">{title_html}</div>
                            <div style="font-size:14px;color:#6b7280;margin-top:2px;">{company}{(' &middot; ' + posted) if posted else ''}</div>
                        </td>
                        <td style="text-align:right;vertical-align:top;white-space:nowrap;">
                            {_source_badge(job)}
                            <span style="display:inline-block;background:{score_bg};color:white;padding:4px 12px;border-radius:20px;font-weight:700;font-size:15px;">{score}/10</span>
                        </td>
                    </tr>
                </table>

                <div style="margin-top:10px;">
                    <span style="display:inline-block;background:{type_color};color:white;padding:2px 10px;border-radius:4px;font-weight:600;font-size:13px;">{job_type}</span>
                    <span style="display:inline-block;background:#f3f4f6;color:#374151;padding:2px 10px;border-radius:4px;font-size:13px;">{location}</span>
                    <span style="display:inline-block;background:#f3f4f6;color:#374151;padding:2px 10px;border-radius:4px;font-weight:600;font-size:13px;">{salary}</span>
                </div>

                <div style="margin-top:10px;font-size:14px;color:#374151;line-height:1.5;">{tldr}</div>

                {pros_cons_html}

                {_render_legitimacy(job)}

                <div style="margin-top:10px;">{listing_link}</div>
            </td>
        </tr>"""

    # Real scoring cost this run (actual OpenRouter usage.cost, not an estimate).
    import llm_client
    cost_line = ""
    if llm_client.run_calls > 0:
        cost_line = f'<span style="color:#9ca3af;"> | Scoring cost: ${llm_client.run_cost_usd:.4f} ({llm_client.run_calls} calls)</span>'

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:640px;margin:0 auto;padding:24px;">
        <h1 style="font-size:22px;color:#111827;margin-bottom:4px;">Job Scout Report</h1>
        <p style="color:#6b7280;margin-top:0;">{len(scored_jobs)} matches | Top score: {top_score}/10{cost_line}</p>

        <table style="width:100%;border-collapse:collapse;">
            {rows}
        </table>

        <div style="margin-top:24px;color:#9ca3af;font-size:12px;text-align:center;">
            Otis's Job Finder System | Scores based on your profile and preferences
        </div>
    </div>"""

    return html


def send_digest(scored_jobs: list) -> bool:
    """
    Send the job digest email via Emailit.

    Args:
        scored_jobs: List of jobs with score, tldr, pros, cons fields.

    Returns:
        True if email sent successfully, False otherwise.
    """
    if not config.EMAILIT_API_KEY:
        logging.error("EMAILIT_API_KEY not set. Cannot send digest.")
        return False

    if not scored_jobs:
        logging.info("No jobs to send in digest. Skipping email.")
        return True

    # Filter to threshold, then sort: AVOID-flagged jobs demoted to the bottom,
    # otherwise highest score first (label + demote, never hidden).
    qualified_jobs = [j for j in scored_jobs if j.get("score", 0) >= config.SCORING_THRESHOLD]
    qualified_jobs.sort(
        key=lambda x: (str(x.get("legitimacy_verdict") or "").upper() == "AVOID",
                       -int(x.get("score", 0) or 0))
    )

    # Safety net: drop duplicate listings (same job pulled from two search
    # URLs). Sorted score-desc, so the copy kept is the highest-scored one.
    seen_keys = set()
    deduped = []
    for j in qualified_jobs:
        k = supabase_utils.normalize_key(j.get("company", ""), j.get("job_title", ""))
        if k in seen_keys:
            continue
        seen_keys.add(k)
        deduped.append(j)
    qualified_jobs = deduped

    if not qualified_jobs:
        logging.info(f"No jobs met the threshold ({config.SCORING_THRESHOLD}/10). Skipping email.")
        return True

    top_score = qualified_jobs[0].get("score", 0)
    subject = f"Job Scout: {len(qualified_jobs)} new matches (top score: {top_score}/10)"

    html_body = build_email_html(qualified_jobs)

    logging.info(f"Sending digest email: {subject}")

    try:
        response = httpx.post(
            config.EMAILIT_API_URL,
            headers={
                "Authorization": f"Bearer {config.EMAILIT_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": config.EMAILIT_FROM,
                "to": config.EMAILIT_TO,
                "subject": subject,
                "html": html_body,
            },
            timeout=30,
        )

        if response.status_code in (200, 201, 202):
            logging.info(f"Digest sent successfully to {config.EMAILIT_TO}")
            return True
        else:
            logging.error(f"Emailit API error: {response.status_code} -- {response.text[:200]}")
            return False

    except Exception as e:
        logging.error(f"Error sending digest email: {e}")
        return False


def send_alert(subject: str, body: str) -> bool:
    """
    Send a failure alert email via Emailit.

    Used when the pipeline hits a hard error (scraper crash, uncaught
    exception) so a silent break never goes unnoticed again.
    """
    if not config.EMAILIT_API_KEY:
        logging.error("EMAILIT_API_KEY not set. Cannot send alert.")
        return False

    safe_body = (body or "").replace("<", "&lt;").replace(">", "&gt;")
    html = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;'
        'max-width:600px;margin:0 auto;padding:24px;">'
        '<h1 style="font-size:20px;color:#991b1b;">Job Scout: pipeline alert</h1>'
        f'<p style="font-size:14px;color:#374151;line-height:1.5;white-space:pre-wrap;">{safe_body}</p>'
        '<p style="font-size:12px;color:#9ca3af;margin-top:20px;">'
        "Today's run did not deliver matches as expected. "
        'Check the GitHub Actions log for the Job Finder Daily Pipeline.</p>'
        '</div>'
    )

    logging.info(f"Sending alert email: {subject}")

    try:
        response = httpx.post(
            config.EMAILIT_API_URL,
            headers={
                "Authorization": f"Bearer {config.EMAILIT_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": config.EMAILIT_FROM,
                "to": config.EMAILIT_TO,
                "subject": subject,
                "html": html,
            },
            timeout=30,
        )

        if response.status_code in (200, 201, 202):
            logging.info(f"Alert email sent to {config.EMAILIT_TO}")
            return True
        logging.error(f"Emailit alert error: {response.status_code} -- {response.text[:200]}")
        return False

    except Exception as e:
        logging.error(f"Error sending alert email: {e}")
        return False
