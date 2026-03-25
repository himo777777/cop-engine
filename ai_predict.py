"""
COP AI Predict — Predictive Absence Analysis
==============================================
Analyserar historisk frånvaro och förutsäger riskperioder.
"""

import json
from ai_base import call_claude

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
        return {"predictions": [], "patterns_sv": [], "recommendations_sv": [], "overall_risk": "unknown", "error": result["error"]}

    try:
        text = result["text"]
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        parsed = json.loads(text[json_start:json_end])
        parsed["error"] = None
        return parsed
    except Exception as e:
        return {"predictions": [], "patterns_sv": [], "recommendations_sv": [], "overall_risk": "unknown", "error": str(e)}
