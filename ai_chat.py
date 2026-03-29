"""
COP AI Chat — Conversational Schedule Interface
=================================================
Chattgränssnitt där personal kan ställa frågor och göra ändringar.
"""

import json
from data_model import ClinicConfig
from ai_base import call_claude, _extract_json

SYSTEM_PROMPT = """Du är COP-assistenten — ett chattgränssnitt för klinisk schemaläggning.

Du kan hjälpa personalen med:
- Visa schema: "Vem jobbar nästa helg?", "Visa mitt schema"
- Byta pass: "Byt mitt torsdagspass med någon"
- Begära ledigt: "Kan jag ta ledigt den 15:e?"
- Generell info: "Hur många jourer har jag denna månad?"

Svara ALLTID på svenska. Var kortfattad och vänlig.

Returnera JSON:
```json
{{
  "response_sv": "Ditt svar till användaren",
  "intent": "query|swap|leave_request|info|other",
  "action": null eller {{"type": "swap|absence|query", "details": {{...}}}},
  "suggestions": ["Förslag 1", "Förslag 2"]
}}
```

Om du inte kan utföra en handling, förklara varför och ge alternativ."""


async def chat(
    config: ClinicConfig, schedule: dict, user_id: str, message: str,
    chat_history: list[dict] = None, clinic_id: str = "default"
) -> dict:
    """Hantera ett chattmeddelande."""
    doc = next((d for d in config.doctors if d.id == user_id), None)
    user_name = doc.name if doc else user_id

    # Bygg kontext
    sched = schedule.get("schedule", schedule) if schedule else {}
    user_sched = sched.get(user_id, {})
    upcoming = {k: v for k, v in sorted(user_sched.items())[:14]} if user_sched else {}

    context = f"""Användare: {user_name} (ID: {user_id})
Kommande schema: {json.dumps(upcoming, ensure_ascii=False)}
Klinik: {len(config.doctors)} läkare, sites: {config.sites}"""

    messages = []
    # Lägg till historik (senaste 5 meddelanden)
    if chat_history:
        for h in chat_history[-5:]:
            messages.append({"role": "user", "content": h.get("message", "")})
            messages.append({"role": "assistant", "content": h.get("response", "")})

    messages.append({"role": "user", "content": f"{context}\n\nMeddelande: {message}"})

    result = await call_claude(SYSTEM_PROMPT, messages, clinic_id=clinic_id)

    if result.get("error"):
        return {
            "response_sv": "AI-assistenten är tillfälligt otillgänglig. Kontakta schemaadministratören.",
            "intent": "other", "action": None, "suggestions": [],
            "error": result["error"], "fallback": True,
        }

    parsed = _extract_json(result["text"])
    if parsed is not None:
        parsed["error"] = None
        return parsed

    return {
        "response_sv": result.get("text", "Kunde inte tolka svaret"),
        "intent": "other", "action": None, "suggestions": [], "error": None,
    }
