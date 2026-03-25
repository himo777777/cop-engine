"""
COP AI Conflicts — Rule Conflict Detection
============================================
Identifierar konflikter mellan befintliga och nya regler.
"""

import json
from data_model import ClinicConfig, config_to_dict
from ai_base import call_claude

SYSTEM_PROMPT = """Du är en regelkonfliktanalysator för klinisk schemaläggning.

Givet en lista befintliga regler och en ny regel, identifiera:
1. Direkta konflikter (regler som motsäger varandra)
2. Potentiella omöjligheter (kombination gör schemat olösbart)
3. Förslag på lösningar

Svara på svenska i detta JSON-format:
```json
{
  "conflicts": [
    {"rule_id": "...", "description": "...", "severity": "high|medium|low"}
  ],
  "feasibility": "ok|warning|impossible",
  "suggestions_sv": ["..."]
}
```"""


async def check_conflicts(config: ClinicConfig, new_rule: dict, clinic_id: str = "default") -> dict:
    """Kontrollera konflikter mellan ny regel och befintliga."""
    existing = [{"id": r.id, "name": r.name, "category": r.category,
                 "is_hard": r.is_hard, "weight": r.weight, "parameters": r.parameters}
                for r in config.constraint_rules if r.enabled]

    messages = [{"role": "user", "content": f"""Befintliga regler:
{json.dumps(existing, ensure_ascii=False, indent=2)}

Ny regel att lägga till:
{json.dumps(new_rule, ensure_ascii=False, indent=2)}

Klinik: {len(config.doctors)} läkare, {len(config.operating_rooms)} salar, sites: {config.sites}

Finns det konflikter?"""}]

    result = await call_claude(SYSTEM_PROMPT, messages, clinic_id=clinic_id)

    if result.get("error"):
        return {"conflicts": [], "feasibility": "unknown", "suggestions_sv": [], "error": result["error"]}

    try:
        text = result["text"]
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        parsed = json.loads(text[json_start:json_end])
        parsed["error"] = None
        return parsed
    except Exception as e:
        return {"conflicts": [], "feasibility": "unknown", "suggestions_sv": [], "error": str(e)}
