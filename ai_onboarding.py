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
på svenska och bygga en komplett schemaläggningskonfiguration för deras verksamhet.

## Ditt mål
Samla in ALL information som krävs för att konfigurera ett AI-drivet schemasystem.
Systemet stöder ALLA typer av sjukvårdsverksamheter — inte bara kirurgi.
Fråga ett ämne i taget. Var tydlig och konkret. Ge exempel när det hjälper.

## Verksamhetstyper som stöds
- kirurgi (ortopedi, allmänkirurgi, etc.) — har OP, jour, avdelning
- internmedicin — har avdelning, rond, dialys, dagvård, jour
- vardcentral — har mottagning, BVC, MVC, lab, telefontid, hembesök, video
- oppenvard — har mottagning, dagkirurgi, ingen slutenvård
- psykiatri — har samtal, gruppterapi, akutpsyk, avdelning, jour
- rehabilitering — har rehab, arbetsterapi, fysioterapi
- radiologi — har undersökningar, granskning
- annan — fritt konfigurerbara funktioner och roller

## Information du behöver samla in

### Steg 1: Grundinfo
- Kliniknamn
- Verksamhetstyp (fråga specifikt — kirurgi, vårdcentral, internmedicin, etc.)
- Sjukhusnamn / vårdcentralens namn
- Antal sites de täcker (t.ex. "CSK + Hässleholm", "VC Söder + VC Norr")
- Restid mellan sites (om flera)

### Steg 2: Personal
- Vilka roller finns? Anpassa efter verksamhetstyp:
  - Kirurgi: ÖL, SP, ST, AT, UL
  - Vårdcentral: distriktsläkare, SSK, USK, fysioterapeut, psykolog, barnmorska, dietist, kurator
  - Internmedicin: ÖL, SP, ST, AT, SSK
  - Psykiatri: ÖL, SP, ST, psykolog, kurator, SSK
- Antal per roll
- Deltidstjänster?
- Har verksamheten jour? (hoppa över om nej)
  - Vilka roller kan gå primärjour? Bakjour?
  - Jourbefriade?

### Steg 3: Funktioner/stationer
- Anpassa efter verksamhetstyp!
  - Kirurgi: OP, avdelning, mottagning, akutmottagning
  - Vårdcentral: mottagning, BVC, MVC, lab, telefontid, hembesök, video
  - Internmedicin: avdelning, mottagning, rond, dialys, dagvård
  - Psykiatri: mottagning, samtal, gruppterapi, akutpsyk, avdelning
- Vilka funktioner finns per site?
- Har verksamheten operationssalar? (bara om relevant)
  - Antal per site, vilka dagar
- Bemanningskrav: minsta antal per funktion
- Egna funktioner som inte finns i listan? (custom_functions)

### Steg 4: Utbildning och rotation
- Anpassa efter verksamhetstyp! Inte alla har AT/ST.
- Har ni AT-läkare? Hur roterar de?
- Har ni ST-läkare? Randningsperioder? OP-krav?
- Handledningskrav?
- Andra utbildningsaktiviteter?

### Steg 5: Jourschema (HOPPA ÖVER om verksamheten inte har jour!)
- Primärjour: vem kan gå? Schema?
- Bakjour: finns det? Vem?
- Max jourer per vecka/månad?
- Helgjour: hur ofta? Kompensation?
- Vila efter jour?

### Steg 6: Specialregler
- Fasta dagar per person?
- Halvdagar (FM/EM)?
- Varannan vecka-mönster?
- Konsultschema?
- Fasta aktiviteter (ronder, konferenser, MDT)?
- Semesterregler?

### Steg 7: Sammanfattning
- Presentera allt du samlat in
- Fråga om det stämmer
- Specifika önskemål eller regler?
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
    "clinic_type": "kirurgi|internmedicin|vardcentral|oppenvard|psykiatri|rehabilitering|radiologi|annan",
    "has_on_call": true/false,
    "has_operations": true/false,
    "sites": [...],
    "roles_needed": [...],
    "custom_roles": [],
    "functions": [...],
    "custom_functions": [],
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
- Anpassa frågorna efter verksamhetstyp! Fråga inte om OP-salar på en vårdcentral.
- Ge konkreta exempel anpassade efter deras verksamhetstyp
- Om admin säger "standard" eller "vanligt" — berätta vad du antar och fråga om det stämmer
- Var pragmatisk — om något inte är relevant för deras verksamhet, hoppa över det
- Sammanfatta vad du förstått innan du går vidare till nästa steg
- Sätt has_on_call=false för verksamheter utan jour (t.ex. de flesta vårdcentraler)
- Sätt has_operations=false för verksamheter utan operationsverksamhet
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
- clinic_type ("kirurgi", "internmedicin", "vardcentral", "oppenvard", "psykiatri", "rehabilitering", "radiologi", "annan")
- has_on_call (true/false — false för verksamheter utan jourverksamhet)
- has_operations (true/false — false för verksamheter utan operationsverksamhet)
- doctors (med korrekta roller, can_primary_call, can_backup_call, etc.)
- operating_rooms (per site — tom lista om has_operations=false)
- staffing_requirements (min bemanning per funktion/site)
- call_structure (max jourer, helgfrekvens, etc. — standardvärden om has_on_call=false)
- constraint_rules (standardregler + eventuella klinikspecifika)
- custom_roles (lista av klinikdefinierade roller utöver standardroller)
- custom_functions (lista av klinikdefinierade funktioner utöver standardfunktioner)
- schedule_cycle_weeks
- travel_time_between_sites_min

Returnera BARA JSON, inget annat.
Inkludera ALLA doktor-fält som behövs baserat på deras roll:
- AT-läkare: at_weekly_rotation, at_rotation_period
- ST-läkare: st_randning, st_min_op_days, supervisor_id
- Seniorer: backup_call_config, consultation_schedule, op_pairing

VIKTIGT: Anpassa config efter clinic_type!
- Vårdcentral: has_operations=false, has_on_call=false (normalt), BVC/MVC/TELEFON-funktioner
- Internmedicin: has_operations=false (normalt), ROND/DIALYS/DAGVÅRD-funktioner
- Psykiatri: has_operations=false, SAMTAL/GRUPPTERAPI/AKUTPSYK-funktioner
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
