"""
COP AI Onboarding — Klinik-konfiguration via konversation
============================================================
AI:n intervjuar klinikadmin på svenska och bygger en komplett
ClinicConfig steg för steg. Ingen UI behöver förutse vilka
frågor som är relevanta — AI:n anpassar sig efter kliniktyp.

Flöde:
  1. Admin startar onboarding → AI frågar om kliniktyp
  2. AI ställer följdfrågor baserat på svar (roller, sites, funktioner, regler)
  3. Admin svarar på svenska → AI bygger config iterativt
  4. AI presenterar sammanfattning → admin godkänner → config sparas
"""

import json
from ai_base import call_claude, _extract_json
from data_model import default_constraint_rules

SYSTEM_PROMPT = """Du är COP Onboarding-assistenten. Din uppgift: intervjua en klinikadmin
på svenska och bygga en komplett schemaläggningskonfiguration för deras klinik.

## Ditt mål
Samla in ALL information som krävs för att konfigurera ett AI-drivet schemasystem.
Fråga ett ämne i taget. Var tydlig och konkret. Ge exempel när det hjälper.

## Information du behöver samla in

### Steg 1: Grundinfo
- Kliniknamn och specialitet (ortopedi, medicin, kirurgi, anestesi, etc.)
- Sjukhusnamn
- Antal sites/sjukhus de täcker (t.ex. "CSK + Hässleholm")
- Restid mellan sites (om flera)

### Steg 2: Personal
- Vilka roller finns? (ÖL, SP, ST-sen, ST-tidig, AT, UL, andra?)
- Ungefärligt antal per roll
- Vilka roller kan gå primärjour? Bakjour?
- Finns jourbefriade läkare?
- Deltidstjänster?

### Steg 3: Funktioner/stationer
- Vilka funktioner bemannas? (OP, avdelning, mottagning, akut, konsult, rond, etc.)
- Vilka funktioner finns per site?
- Operationssalar: antal per site, vilka dagar
- Bemanningskrav: minsta antal per funktion

### Steg 4: Utbildning och rotation
- Har ni AT-läkare? Hur roterar de? (block, veckoschema, fritt?)
- Har ni ST-läkare? Randningsperioder? OP-krav?
- Handledningskrav? Senior/junior-parning på OP?
- Andra utbildningsaktiviteter?

### Steg 5: Jourschema
- Primärjour: vem kan gå? Schema (dygn, kväll+natt, delat?)
- Bakjour: finns det? Vem?
- Max jourer per vecka/månad?
- Helgjour: hur ofta? Kompensation?
- Vila efter jour: hur lång? (ATL kräver 11h, men lokala avtal?)

### Steg 6: Specialregler
- Fasta dagar (t.ex. "Dr X opererar alltid tisdagar")?
- Halvdagar (t.ex. "FM mottagning, EM admin")?
- Varannan vecka-mönster?
- Konsultschema (andra avdelningar ringer in)?
- Fasta aktiviteter (ronder, konferenser, MDT)?
- Semesterregler?

### Steg 7: Övrigt
- Specifika önskemål eller regler som inte passar ovan?
- Integration med befintligt schemasystem (Tessa, Time Care, Heroma)?

## Output-format
Svara ALLTID med JSON:
```json
{
  "message_sv": "Ditt meddelande till admin (svenska, vänligt, konkret)",
  "current_step": 1-7,
  "is_complete": false,
  "config_so_far": {
    "name": "...",
    "sites": [...],
    "roles_needed": [...],
    "functions": [...],
    "rules_identified": [...],
    ...övriga fält du samlat in...
  },
  "missing_info": ["Kort lista av vad som saknas"],
  "confidence": 0.0-1.0
}
```

När is_complete=true, inkludera "final_config" med en komplett ClinicConfig-dict.

## Viktigt
- Fråga ETT ämne i taget — inte allt på en gång
- Ge konkreta exempel: "T.ex. har ni roller som ÖL, specialist, ST?"
- Om admin säger "standard" eller "vanligt" — berätta vad du antar och fråga om det stämmer
- Var pragmatisk — om något inte är relevant för deras klinik, hoppa över det
- Sammanfatta vad du förstått innan du går vidare till nästa steg
"""


