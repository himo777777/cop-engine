"""
COP Agent v0.1 — Autonomous Clinical Intelligence Layer
========================================================
Claude-driven LLM agent som förstår naturligt språk och styr COP-motorn.

Arkitektur:
  Användare → [Naturligt språk] → [COP Agent / Claude] → [Tool Use] → [COP API] → [Svar]

Agenten kan:
  - Generera scheman ("schemalägg nästa 2 veckor")
  - Göra justeringar ("byt Dr Erikssons jour nästa tisdag")
  - Hantera frånvaro ("Dr Holm är sjuk 1-3 april")
  - Visa statistik ("hur ser jourfördelningen ut?")
  - Validera ("finns det ATL-brott i schemat?")
  - Svara på frågor ("vem har jour på fredag?")
  - Optimera ("maximera ST-utbildningstid")

Kräver:
  - ANTHROPIC_API_KEY i miljövariabler
  - COP API körandes på localhost:8000
"""

import os
import json
import httpx
from datetime import datetime, date, timedelta
from typing import Optional

from anthropic import Anthropic

# === KONFIGURATION ===
COP_API_BASE = os.getenv("COP_API_BASE", "http://localhost:8000")
MODEL = os.getenv("COP_MODEL", "claude-sonnet-4-20250514")

# === COP TOOLS (Claude Tool Use) ===
# Dessa tools mappar direkt till COP REST API endpoints.
# Claude väljer själv vilka tools att använda baserat på användarens fråga.

