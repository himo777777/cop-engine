"""
COP AI Base — Gemensam infrastruktur för AI-moduler
====================================================
Claude API wrapper med caching, rate limiting och felhantering.
"""

import os
import re
import time
import json
import hashlib
import logging
import threading
from typing import Optional

logger = logging.getLogger("cop.ai")

ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
_client = None
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_rate_counters: dict[str, list[float]] = {}

CACHE_TTL = 300  # 5 minutes
RATE_LIMIT = 10  # per minute per clinic
RATE_WINDOW = 60  # seconds
MAX_RETRIES = 3
RETRY_DELAYS = [1, 2, 4]  # seconds


class ConfigError(Exception):
    """Raised when required configuration (e.g. API key) is missing."""
    pass


def get_client():
    """Hämta Anthropic-klient (lazy init). Kastar ConfigError om API-nyckel saknas."""
    global _client
    if _client is None:
        try:
            import anthropic
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                raise ConfigError("ANTHROPIC_API_KEY saknas — sätt miljövariabeln innan start")
            _client = anthropic.Anthropic(api_key=api_key)
        except ImportError:
            logger.error("anthropic-paketet är inte installerat")
            raise ConfigError("anthropic-paketet saknas — kör: pip install anthropic")
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
    with _cache_lock:
        if key in _cache:
            ts, result = _cache[key]
            if time.time() - ts < CACHE_TTL:
                return result
            del _cache[key]
    return None


def _set_cached(key: str, result: dict):
    with _cache_lock:
        _cache[key] = (time.time(), result)


def _extract_json(text: str) -> Optional[dict]:
    """
    Försöker extrahera ett JSON-objekt ur text på tre sätt:
    1. Direkt parsning
    2. find/rfind (hanterar text runt JSON)
    3. Regex (hanterar inbäddad JSON i längre text)
    Returnerar None om alla försök misslyckas.
    """
    # Försök 1: direkt parsning
    try:
        return json.loads(text)
    except Exception:
        pass

    # Försök 2: find/rfind
    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end])
        except Exception:
            pass

    # Försök 3: regex (hanterar kod-block-inbäddad JSON)
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass

    return None


async def call_claude(
    system: str,
    messages: list[dict],
    clinic_id: str = "default",
    tools: list[dict] = None,
    max_tokens: int = 2000,
) -> dict:
    """
    Anropa Claude med caching, rate limiting och retry.

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
        logger.debug("cache_hit", extra={"clinic_id": clinic_id, "key": cache_key[:8]})
        return cached

    try:
        client = get_client()
    except ConfigError as e:
        logger.error(f"Konfigurationsfel: {e}")
        return {"error": f"AI ej tillgänglig ({e})", "text": "", "tool_calls": []}

    kwargs = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools

    _record_call(clinic_id)
    t_start = time.time()

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            import anthropic as _anthropic
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
                "usage": {
                    "input": response.usage.input_tokens,
                    "output": response.usage.output_tokens,
                },
            }

            logger.info(
                "claude_call",
                extra={
                    "clinic_id": clinic_id,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "latency_ms": int((time.time() - t_start) * 1000),
                    "cached": False,
                },
            )

            _set_cached(cache_key, result)
            return result

        except Exception as e:
            # Klassificera: retry bara på transienta fel
            retryable = False
            try:
                import anthropic as _anthropic
                retryable = isinstance(
                    e,
                    (_anthropic.RateLimitError, _anthropic.APIConnectionError, _anthropic.InternalServerError),
                )
            except ImportError:
                pass

            last_error = e
            if retryable and attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[attempt]
                logger.warning(f"Retry {attempt + 1}/{MAX_RETRIES} efter {delay}s: {e}")
                time.sleep(delay)
            else:
                break

    logger.error(f"Claude API-fel efter {MAX_RETRIES} försök: {last_error}")
    return {"error": f"AI-fel: {str(last_error)}", "text": "", "tool_calls": []}
