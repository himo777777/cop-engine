"""
COP AI Base — Gemensam infrastruktur för AI-moduler
====================================================
Claude API wrapper med caching, rate limiting och felhantering.
"""

import os
import time
import json
import hashlib
import logging
from typing import Optional

logger = logging.getLogger("cop.ai")

ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
_client = None
_cache: dict[str, tuple[float, dict]] = {}  # key → (timestamp, result)
_rate_counters: dict[str, list[float]] = {}  # clinic_id → [timestamps]

CACHE_TTL = 300  # 5 minutes
RATE_LIMIT = 10  # per minute per clinic
RATE_WINDOW = 60  # seconds


def get_client():
    """Hämta Anthropic-klient (lazy init)."""
    global _client
    if _client is None:
        try:
            import anthropic
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                logger.warning("ANTHROPIC_API_KEY not set")
                return None
            _client = anthropic.Anthropic(api_key=api_key)
        except ImportError:
            logger.error("anthropic package not installed")
            return None
    return _client


def _check_rate_limit(clinic_id: str) -> bool:
    """Returnerar True om under gränsen."""
    now = time.time()
    timestamps = _rate_counters.get(clinic_id, [])
    timestamps = [t for t in timestamps if now - t < RATE_WINDOW]
    _rate_counters[clinic_id] = timestamps
    return len(timestamps) < RATE_LIMIT


def _record_call(clinic_id: str):
    _rate_counters.setdefault(clinic_id, []).append(time.time())


def _cache_key(system: str, messages: list, tools: list = None) -> str:
    raw = json.dumps({"s": system, "m": messages, "t": tools}, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()


def _get_cached(key: str) -> Optional[dict]:
    if key in _cache:
        ts, result = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return result
        del _cache[key]
    return None


async def call_claude(
    system: str,
    messages: list[dict],
    clinic_id: str = "default",
    tools: list[dict] = None,
    max_tokens: int = 2000,
) -> dict:
    """
    Anropa Claude med caching och rate limiting.

    Returns:
        {"text": str, "tool_calls": list, "usage": dict} eller {"error": str}
    """
    # Rate limit check
    if not _check_rate_limit(clinic_id):
        return {"error": "Rate limit: max 10 AI-anrop per minut", "text": "", "tool_calls": []}

    # Cache check
    cache_key = _cache_key(system, messages, tools)
    cached = _get_cached(cache_key)
    if cached:
        return cached

    client = get_client()
    if not client:
        return {"error": "AI ej tillgänglig (API-nyckel saknas)", "text": "", "tool_calls": []}

    try:
        _record_call(clinic_id)

        kwargs = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        response = client.messages.create(**kwargs)

        text = ""
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                text += block.text
            elif block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "input": block.input})

        result = {
            "text": text,
            "tool_calls": tool_calls,
            "usage": {"input": response.usage.input_tokens, "output": response.usage.output_tokens},
        }

        # Cache result
        _cache[cache_key] = (time.time(), result)
        return result

    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return {"error": f"AI-fel: {str(e)}", "text": "", "tool_calls": []}
