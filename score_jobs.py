"""
Job scoring pipeline using Claude.

Scores each job listing against the agent profile on a 1-10 scale
with a one-line reasoning explanation.
"""

import time
import logging
from typing import Optional

import config
import supabase_utils
from llm_client import generate

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


SCORING_SYSTEM_PROMPT = """You are a job scoring assistant. You evaluate job listings for fit against a candidate's profile.

Score each job on a scale of 1-10:
- 1-2: Dealbreaker present or completely wrong fit
- 3-4: Poor fit, major misalignment
- 5-6: Partial fit, some relevant elements
- 7-8: Good fit, strong alignment
- 9-10: Excellent fit, nearly perfect match

JOB TYPE: The candidate prefers part-time, contract, freelance, or project-based work, but is also open to full-time roles when the work itself is a strong fit (AI, automation, or workflow focus). Do NOT penalize a job just for being full-time. Judge full-time roles on role fit, skills match, pay, and the dealbreakers below. A strong full-time AI/automation role can still score 7 or higher. A generic full-time role with weak fit scores on its merits, with no automatic boost.

Return ONLY a JSON object with these fields:
{
  "score": <integer 1-10>,
  "tldr": "<2-3 sentence plain-English summary of what the job actually is and what they want>",
  "pros": ["<pro 1>", "<pro 2>"],
  "cons": ["<con 1>", "<con 2>"]
}

Keep the tldr conversational and specific. Pros/cons should be from the candidate's perspective based on their profile. 2-3 of each, short phrases. No other text, just the JSON."""


def build_scoring_prompt(job: dict, profile: dict) -> str:
    """Build the scoring prompt with job details and profile context."""

    resume_text = profile.get("resume_text", "")
    target_roles = profile.get("target_roles", [])
    skills = profile.get("skills", [])
    job_types = profile.get("job_types", [])
    location_pref = profile.get("location_preference", "")
    anti_patterns = profile.get("anti_patterns") or ""
    # anti_patterns is a text[] column — render the list as bullet lines for the prompt
    if isinstance(anti_patterns, list):
        anti_patterns = "\n".join(f"- {a}" for a in anti_patterns if a)
    custom_prompt = profile.get("custom_prompt", "")

    prompt = f"""## CANDIDATE PROFILE

**Target Roles:** {', '.join(target_roles) if target_roles else 'Not specified'}
**Key Skills:** {', '.join(skills) if skills else 'Not specified'}
**Work Types:** {', '.join(job_types) if job_types else 'Any'}
**Location:** {location_pref or 'Remote preferred'}

**Resume:**
{resume_text[:3000] if resume_text else 'Not available'}

**Positive Signal Keywords (boost score):**
n8n, Make, Zapier, automation platforms, AI, LLM, Claude, OpenAI, GPT, Gemini, AI integration, AI implementation, Supabase, Airtable, Firebase, PostgreSQL, React, Next.js, Tailwind, web development, API integration, webhook, workflow automation, no-code, low-code, citizen developer, MCP, Model Context Protocol, AI agents, Cursor, Claude Code, AI-assisted development, CRM, process optimization, digital transformation, UI/UX, design systems, frontend development

**Automatic Dealbreakers (score 1-2) — only when this is the CORE of the role, not an incidental mention:**
- Strictly requires a CS degree with no "or equivalent experience" path
- On-call / pager duty as a routine expectation
- Heavy people management (leading a team is the main job)
- Java, .NET, C++, or enterprise legacy stacks as the primary tech
- Heavy data science / ML model training / statistical modeling as the main work
- Manual QA / software testing as the primary role
- Pure DevOps, infrastructure, sysadmin, or cloud-ops as the primary role

A job asking for "5+ years experience" (or similar) is NOT a dealbreaker. The
candidate actively applies to and interviews for roles with those asks. Judge
on skill and role fit, not years-of-experience requirements.

**Bilingual bonus:** English (native) + Spanish (fluent). Roles valuing bilingual get a score boost.

**Time Zone Constraint (IMPORTANT):**
The candidate is in **Eastern Time (ET, UTC-5/-4)** and will not relocate. Even for remote roles, the company's HQ time zone matters because of meetings and overlap windows. Apply this rule:
- Job HQ in ET or "US-wide remote": no penalty
- Job HQ in CT (Central, 1hr behind): minor penalty (-1)
- Job HQ in MT/PT (Mountain/Pacific, 2-3hr behind) AND requires real-time collaboration / standup attendance / overlap with West Coast hours: significant penalty (-2 or score 4 max)
- Job explicitly requires being in a non-ET time zone (e.g. "must work PT hours", "core hours 9-5 PST"): score 3 or lower
- International (EU, APAC) requiring overlap outside ET business hours: score 2 or lower

If the description doesn't mention time zone requirements at all, assume flexible and don't penalize.

"""

    if anti_patterns:
        prompt += f"""**Learned Anti-Patterns (from feedback, also score lower):**
{anti_patterns}

"""

    if custom_prompt:
        prompt += f"""**Additional Instructions:**
{custom_prompt}

"""

    # Format salary for the prompt
    sal_min = job.get('salary_min')
    sal_max = job.get('salary_max')
    sal_interval = job.get('salary_interval', '')
    salary_str = 'Not listed'
    if sal_min or sal_max:
        parts = []
        if sal_min:
            parts.append(f"${float(sal_min):,.0f}")
        if sal_max:
            parts.append(f"${float(sal_max):,.0f}")
        salary_str = " - ".join(parts)
        if sal_interval:
            salary_str += f" ({sal_interval})"

    job_type = job.get('job_type', 'Not specified')
    is_remote = job.get('is_remote')
    remote_str = 'Yes' if is_remote else ('No' if is_remote is False else 'Not specified')

    prompt += f"""## JOB LISTING

**Title:** {job.get('job_title', 'N/A')}
**Company:** {job.get('company', 'N/A')}
**Location:** {job.get('location', 'N/A')}
**Job Type:** {job_type}
**Remote:** {remote_str}
**Salary:** {salary_str}
**Level:** {job.get('level', 'N/A')}

**Description:**
{job.get('description', 'No description available')[:4000]}

---

Score this job. Return only the JSON object."""

    return prompt


