"""
COP AI Predict — Predictive Absence Analysis
==============================================
Analyserar historisk frånvaro och förutsäger riskperioder.
"""

import json
import collections
import datetime
from ai_base import call_claude, _extract_json

SYSTEM_PROMPT = """Du är en frånvaroanalytiker för klinisk schemaläggning.

Analysera historisk frånvarodata och ge:
1. Mönster (vilka dagar/perioder har hög frånvaro)
2. Riskbedömning för kommande period
3. Konkreta rekommendationer

Svara på svenska i JSON:
```json
{
  "predictions": [
    {"date": "2026-04-07", "risk_level": "high|medium|low", "reason": "..."}
  ],
  "patterns_sv": ["Måndagar har 30% högre sjukfrånvaro", "..."],
  "recommendations_sv": ["Överväg extra bemanning tisdag", "..."],
  "overall_risk": "high|medium|low"
}
```"""

_WEEKDAYS_SV = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"]


async def predict_absence(
    history: list[dict], period_start: str, period_end: str,
    num_doctors: int, clinic_id: str = "default"
) -> dict:
    """Förutsäg frånvarorisk baserat på historik."""
    if not history:
        return {
            "predictions": [],
            "patterns_sv": ["Ingen historisk data tillgänglig"],
            "recommendations_sv": ["Samla frånvarodata i minst 3 månader för bättre förutsägelser"],
            "overall_risk": "unknown",
            "error": None,
        }

    # Sammanfatta historik för prompten
    summary = json.dumps(history[:50], ensure_ascii=False, default=str)

    messages = [{"role": "user", "content": f"""Historisk frånvarodata ({len(history)} poster):
{summary}

Klinik: {num_doctors} läkare
Period att förutsäga: {period_start} till {period_end}

Analysera mönster och ge riskbedömning."""}]

    result = await call_claude(SYSTEM_PROMPT, messages, clinic_id=clinic_id)

    if result.get("error"):
        return _statistical_fallback(history, period_start, period_end)

    parsed = _extract_json(result["text"])
    if parsed is not None:
        parsed["error"] = None
        return parsed

    return _statistical_fallback(history, period_start, period_end)


def _statistical_fallback(history: list[dict], period_start: str, period_end: str) -> dict:
    """
    Enkel statistisk fallback: räknar frånvaro per veckodag.
    """
    weekday_counts: collections.Counter = collections.Counter()
    total = 0

    for entry in history:
        date_val = entry.get("date", "")
        try:
            if isinstance(date_val, str):
                d = datetime.date.fromisoformat(date_val)
            else:
                d = date_val
            weekday_counts[d.weekday()] += 1
            total += 1
        except (ValueError, AttributeError):
            continue

    patterns = []
    if weekday_counts and total > 0:
        top_day, top_count = weekday_counts.most_common(1)[0]
        pct = int(top_count / total * 100)
        patterns.append(f"Historiskt flest frånvarofall på {_WEEKDAYS_SV[top_day]} ({pct}% av fallen).")

    # Overall risk baserat på total frånvarofrekvens mot historikens längd
    unique_dates = len({e.get("date") for e in history if e.get("date")})
    if unique_dates == 0:
        overall_risk = "unknown"
    else:
        try:
            start = datetime.date.fromisoformat(period_start)
            end = datetime.date.fromisoformat(period_end)
            period_days = max((end - start).days, 1)
            ratio = total / period_days
        except ValueError:
            ratio = 0

        if ratio > 0.2:
            overall_risk = "high"
        elif ratio > 0.1:
            overall_risk = "medium"
        else:
            overall_risk = "low"

    return {
        "predictions": [],
        "patterns_sv": patterns or ["Otillräcklig data för mönsteranalys."],
        "recommendations_sv": ["AI-analys ej tillgänglig — baserat på historisk statistik."],
        "overall_risk": overall_risk,
        "error": None,
        "fallback": True,
    }
