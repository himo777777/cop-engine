"""
COP AI Explain — Schedule Explanation
=======================================
Förklarar schemaläggningsbeslut på svenska.
"""

import json
from data_model import ClinicConfig
from ai_base import call_claude

SYSTEM_PROMPT = """Du är en schemaförklarare för ett kliniskt schemaläggningssystem.

Förklara på svenska varför en specifik läkare fick ett visst pass. Var konkret:
- Vilka constraints tvingade detta val?
- Fanns alternativ? Varför valdes de bort?
- Var detta optimalt eller en kompromiss?

Svara i JSON:
```json
{
  "explanation_sv": "Tydlig förklaring...",
  "constraints_applied": ["constraint1", "constraint2"],
  "alternatives_considered": [
    {"doctor": "...", "reason_rejected": "..."}
  ],
  "quality": "optimal|acceptable|compromise"
}
```"""


async def explain_assignment(
    config: ClinicConfig, schedule: dict, doctor_id: str, date_str: str,
    clinic_id: str = "default"
) -> dict:
    """Förklara varför en läkare fick ett specifikt pass."""
    doc = next((d for d in config.doctors if d.id == doctor_id), None)
    if not doc:
        return {"explanation_sv": f"Läkare {doctor_id} finns inte", "error": "not_found"}

    # Hämta tilldelningen
    sched = schedule.get("schedule", schedule)
    doc_sched = sched.get(doctor_id, {})
    assignment = doc_sched.get(date_str, "LEDIG")

    # Kontext: vad andra gör samma dag
    day_summary = {}
    for did, days in sched.items():
        func = days.get(date_str)
        if func:
            day_summary.setdefault(func, []).append(did)

    rules = [{"id": r.id, "name": r.name, "is_hard": r.is_hard} for r in config.constraint_rules if r.enabled]

    messages = [{"role": "user", "content": f"""Läkare: {doc.name} ({doc.role.value}), ID: {doctor_id}
Datum: {date_str}
Tilldelning: {assignment}

Dagsöversikt: {json.dumps(day_summary, ensure_ascii=False)}
Aktiva regler: {json.dumps(rules, ensure_ascii=False)}

Doktorn har: can_primary_call={doc.can_primary_call}, can_backup_call={doc.can_backup_call}, employment_rate={doc.employment_rate}

Förklara varför {doc.name} fick {assignment} denna dag."""}]

    result = await call_claude(SYSTEM_PROMPT, messages, clinic_id=clinic_id)

    if result.get("error"):
        return {"explanation_sv": "", "constraints_applied": [], "alternatives_considered": [], "error": result["error"]}

    try:
        text = result["text"]
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        parsed = json.loads(text[json_start:json_end])
        parsed["error"] = None
        return parsed
    except Exception as e:
        return {"explanation_sv": result.get("text", ""), "constraints_applied": [], "alternatives_considered": [], "error": str(e)}