def score_job(job: dict, profile: dict) -> Optional[dict]:
    """
    Score a single job against the profile.

    Returns:
        Dict with score, tldr, pros, cons — or None on failure.
    """
    import json
    import re

    prompt = build_scoring_prompt(job, profile)

    try:
        response_text = generate(
            prompt=prompt,
            system_prompt=SCORING_SYSTEM_PROMPT,
            temperature=0,  # deterministic — same job scores the same every time
            max_tokens=800,  # headroom so a verbose tldr can't truncate the JSON
            response_format={"type": "json_object"},  # force clean JSON (no prose/fences)
        )

        # Strip markdown code fences if present
        text = response_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]  # remove first line (```json)
            if text.endswith("```"):
                text = text[:-3].strip()

        # Parse JSON response. Fall back to extracting the first {...} block so a
        # model that adds stray prose around the object still scores.
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if not m:
                raise
            result = json.loads(m.group(0))
        score = int(result.get("score", 0))
        tldr = result.get("tldr", "")
        pros = result.get("pros", [])
        cons = result.get("cons", [])

        if 1 <= score <= 10:
            return {
                "score": score,
                "tldr": tldr,
                "pros": pros,
                "cons": cons,
                "reason": tldr,  # backward compat
            }
        else:
            logging.warning(f"Score out of range ({score}) for job {job.get('job_id')}")
            return None

    except json.JSONDecodeError:
        logging.error(f"Could not parse JSON from LLM for job {job.get('job_id')}: {response_text[:200]}")
        return None
    except Exception as e:
        logging.error(f"Error scoring job {job.get('job_id')}: {e}")
        return None


def get_agent_profile() -> dict:
    """Fetch the agent profile from Supabase."""
    try:
        response = supabase_utils.supabase.table("agent_profile").select("*").limit(1).execute()
        if response.data and len(response.data) > 0:
            return response.data[0]
        else:
            logging.warning("No agent profile found in database. Using empty profile.")
            return {}
    except Exception as e:
        logging.error(f"Error fetching agent profile: {e}")
        return {}


def score_unscored_jobs(limit: int = None) -> list:
    """
    Fetch unscored jobs from Supabase, score them, and update the database.

    Returns:
        List of scored job dicts (with score and reason).
    """
    limit = limit or config.JOBS_TO_SCORE_PER_RUN
    logging.info(f"--- Starting Job Scoring (limit: {limit}) ---")

    # Get profile
    profile = get_agent_profile()
    if not profile:
        logging.error("Cannot score without a profile. Aborting.")
        return []

    # Get unscored jobs (include extra fields for the digest)
    jobs_to_score = supabase_utils.get_jobs_to_score(limit)
    if not jobs_to_score:
        logging.info("No jobs need scoring.")
        return []

    logging.info(f"Scoring {len(jobs_to_score)} jobs...")
    scored_jobs = []

    for i, job in enumerate(jobs_to_score):
        job_id = job.get("job_id")
        if not job_id:
            continue

        logging.info(f"Scoring {i+1}/{len(jobs_to_score)}: {job.get('job_title', 'Unknown')} at {job.get('company', 'Unknown')}")

        result = score_job(job, profile)
        if result:
            # Update in Supabase (store score * 10 to fit the 0-100 column)
            score_for_db = result["score"] * 10
            supabase_utils.update_job_score(job_id, score_for_db, resume_score_stage="initial")
            # Save scoring details
            try:
                import json as _json
                supabase_utils.supabase.table("jobs").update({
                    "score_reason": result["tldr"],
                    "score_tldr": result["tldr"],
                    "score_pros": _json.dumps(result["pros"]),
                    "score_cons": _json.dumps(result["cons"]),
                }).eq("job_id", job_id).execute()
            except Exception:
                pass

            scored_jobs.append({
                **job,
                "score": result["score"],
                "reason": result["reason"],
                "tldr": result["tldr"],
                "pros": result["pros"],
                "cons": result["cons"],
            })
            logging.info(f"  Score: {result['score']}/10 — {result['reason']}")
        else:
            logging.warning(f"  Failed to score job {job_id}")

        # Small delay between API calls
        if i < len(jobs_to_score) - 1:
            time.sleep(1)

    logging.info(f"--- Scoring complete: {len(scored_jobs)}/{len(jobs_to_score)} scored ---")
    return scored_jobs


if __name__ == "__main__":
    if not config.ANTHROPIC_API_KEY:
        logging.error("ANTHROPIC_API_KEY not set.")
    elif not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        logging.error("Supabase URL or Key not set.")
    else:
        score_unscored_jobs()
