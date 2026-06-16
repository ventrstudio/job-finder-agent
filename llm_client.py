"""
LLM Client using OpenRouter (OpenAI-compatible chat completions).

Scoring runs through OpenRouter's auto-router by default (model
"openrouter/auto"), which picks the cheapest capable model per request at no
upcharge. Pin config.SCORING_MODEL to a specific slug to override.

Same public interface as before so callers (score_jobs.py) need no changes:

    from llm_client import generate
    text = generate(prompt="Score this job...", system_prompt="...")
"""

from __future__ import annotations

import logging
import random
import time
from typing import Optional

import httpx

import config
import cost_tracker

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Optional attribution headers (OpenRouter ranks/credits these; harmless if kept).
_HEADERS_EXTRA = {
    "HTTP-Referer": "https://ventr.studio",
    "X-Title": "VENTR Job Scout",
}


def generate(
    prompt: str,
    system_prompt: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 1024,
    model: Optional[str] = None,
    max_retries: int = 3,
    response_format: Optional[dict] = None,
) -> str:
    """
    Generate content via OpenRouter.

    Args mirror the old Anthropic client. Returns the assistant text content.
    Retries on 429 / 5xx with exponential backoff.
    """
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set. Cannot call the LLM.")

    model = model or config.SCORING_MODEL

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        payload["response_format"] = response_format
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        **_HEADERS_EXTRA,
    }

    last_exception: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            resp = httpx.post(OPENROUTER_URL, json=payload, headers=headers, timeout=60)

            # Retry on rate limit / transient server errors.
            if resp.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(
                    f"retryable {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()

            data = resp.json()

            # Cost tracking (best-effort; unknown models fall back to a default price).
            try:
                usage = data.get("usage", {}) or {}
                cost_tracker.tracker.record(
                    model=data.get("model", model),
                    input_tokens=usage.get("prompt_tokens", 0) or 0,
                    output_tokens=usage.get("completion_tokens", 0) or 0,
                )
            except Exception as ce:  # noqa: BLE001
                logger.debug(f"Cost tracking skipped: {ce}")

            content = (
                data.get("choices", [{}])[0].get("message", {}).get("content", "")
            )
            return content.strip() if content else ""

        except httpx.HTTPStatusError as e:
            last_exception = e
            status = e.response.status_code if e.response is not None else "?"
            if status in (429, 500, 502, 503, 504) and attempt < max_retries:
                delay = (2**attempt) * 8 + random.uniform(0, 4)
                logger.warning(
                    f"OpenRouter {status} (attempt {attempt + 1}/{max_retries + 1}). "
                    f"Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
                continue
            body = e.response.text[:200] if e.response is not None else ""
            logger.error(f"OpenRouter API error {status}: {body}")
            raise

        except httpx.HTTPError as e:
            last_exception = e
            if attempt < max_retries:
                delay = (2**attempt) * 8 + random.uniform(0, 4)
                logger.warning(
                    f"OpenRouter request failed: {e} (attempt {attempt + 1}). "
                    f"Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
                continue
            logger.error(f"OpenRouter request failed after retries: {e}")
            raise

    raise last_exception  # type: ignore[misc]
