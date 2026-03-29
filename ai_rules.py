"""
COP AI Rules — Natural Language → Constraints
===============================================
Parsar svenska regeltexter till strukturerade ConstraintRule-objekt.
"""

import json
from data_model import ClinicConfig, config_to_dict
from ai_base import call_claude, _extract_json

SYSTEM_PROMPT = """Du är en regelparser för ett kliniskt schemaläggningssystem.

Din uppgift: Konvertera svenska regeltexter till strukturerade constraint-objekt.

## Output-format (JSON)
```json
{{
  "id": "custom_<kort_id>",
  "name": "<regelns namn>",
  "category": "atl|staffing|fairness|preference",
  "is_hard": true/false,
  "weight": 1-10,
  "enabled": true,
  "parameters": {{
    "doctor_ids": ["OL1"],
    "shift_types": ["natt"],
    "conditions": ["efter helgdag"],
    "max_value": 2,
    "min_value": 1
  }}
}}
```

## Kategorier
- "atl" = arbetstidslagen (vila, max timmar)
- "staffing" = bemanning (minsta antal, roller)
- "fairness" = rättvisa (jourfördelning, helger)
- "preference" = önskemål (specifika läkare, dagar)

## Regler
- is_hard=true: aldrig bryts (t.ex. "ska aldrig", "måste alltid")
- is_hard=false: optimeras (t.ex. "helst", "bör", "företrädesvis")
- weight: 10=kritiskt, 5=normalt, 1=nice-to-have

Svara BARA med JSON-objektet, inget annat. På svenska i name-fältet."""


async def parse_rule(config: ClinicConfig, rule_text: str, clinic_id: str = "default") -> dict:
    """
    Parsa en svensk regeltext till en ConstraintRule-dict.

    Returns:
        {"constraint": dict, "explanation_sv": str, "confidence": float, "error": str|None}
    """
    config_summary = _build_context(config)

    messages = [{"role": "user", "content": f"""Klinikkontext:
{config_summary}

Parsa denna regel till ett constraint-objekt:
"{rule_text}"

Returnera JSON-objektet + en kort svensk förklaring av hur regeln tolkas."""}]

    result = await call_claude(SYSTEM_PROMPT, messages, clinic_id=clinic_id)

    if result.get("error"):
        return {
            "constraint": None,
            "explanation_sv": "",
            "confidence": 0.0,
            "error": "AI krävs för regelparser — tjänsten är tillfälligt otillgänglig",
        }

    text = result["text"]
    try:
        constraint, explanation = _parse_response(text)
        confidence = _estimate_confidence(constraint, rule_text)
        return {
            "constraint": constraint,
            "explanation_sv": explanation,
            "confidence": confidence,
            "error": None,
        }
    except Exception as e:
        return {"constraint": None, "explanation_sv": "", "confidence": 0.0, "error": f"Kunde inte tolka AI-svar: {e}"}


def _build_context(config: ClinicConfig) -> str:
    """Bygg en kortfattad klinikkontext för prompten."""
    doctors = ", ".join(f"{d.id} ({d.name}, {d.role.value})" for d in config.doctors[:15])
    sites = ", ".join(config.sites)
    roles = "ÖL, SP, ST_SEN, ST_TIDIG, UL"
    return f"Läkare: {doctors}\nSites: {sites}\nRoller: {roles}\nAntal salar: {len(config.operating_rooms)}"


def _parse_response(text: str) -> tuple[dict, str]:
    """Extrahera JSON-constraint och förklaring från Claude-svar."""
    # Försök extrahera JSON med robust helper
    constraint = _extract_json(text)
    if constraint is None:
        raise ValueError("Inget JSON-objekt i svaret")

    # Förklaring = text efter JSON-blocket (om det finns)
    json_end = text.rfind("}") + 1
    explanation = text[json_end:].strip() if json_end > 0 else ""
    if not explanation:
        explanation = f"Regel tillagd: {constraint.get('name', 'okänd')}"

    return constraint, explanation


def _estimate_confidence(constraint: dict, original: str) -> float:
    """Enkel konfidensuppskattning baserad på constraint-kvalitet."""
    score = 0.5
    if constraint.get("id") and constraint.get("name"):
        score += 0.2
    if constraint.get("category") in ("atl", "staffing", "fairness", "preference"):
        score += 0.1
    if constraint.get("parameters") and len(constraint["parameters"]) > 0:
        score += 0.1
    if isinstance(constraint.get("weight"), int) and 1 <= constraint["weight"] <= 10:
        score += 0.1
    return min(score, 1.0)