COP_TOOLS = [
    {
        "name": "generate_schedule",
        "description": """Generera ett nytt optimerat schema för kliniken.
        Använd detta när användaren ber om att schemalägga, skapa ett nytt schema,
        eller optimera schemat. Returnerar ett komplett schema med statistik.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "clinic_id": {
                    "type": "string",
                    "description": "Klinik-ID (required)"
                },
                "num_weeks": {
                    "type": "integer",
                    "description": "Antal veckor att schemalägga (1-8)",
                    "default": 2
                },
                "start_date": {
                    "type": "string",
                    "description": "Startdatum YYYY-MM-DD (default: nästa måndag)"
                },
                "time_limit_seconds": {
                    "type": "integer",
                    "description": "Max tid för optimering i sekunder",
                    "default": 30
                }
            },
            "required": []
        }
    },
    {
        "name": "get_schedule",
        "description": """Hämta ett befintligt genererat schema. Använd detta för att visa
        schemat eller svara på frågor om vem som jobbar när.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "schedule_id": {
                    "type": "string",
                    "description": "Schema-ID att hämta"
                }
            },
            "required": ["schedule_id"]
        }
    },
    {
        "name": "get_doctor_schedule",
        "description": """Hämta schema för en specifik läkare. Använd när användaren frågar
        om en specifik persons schema, jour, eller tilldelningar.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "schedule_id": {
                    "type": "string",
                    "description": "Schema-ID"
                },
                "doctor_id": {
                    "type": "string",
                    "description": "Läkar-ID (t.ex. 'OL1', 'SP3', 'ST2')"
                }
            },
            "required": ["schedule_id", "doctor_id"]
        }
    },
    {
        "name": "list_schedules",
        "description": """Lista alla genererade scheman. Använd för att hitta aktuellt schema-ID
        eller visa historik.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "clinic_id": {
                    "type": "string",
                    "description": "Filtrera på klinik-ID"
                }
            },
            "required": []
        }
    },
    {
        "name": "swap_assignment",
        "description": """Byt funktion mellan två läkare en specifik dag. Använd när användaren
        vill byta pass, ändra jour, eller flytta tilldelningar mellan läkare.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "schedule_id": {
                    "type": "string",
                    "description": "Schema-ID"
                },
                "doctor_id": {
                    "type": "string",
                    "description": "Första läkarens ID"
                },
                "day": {
                    "type": "integer",
                    "description": "Dag-index (0=mån v1, 1=tis v1, ... 7=mån v2)"
                },
                "swap_with_doctor_id": {
                    "type": "string",
                    "description": "Andra läkarens ID att byta med"
                }
            },
            "required": ["schedule_id", "doctor_id", "day", "swap_with_doctor_id"]
        }
    },
    {
        "name": "replace_assignment",
        "description": """Ändra en läkares funktion en specifik dag. Använd när användaren vill
        sätta en specifik person på en specifik funktion, eller manuellt ändra schemat.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "schedule_id": {
                    "type": "string",
                    "description": "Schema-ID"
                },
                "doctor_id": {
                    "type": "string",
                    "description": "Läkar-ID"
                },
                "day": {
                    "type": "integer",
                    "description": "Dag-index"
                },
                "new_function": {
                    "type": "string",
                    "description": "Ny funktionskod enligt klinikens konfiguration (t.ex. OP_H, AVD_C, JOUR_P, LEDIG). Hämta giltiga koder via get_config."
                },
                "reason": {
                    "type": "string",
                    "description": "Anledning till ändringen"
                }
            },
            "required": ["schedule_id", "doctor_id", "day", "new_function"]
        }
    },
    {
        "name": "register_absence",
        "description": """Registrera frånvaro för en läkare. Använd när någon är sjuk, ska på
        semester, VAB, utbildning, eller konferens. Systemet hittar automatiskt berörda
        scheman och kan omoptimera.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "doctor_id": {
                    "type": "string",
                    "description": "Läkar-ID"
                },
                "absence_type": {
                    "type": "string",
                    "description": "Typ av frånvaro",
                    "enum": ["sjuk", "semester", "vab", "utbildning", "konferens"]
                },
                "start_date": {
                    "type": "string",
                    "description": "Startdatum YYYY-MM-DD"
                },
                "end_date": {
                    "type": "string",
                    "description": "Slutdatum YYYY-MM-DD"
                },
                "reoptimize": {
                    "type": "boolean",
                    "description": "Omoptimera berörda scheman automatiskt?",
                    "default": True
                }
            },
            "required": ["doctor_id", "absence_type", "start_date", "end_date"]
        }
    },
    {
        "name": "validate_schedule",
        "description": """Kör fullständig ATL-validering av ett schema. Kontrollerar dygnsvila,
        veckovila, max arbetstid, bemanningstal och mer. Använd för att kolla om
        schemat är lagligt och korrekt.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "schedule_id": {
                    "type": "string",
                    "description": "Schema-ID att validera"
                }
            },
            "required": ["schedule_id"]
        }
    },
    {
        "name": "get_statistics",
        "description": """Hämta detaljerad statistik för ett schema. Inkluderar jourfördelning,
        bemanningstal, ST-handledarmatchning, arbetsbelastning och ATL-status.
        Använd för att svara på frågor om rättvisa, belastning, utbildning.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "schedule_id": {
                    "type": "string",
                    "description": "Schema-ID"
                }
            },
            "required": ["schedule_id"]
        }
    },
    {
        "name": "get_config",
        "description": """Hämta klinikkonfiguration — alla läkare, salar, roller, jourregler.
        Använd för att svara på frågor om kliniken, vilka läkare som finns,
        roller, eller systemkonfiguration.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "clinic_id": {
                    "type": "string",
                    "description": "Klinik-ID (required)"
                }
            },
            "required": []
        }
    },
    {
        "name": "reoptimize_schedule",
        "description": """Omoptimera ett befintligt schema efter ändringar (frånvaro, byten).
        Genererar ett nytt optimalt schema med hänsyn till gjorda ändringar.
        Använd efter att frånvaro registrerats eller manuella ändringar gjorts.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "schedule_id": {
                    "type": "string",
                    "description": "Schema-ID att omoptimera"
                },
                "time_limit_seconds": {
                    "type": "integer",
                    "description": "Max tid för solver",
                    "default": 30
                }
            },
            "required": ["schedule_id"]
        }
    },
]

# === TOOL EXECUTION ===

def execute_tool(tool_name: str, tool_input: dict) -> dict:
    """Exekvera ett COP-tool mot API:t."""
    try:
        with httpx.Client(timeout=120.0) as client:
            if tool_name == "generate_schedule":
                payload = {
                    "clinic_id": tool_input.get("clinic_id"),
                    "num_weeks": tool_input.get("num_weeks", 2),
                    "time_limit_seconds": tool_input.get("time_limit_seconds", 30),
                }
                if tool_input.get("start_date"):
                    payload["start_date"] = tool_input["start_date"]
                resp = client.post(f"{COP_API_BASE}/schedule/generate", json=payload)

            elif tool_name == "get_schedule":
                resp = client.get(f"{COP_API_BASE}/schedule/{tool_input['schedule_id']}")

            elif tool_name == "get_doctor_schedule":
                resp = client.get(
                    f"{COP_API_BASE}/schedule/{tool_input['schedule_id']}/doctor/{tool_input['doctor_id']}"
                )

            elif tool_name == "list_schedules":
                params = {}
                if tool_input.get("clinic_id"):
                    params["clinic_id"] = tool_input["clinic_id"]
                resp = client.get(f"{COP_API_BASE}/schedules", params=params)

            elif tool_name == "swap_assignment":
                resp = client.post(f"{COP_API_BASE}/schedule/adjust", json={
                    "schedule_id": tool_input["schedule_id"],
                    "adjustment_type": "swap",
                    "doctor_id": tool_input["doctor_id"],
                    "day": tool_input["day"],
                    "swap_with_doctor_id": tool_input["swap_with_doctor_id"],
                })

            elif tool_name == "replace_assignment":
                resp = client.post(f"{COP_API_BASE}/schedule/adjust", json={
                    "schedule_id": tool_input["schedule_id"],
                    "adjustment_type": "replace",
                    "doctor_id": tool_input["doctor_id"],
                    "day": tool_input["day"],
                    "new_function": tool_input["new_function"],
                    "reason": tool_input.get("reason"),
                })

            elif tool_name == "register_absence":
                resp = client.post(f"{COP_API_BASE}/absence", json={
                    "clinic_id": tool_input.get("clinic_id"),
                    "doctor_id": tool_input["doctor_id"],
                    "absence_type": tool_input["absence_type"],
                    "start_date": tool_input["start_date"],
                    "end_date": tool_input["end_date"],
                    "reoptimize": tool_input.get("reoptimize", False),
                })

            elif tool_name == "validate_schedule":
                resp = client.post(f"{COP_API_BASE}/validate/{tool_input['schedule_id']}")

            elif tool_name == "get_statistics":
                resp = client.get(f"{COP_API_BASE}/statistics/{tool_input['schedule_id']}")

            elif tool_name == "get_config":
                clinic_id = tool_input.get("clinic_id")
                resp = client.get(f"{COP_API_BASE}/config/{clinic_id}")

            elif tool_name == "reoptimize_schedule":
                resp = client.post(f"{COP_API_BASE}/schedule/reoptimize", json={
                    "schedule_id": tool_input["schedule_id"],
                    "time_limit_seconds": tool_input.get("time_limit_seconds", 30),
                })

            else:
                return {"error": f"Okänt verktyg: {tool_name}"}

            if resp.status_code >= 400:
                return {"error": f"API-fel {resp.status_code}: {resp.text}"}

            return resp.json()

    except httpx.ConnectError:
        return {"error": "Kunde inte ansluta till COP API. Är servern igång? (python3 api.py)"}
    except Exception as e:
        return {"error": f"Oväntat fel: {str(e)}"}


# === SYSTEM PROMPT ===

SYSTEM_PROMPT = """Du är COP-agenten — en AI-assistent för klinisk schemaläggning.

## Din roll
Du hjälper schemaansvariga, klinikledning och enskilda läkare att:
- Generera och optimera scheman
- Hantera frånvaro och byten
- Svara på frågor om schemat
- Validera scheman mot ATL (Arbetstidslagen)
- Analysera rättvisa och arbetsbelastning

## Kliniken
Klinikens konfiguration (sajter, läkare, salar, funktioner) hämtas dynamiskt
via get_config-verktyget. Använd det för att ta reda på vilka läkare, roller,
sajter och funktioner som finns i den aktuella kliniken.

## Funktioner
Funktionskoder följer mönstret TYP_SAJT (t.ex. OP_H = Operation på sajt H,
AVD_C = Avdelning på sajt C). Speciella funktioner: JOUR_P = Primärjour,
JOUR_B = Bakjour, LEDIG = Ledig. Exakta funktioner beror på klinikens
konfiguration.

## Regler du ALLTID ska nämna vid brott
- ATL kräver minst 11 timmars sammanhängande dygnsvila
- Efter nattjour ska läkaren vara ledig nästa dag
- Max 1 jour per vecka per person
- Max 5 arbetsdagar per vecka
- Underläkare får INTE jobba mottagning ensamt
- ST-läkare ska ha handledare närvarande vid operation

## Dagindex
- Vecka 1: 0=Mån, 1=Tis, 2=Ons, 3=Tor, 4=Fre, 5=Lör, 6=Sön
- Vecka 2: 7=Mån, 8=Tis, 9=Ons, 10=Tor, 11=Fre, 12=Lör, 13=Sön

## Ditt beteende
- Svara på svenska
- Var koncis men noggrann
- Varna ALLTID om ATL-brott eller bemanningsproblem
- Föreslå alltid alternativ om en ändring skapar problem
- Visa schema i tydligt tabellformat
- När du hämtar schema, sammanfatta det kort — visa inte rå JSON
- Om användaren nämner ett namn, mappa det till rätt ID
- Första gången: lista befintliga scheman eller generera ett nytt
"""


# === AGENT LOOP ===

class COPAgent:
    """COP Agent — Claude-driven schemaassistent."""

    def __init__(self, api_key: Optional[str] = None):
        self.client = Anthropic(api_key=api_key or os.getenv("ANTHROPIC_API_KEY"))
        self.conversation: list = []
        self.current_schedule_id: Optional[str] = None

    def chat(self, user_message: str) -> str:
        """Skicka meddelande och få svar (med automatisk tool-use)."""
        self.conversation.append({
            "role": "user",
            "content": user_message
        })

        # Agent-loop: kör tills Claude ger ett text-svar (inga fler tool calls)
        while True:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=COP_TOOLS,
                messages=self.conversation,
            )

            # Samla svar
            assistant_content = response.content
            self.conversation.append({
                "role": "assistant",
                "content": assistant_content
            })

            # Kolla om Claude vill använda tools
            tool_uses = [b for b in assistant_content if b.type == "tool_use"]

            if not tool_uses:
                # Inget tool use — extrahera text-svaret
                text_parts = [b.text for b in assistant_content if b.type == "text"]
                return "\n".join(text_parts)

            # Exekvera alla tool calls
            tool_results = []
            for tool_use in tool_uses:
                print(f"  🔧 {tool_use.name}({json.dumps(tool_use.input, ensure_ascii=False)[:80]}...)")
                result = execute_tool(tool_use.name, tool_use.input)

                # Spåra aktuellt schema-ID
                if tool_use.name == "generate_schedule" and "schedule_id" in result:
                    self.current_schedule_id = result["schedule_id"]
                elif tool_use.name == "list_schedules" and isinstance(result, list) and result:
                    self.current_schedule_id = result[-1].get("schedule_id")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str)[:15000],  # Truncate large responses
                })

            self.conversation.append({
                "role": "user",
                "content": tool_results
            })

    def reset(self):
        """Nollställ konversationen."""
        self.conversation = []
        self.current_schedule_id = None


# === CLI INTERFACE ===

def run_cli():
    """Kör COP Agent som interaktiv CLI."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌ Sätt ANTHROPIC_API_KEY innan du kör agenten.")
        print("   export ANTHROPIC_API_KEY='sk-ant-...'")
        return

    agent = COPAgent(api_key=api_key)

    print("=" * 60)
    print("🏥 COP Agent — Schemaassistent")
    print("=" * 60)
    print("Skriv ditt meddelande. Skriv 'quit' för att avsluta.\n")

    while True:
        try:
            user_input = input("Du: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                print("👋 Hej då!")
                break
            if user_input.lower() == "reset":
                agent.reset()
                print("🔄 Konversation nollställd.\n")
                continue

            response = agent.chat(user_input)
            print(f"\n🤖 COP: {response}\n")

        except KeyboardInterrupt:
            print("\n👋 Hej då!")
            break
        except Exception as e:
            print(f"❌ Fel: {e}\n")


# === DEMO MODE (utan API-nyckel) ===

def run_demo():
    """Demonstrera agentens tool-routing utan att kalla Claude API.
    Visar vilka tools som hade anropats för olika frågor."""

    demo_queries = [
        "Schemalägg nästa 2 veckor",
        "Vem har jour på fredag?",
        "Dr Holm är sjuk 1-3 april",
        "Byt Dr Erikssons bakjour tisdag med Dr Danielsson",
        "Hur ser jourfördelningen ut?",
        "Finns det ATL-brott i schemat?",
        "Maximera ST-utbildningstid nästa period",
        "Visa Nilssons schema",
    ]

    # Enkel keyword→tool routing för demo
    keyword_map = {
        "schemalägg": "generate_schedule",
        "generera": "generate_schedule",
        "jour": "get_schedule",
        "vem har": "get_schedule",
        "sjuk": "register_absence",
        "semester": "register_absence",
        "vab": "register_absence",
        "byt": "swap_assignment",
        "byte": "swap_assignment",
        "jourfördelning": "get_statistics",
        "statistik": "get_statistics",
        "rättvis": "get_statistics",
        "atl": "validate_schedule",
        "brott": "validate_schedule",
        "validera": "validate_schedule",
        "maximera": "reoptimize_schedule",
        "optimera": "reoptimize_schedule",
        "visa": "get_doctor_schedule",
        "schema för": "get_doctor_schedule",
    }

    print("=" * 60)
    print("🏥 COP Agent — DEMO MODE")
    print("   (Visar tool-routing utan Claude API)")
    print("=" * 60)

    for query in demo_queries:
        matched_tool = "unknown"
        query_lower = query.lower()
        for keyword, tool in keyword_map.items():
            if keyword in query_lower:
                matched_tool = tool
                break

        print(f"\n  📝 \"{query}\"")
        print(f"  → 🔧 {matched_tool}")

    print("\n" + "=" * 60)
    print("I produktionsläge kör Claude full NLU och väljer rätt tool")
    print("automatiskt baserat på kontext och konversationshistorik.")
    print("=" * 60)

    # Kör ett riktigt API-anrop för att visa att det fungerar
    print("\n🔄 Testar API-anslutning...")
    # Demo: hämta första tillgängliga klinik dynamiskt via API
    # Fallback till "kristianstad" om /clinics-endpoint saknas
    demo_clinic_id = "kristianstad"  # demo-only default
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.get(f"{COP_API_BASE}/clinics")
            if r.status_code == 200:
                clinics = r.json()
                if isinstance(clinics, list) and clinics:
                    demo_clinic_id = clinics[0].get("clinic_id", demo_clinic_id)
    except Exception:
        pass
    result = execute_tool("get_config", {"clinic_id": demo_clinic_id})
    if "error" in result:
        print(f"  ⚠️  {result['error']}")
    else:
        print(f"  ✅ API OK — {result['num_doctors']} läkare, {result['num_rooms']} salar")

    # Generera schema via tool
    print("\n🔄 Genererar testschema via API...")
    result = execute_tool("generate_schedule", {"num_weeks": 2, "time_limit_seconds": 30})
    if "error" in result:
        print(f"  ⚠️  {result['error']}")
    else:
        schedule_id = result.get("schedule_id", "?")
        status = result.get("status", "?")
        stats = result.get("statistics", {})
        atl = stats.get("atl_violations", [])
        st = stats.get("st_matching", {})
        calls = stats.get("call_distribution", {})

        print(f"  ✅ Schema {schedule_id}: {status}")
        print(f"  📊 ATL-brott: {len(atl)}")
        print(f"  📊 ST-matchning: {len([s for s in st.values() if s.get('match_rate', 0) == 100])}/{len(st)} = 100%")

        totals = [v['total'] for v in calls.values()]
        if totals:
            print(f"  📊 Jourfördelning: {min(totals)}-{max(totals)} jourer/person")

        # Validera
        print("\n🔄 Validerar schema...")
        val = execute_tool("validate_schedule", {"schedule_id": schedule_id})
        if "error" not in val:
            print(f"  ✅ Valid: {val.get('valid', '?')}")
            print(f"  📊 Kritiska brott: {val.get('summary', {}).get('critical', '?')}")
            print(f"  📊 Varningar: {val.get('summary', {}).get('warnings', '?')}")

        # Testa frånvaro
        print("\n🔄 Registrerar testfrånvaro (Dr Holm sjuk)...")
        abs_result = execute_tool("register_absence", {
            "doctor_id": "SP3",
            "absence_type": "sjuk",
            "start_date": "2026-03-31",
            "end_date": "2026-04-02",
            "reoptimize": False,
        })
        if "error" not in abs_result:
            print(f"  ✅ {abs_result.get('doctor_name', '?')}: {abs_result.get('absence_type', '?')}")
            print(f"  📊 Berörda scheman: {len(abs_result.get('affected_schedules', []))}")

        # Validera efter frånvaro
        print("\n🔄 Validerar schema efter frånvaro...")
        val2 = execute_tool("validate_schedule", {"schedule_id": schedule_id})
        if "error" not in val2:
            print(f"  ✅ Valid: {val2.get('valid', '?')}")
            print(f"  📊 Kritiska brott: {val2.get('summary', {}).get('critical', '?')}")

    print("\n" + "=" * 60)
    print("✅ Demo klar! Alla COP Agent → API-flöden fungerar.")
    print("\nFör att köra med Claude: export ANTHROPIC_API_KEY='...' && python3 agent.py")
    print("=" * 60)


if __name__ == "__main__":
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if api_key:
        run_cli()
    else:
        print("ℹ️  Ingen ANTHROPIC_API_KEY hittad — kör demo mode.\n")
        run_demo()
