"""
COP AI Conflicts — Rule Conflict Detection
============================================
Identifierar konflikter mellan befintliga och nya regler.
"""

import json
from data_model import ClinicConfig, config_to_dict
from ai_base import call_claude, _extract_json

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
        return _rule_based_conflicts(existing, new_rule)

    parsed = _extract_json(result["text"])
    if parsed is not None:
        parsed["error"] = None
        return parsed

    return _rule_based_conflicts(existing, new_rule)


def _rule_based_conflicts(existing: list[dict], new_rule: dict) -> dict:
    """
    Regelbaserad fallback för konfliktdetektering utan AI.
    Kollar kategori-överlapp och doctor_ids + shift_types-kollisioner.
    """
    conflicts = []
    new_params = new_rule.get("parameters", {})
    new_doctors = set(new_params.get("doctor_ids", []))
    new_shifts = set(new_params.get("shift_types", []))
    new_cat = new_rule.get("category", "")
    new_is_hard = new_rule.get("is_hard", False)

    # Varning: multipla hårda ATL-regler kan begränsa schemat kraftigt
    if new_cat == "atl" and new_is_hard:
        hard_atl = [r for r in existing if r.get("category") == "atl" and r.get("is_hard")]
        if hard_atl:
            conflicts.append({
                "rule_id": hard_atl[0]["id"],
                "description": f"Flera hårda ATL-regler kan göra schemat svårlöst. Kontrollera med {hard_atl[0]['name']}.",
                "severity": "medium",
            })

    # Direkt kollision: samma läkare + samma skift-typ + hård motstridig regel
    if new_doctors and new_shifts:
        for r in existing:
            if not r.get("is_hard"):
                continue
            rp = r.get("parameters", {})
            overlap_docs = new_doctors & set(rp.get("doctor_ids", []))
            overlap_shifts = new_shifts & set(rp.get("shift_types", []))
            if overlap_docs and overlap_shifts:
                conflicts.append({
                    "rule_id": r["id"],
                    "description": (
                        f"Potentiell kollision med '{r['name']}' för "
                        f"läkare {sorted(overlap_docs)} på skift {sorted(overlap_shifts)}."
                    ),
                    "severity": "high",
                })

    feasibility = "ok"
    if any(c["severity"] == "high" for c in conflicts):
        feasibility = "warning"

    return {
        "conflicts": conflicts,
        "feasibility": feasibility,
        "suggestions_sv": ["Granska regeln manuellt — AI-analys ej tillgänglig."] if conflicts else [],
        "fallback": True,
    }