async def onboard_step(
    message: str,
    chat_history: list[dict] = None,
    partial_config: dict = None,
) -> dict:
    """
    Hantera ett steg i onboarding-konversationen.

    Args:
        message: Adminens senaste svar
        chat_history: Tidigare meddelanden [{"role": "user/assistant", "content": "..."}]
        partial_config: Config som byggts upp hittills

    Returns:
        {
            "message_sv": str,       # AI:ns svar till admin
            "current_step": int,     # 1-7
            "is_complete": bool,     # True = redo att generera config
            "config_so_far": dict,   # Partiell config
            "missing_info": list,    # Vad som saknas
            "confidence": float,     # 0-1
            "final_config": dict|None  # Komplett config om is_complete
        }
    """
    messages = []

    # Inkludera kontext
    context = ""
    if partial_config:
        context = f"\n\nHittills insamlad konfiguration:\n{json.dumps(partial_config, ensure_ascii=False, indent=2)}"

    # Bygg historik
    if chat_history:
        for h in chat_history:
            messages.append({"role": h["role"], "content": h["content"]})

    messages.append({"role": "user", "content": f"{message}{context}"})

    result = await call_claude(SYSTEM_PROMPT, messages)

    try:
        parsed = _extract_json(result)
        return parsed
    except Exception:
        return {
            "message_sv": result if isinstance(result, str) else str(result),
            "current_step": 1,
            "is_complete": False,
            "config_so_far": partial_config or {},
            "missing_info": [],
            "confidence": 0.0,
            "final_config": None,
        }


async def generate_clinic_config(onboarding_result: dict) -> dict:
    """
    Generera en komplett ClinicConfig-dict från onboarding-resultatet.
    Kallas när is_complete=True.
    """
    config_data = onboarding_result.get("final_config") or onboarding_result.get("config_so_far", {})

    prompt = f"""Konvertera denna klinikkonfiguration till ett komplett ClinicConfig JSON-objekt
som kan användas direkt i COP-schemasystemet.

Insamlad data:
{json.dumps(config_data, ensure_ascii=False, indent=2)}

Standardregler som alltid ska inkluderas:
{json.dumps([{{"id": r.id, "name": r.name, "category": r.category, "is_hard": r.is_hard, "weight": r.weight}} for r in default_constraint_rules()], ensure_ascii=False, indent=2)}

Generera en komplett config med:
- name, sites
- doctors (med korrekta roller, can_primary_call, can_backup_call, etc.)
- operating_rooms (per site)
- staffing_requirements (min bemanning per funktion/site)
- call_structure (max jourer, helgfrekvens, etc.)
- constraint_rules (standardregler + eventuella klinikspecifika)
- schedule_cycle_weeks
- travel_time_between_sites_min

Returnera BARA JSON, inget annat.
Inkludera ALLA doktor-fält som behövs baserat på deras roll:
- AT-läkare: at_weekly_rotation, at_rotation_period
- ST-läkare: st_randning, st_min_op_days, supervisor_id
- Seniorer: backup_call_config, consultation_schedule, op_pairing
"""

    result = await call_claude(
        "Du är en konfigurationsgenerator. Returnera BARA valid JSON.",
        [{"role": "user", "content": prompt}],
    )

    try:
        return _extract_json(result)
    except Exception:
        return {"error": "Kunde inte generera config", "raw": str(result)}


async def analyze_config_gaps(config_dict: dict) -> dict:
    """
    Analysera en befintlig config och identifiera saknade/ofullständiga delar.
    Användbar för att hitta "vad har vi missat?"-scenarion.
    """
    prompt = f"""Analysera denna klinikkonfiguration och identifiera:

1. SAKNADE REGLER: Vilka vanliga schemaläggningsregler saknas?
2. OFULLSTÄNDIG DATA: Vilka läkare saknar viktig info?
3. INKONSISTENSER: Konflikter eller saker som inte hänger ihop?
4. FÖRBÄTTRINGAR: Vad skulle göra schemat bättre?

Config:
{json.dumps(config_dict, ensure_ascii=False, indent=2)}

Svara med JSON:
{{
  "missing_rules": [{{"description": "...", "severity": "critical|important|nice_to_have", "suggestion": "..."}}],
  "incomplete_doctors": [{{"doctor_id": "...", "missing_fields": ["..."], "suggestion": "..."}}],
  "inconsistencies": [{{"description": "...", "suggestion": "..."}}],
  "improvements": [{{"description": "...", "impact": "high|medium|low"}}],
  "overall_score": 0-100,
  "summary_sv": "Sammanfattning på svenska"
}}
"""

    result = await call_claude(
        "Du är en klinisk schemaexpert. Analysera konfigurationen noggrant.",
        [{"role": "user", "content": prompt}],
    )

    try:
        return _extract_json(result)
    except Exception:
        return {"error": "Kunde inte analysera config", "raw": str(result)}
