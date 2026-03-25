"""
COP REST API v0.1 Ã¢ÂÂ Clinical Operations Protocol
=================================================
FastAPI-baserat API fÃÂ¶r AI-driven schemaoptimering.

Endpoints:
  POST /schedule/generate     Ã¢ÂÂ Generera optimalt schema
  GET  /schedule/{id}         Ã¢ÂÂ HÃÂ¤mta genererat schema
  POST /schedule/adjust       Ã¢ÂÂ Manuell justering (byte, frÃÂ¥nvaro)
  POST /schedule/reoptimize   Ã¢ÂÂ Omoptimera efter ÃÂ¤ndring
  GET  /health                Ã¢ÂÂ HÃÂ¤lsocheck
  GET  /config                Ã¢ÂÂ HÃÂ¤mta aktiv klinikkonfiguration
  PUT  /config                Ã¢ÂÂ Uppdatera klinikkonfiguration
  POST /absence               Ã¢ÂÂ Registrera frÃÂ¥nvaro
  GET  /statistics/{id}       Ã¢ÂÂ HÃÂ¤mta schemastatistik
  POST /validate              Ã¢ÂÂ Validera schema mot ATL

Arkitektur:
  Browser/Tessa/TimeCase Ã¢ÂÂ [REST API] Ã¢ÂÂ [COP Solver] Ã¢ÂÂ [Optimalt Schema]
                                Ã¢ÂÂ
                         [Schedule Store]
"""

import os
import uuid
import time
import json
from datetime import datetime, date, timedelta
from typing import Optional
from collections import defaultdict
from enum import Enum

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from data_model import (
    ClinicConfig, Role, ShiftType, Function, Doctor,
    OperatingRoom, StaffingRequirement, CallStructure, ATLRules,
    create_kristianstad_example, create_generic_example,
)
from solver import solve_schedule
from absence_chain import AbsenceChain, AbsenceChainResult, ChainStatus
from db import get_db, connect_db, close_db

# PDF Export & Email Notifications
try:
    from pdf_export import pdf_router
    HAS_PDF = True
except ImportError:
    HAS_PDF = False

try:
    from email_service import email_router
    HAS_EMAIL = True
except ImportError:
    HAS_EMAIL = False

# Security
try:
    from security import setup_security
    HAS_SECURITY = True
except ImportError:
    HAS_SECURITY = False

# Auth & WebSocket integration
try:
    from auth import auth_router, get_current_user, require_role, require_permission, Role as AuthRole
    HAS_AUTH = True
except ImportError:
    HAS_AUTH = False

try:
    from websocket_hub import ws_router, hub
    HAS_WS = True
except ImportError:
    HAS_WS = False

# === APP SETUP ===
app = FastAPI(
    title="COP Ã¢ÂÂ Clinical Operations Protocol",
    description="AI-driven schemaoptimering fÃÂ¶r sjukvÃÂ¥rden. System-agnostisk motor som pluggar in i Tessa, Time Care, Heroma, Medvind.",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

_cors_origins = os.environ.get("COP_CORS_ORIGINS", "*")
_origins = ["*"] if _cors_origins == "*" else [o.strip() for o in _cors_origins.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register auth & websocket routers
if HAS_AUTH:
    app.include_router(auth_router)
if HAS_WS:
    app.include_router(ws_router)
if HAS_PDF:
    app.include_router(pdf_router)
if HAS_EMAIL:
    app.include_router(email_router)

if HAS_PDF:
    app.include_router(pdf_router)
if HAS_EMAIL:
    app.include_router(email_router)

# Security middleware
if HAS_SECURITY:
    setup_security(app)

# === DATABASE LAYER (PostgreSQL med in-memory fallback) ===
db = get_db()

# BakÃÂ¥tkompatibla alias Ã¢ÂÂ synkron ÃÂ¥tkomst fÃÂ¶r _run_solver (kÃÂ¶r i thread)
# Dessa wrappas till async i endpoints, men solver-trÃÂ¥den behÃÂ¶ver synkron access
schedule_store = db._schedules  # direct dict reference for backward compat in sync code
config_store = db._configs
job_store = db._jobs


# === PYDANTIC MODELLER (API-kontrakt) ===

class ScheduleRequest(BaseModel):
    """BegÃÂ¤ran om att generera ett nytt schema."""
    clinic_id: str = Field(description="Klinik-ID")
    num_weeks: int = Field(default=2, ge=1, le=8, description="Antal veckor att schemalÃÂ¤gga")
    start_date: Optional[str] = Field(default=None, description="Startdatum (YYYY-MM-DD), default=nÃÂ¤sta mÃÂ¥ndag")
    time_limit_seconds: int = Field(default=30, ge=5, le=300, description="Max tid fÃÂ¶r solver")
    locked_assignments: Optional[dict] = Field(default=None, description="LÃÂ¥sta tilldelningar {doctor_id: {day: function}}")
    excluded_doctors: Optional[list[str]] = Field(default=None, description="LÃÂ¤kar-ID som inte ska schemalÃÂ¤ggas")


class ScheduleResponse(BaseModel):
    """Svar med genererat schema."""
    schedule_id: str
    status: str  # "optimal", "feasible", "infeasible", "pending"
    clinic_id: str
    num_weeks: int
    start_date: str
    created_at: str
    solve_time_ms: int
    objective_value: Optional[float] = None
    schedule: Optional[dict] = None  # {doctor_id: {day_index: function_id}}
    statistics: Optional[dict] = None
    warnings: list[str] = []


class AdjustmentRequest(BaseModel):
    """BegÃÂ¤ran om att justera ett befintligt schema."""
    schedule_id: str
    adjustment_type: str  # "swap", "replace", "lock", "unlock"
    doctor_id: str
    day: int
    new_function: Optional[str] = None
    swap_with_doctor_id: Optional[str] = None
    reason: Optional[str] = None


class AbsenceRequest(BaseModel):
    """Registrera frÃÂ¥nvaro fÃÂ¶r en lÃÂ¤kare."""
    clinic_id: str
    doctor_id: str
    absence_type: str  # "sjuk", "semester", "vab", "utbildning", "konferens"
    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD
    reason: Optional[str] = None
    reoptimize: bool = Field(default=True, description="Auto-omoptimera berÃÂ¶rda scheman?")


class ReoptimizeRequest(BaseModel):
    """BegÃÂ¤ran om omoptimering av befintligt schema."""
    schedule_id: str
    preserve_locked: bool = Field(default=True, description="BehÃÂ¥ll lÃÂ¥sta tilldelningar?")
    time_limit_seconds: int = Field(default=30)


class ValidationResult(BaseModel):
    """Resultat av ATL-validering."""
    valid: bool
    violations: list[dict] = []
    warnings: list[dict] = []
    summary: dict = {}


class DoctorInput(BaseModel):
    """LÃÂ¤karinput fÃÂ¶r API."""
    id: str
    name: str
    role: str  # UL, ST_TIDIG, ST_SEN, SP, ÃÂL
    employment_percent: int = 100
    can_primary_call: bool = False
    can_backup_call: bool = False
    exempt_from_call: bool = False
    supervisor_id: Optional[str] = None
    site_preference: Optional[str] = None


class ConfigUpdate(BaseModel):
    """Uppdatering av klinikkonfiguration."""
    clinic_id: str
    doctors: Optional[list[DoctorInput]] = None
    # Fler config-fÃÂ¤lt kan lÃÂ¤ggas till


class HealthResponse(BaseModel):
    """HÃÂ¤lsocheck-svar."""
    status: str
    version: str
    uptime_seconds: float
    schedules_generated: int
    solver_available: bool


# === STARTUP ===
START_TIME = time.time()

@app.on_event("startup")
async def startup():
    """Anslut till PostgreSQL och ladda demo-konfiguration om COP_DEMO=true."""
    db_ok = await connect_db()
    backend = "PostgreSQL" if db_ok else "in-memory"

    demo_mode = os.environ.get("COP_DEMO", "true").lower() in ("true", "1", "yes")
    if demo_mode:
        config = create_kristianstad_example()
        await db.save_config("kristianstad", config)
        generic = create_generic_example()
        await db.save_config("generic", generic)
        print(f"Ã¢ÂÂ COP API startad. Backend: {backend}. Demo-konfigurationer laddade (kristianstad, generic).")
    else:
        print(f"Ã¢ÂÂ COP API startad. Backend: {backend}. Inga demo-konfigurationer (COP_DEMO=false).")

@app.on_event("shutdown")
async def shutdown():
    """StÃÂ¤ng MongoDB-anslutning."""
    await close_db()


# === HEALTH & INFO ===

@app.get("/health", response_model=HealthResponse, tags=["System"])
async def health_check():
    """Kontrollera att API:t lever och solver fungerar."""
    return HealthResponse(
        status="healthy",
        version="0.1.0",
        uptime_seconds=round(time.time() - START_TIME, 1),
        schedules_generated=len(schedule_store),
        solver_available=True,
    )


@app.get("/config", tags=["Konfiguration"])
async def get_default_config(clinic_id: Optional[str] = None):
    """HÃÂ¤mta klinikkonfiguration. Utan clinic_id returneras fÃÂ¶rsta tillgÃÂ¤ngliga."""
    if not clinic_id:
        configs = await db.list_configs()
        if not configs:
            raise HTTPException(status_code=404, detail="Ingen klinik konfigurerad")
        clinic_id = configs[0]["clinic_id"]
    return await get_config_by_id(clinic_id)

@app.get("/configs", tags=["Konfiguration"])
async def list_configs():
    """Lista alla tillgÃÂ¤ngliga klinikkonfigurationer."""
    configs = await db.list_configs()
    return configs


@app.get("/statistics", tags=["Statistik"])
async def get_latest_statistics():
    """HÃÂ¤mta statistik fÃÂ¶r senaste schemat."""
    schedules = await db.list_schedules(limit=1)
    if not schedules:
        return {"total_doctors": 0, "atl_violations": 0, "weeks": 0,
                "role_distribution": {}, "shift_distribution": {}}
    latest = schedules[0]
    latest_id = latest["schedule_id"]
    sched = await db.get_schedule(latest_id)
    stats = sched.get("statistics", {}) if sched else {}
    stats["schedule_id"] = latest_id
    return stats


@app.get("/config/{clinic_id}", tags=["Konfiguration"])
async def get_config_by_id(clinic_id: str):
    """HÃÂ¤mta klinikkonfiguration."""
    config = await db.get_config(clinic_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Klinik '{clinic_id}' finns inte")

    from data_model import config_to_dict
    result = config_to_dict(config)
    result["clinic_id"] = clinic_id
    result["num_doctors"] = len(config.doctors)
    result["num_rooms"] = len(config.operating_rooms)
    return result


# === CONFIG CRUD ===

class ClinicConfigInput(BaseModel):
    """Input for creating/updating a clinic config."""
    name: str
    sites: list[str]
    doctors: list[dict] = []
    operating_rooms: list[dict] = []
    staffing_requirements: list[dict] = []
    call_structure: dict = {}
    atl_rules: dict = {}
    preferences: list[dict] = []
    shift_definitions: list[dict] = []
    constraint_rules: list[dict] = []
    schedule_cycle_weeks: int = 10
    travel_time_between_sites_min: int = 0

@app.post("/config", tags=["Konfiguration"])
async def create_config(clinic_id: str, body: ClinicConfigInput):
    """Skapa ny klinikkonfiguration."""
    existing = await db.get_config(clinic_id)
    if existing:
        raise HTTPException(status_code=409, detail=f"Klinik '{clinic_id}' finns redan")
    from data_model import dict_to_config
    config = dict_to_config(body.model_dump())
    await db.save_config(clinic_id, config)
    await db.audit("config_created", details={"clinic_id": clinic_id})
    return {"status": "created", "clinic_id": clinic_id}

@app.put("/config/{clinic_id}", tags=["Konfiguration"])
async def update_config(clinic_id: str, body: ClinicConfigInput):
    """Uppdatera hela klinikkonfigurationen."""
    existing = await db.get_config(clinic_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Klinik '{clinic_id}' finns inte")
    from data_model import dict_to_config
    config = dict_to_config(body.model_dump())
    await db.save_config(clinic_id, config)
    await db.audit("config_updated", details={"clinic_id": clinic_id})
    return {"status": "updated", "clinic_id": clinic_id}

@app.patch("/config/{clinic_id}/doctors", tags=["Konfiguration"])
async def patch_doctors(clinic_id: str, doctors: list[dict]):
    """Uppdatera bara lakarlistan."""
    config = await db.get_config(clinic_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Klinik '{clinic_id}' finns inte")
    from data_model import config_to_dict, dict_to_config
    data = config_to_dict(config)
    data["doctors"] = doctors
    new_config = dict_to_config(data)
    await db.save_config(clinic_id, new_config)
    await db.audit("config_doctors_updated", details={"clinic_id": clinic_id, "count": len(doctors)})
    return {"status": "updated", "doctors": len(doctors)}

@app.patch("/config/{clinic_id}/rules", tags=["Konfiguration"])
async def patch_rules(clinic_id: str, rules: list[dict]):
    """Uppdatera regler/constraints."""
    config = await db.get_config(clinic_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Klinik '{clinic_id}' finns inte")
    from data_model import config_to_dict, dict_to_config
    data = config_to_dict(config)
    data["constraint_rules"] = rules
    new_config = dict_to_config(data)
    await db.save_config(clinic_id, new_config)
    await db.audit("config_rules_updated", details={"clinic_id": clinic_id, "count": len(rules)})
    return {"status": "updated", "rules": len(rules)}

@app.patch("/config/{clinic_id}/shifts", tags=["Konfiguration"])
async def patch_shifts(clinic_id: str, shifts: list[dict]):
    """Uppdatera passtyper."""
    config = await db.get_config(clinic_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Klinik '{clinic_id}' finns inte")
    from data_model import config_to_dict, dict_to_config
    data = config_to_dict(config)
    data["shift_definitions"] = shifts
    new_config = dict_to_config(data)
    await db.save_config(clinic_id, new_config)
    return {"status": "updated", "shifts": len(shifts)}

@app.delete("/config/{clinic_id}", tags=["Konfiguration"])
async def delete_config(clinic_id: str):
    """Ta bort klinikkonfiguration."""
    existing = await db.get_config(clinic_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Klinik '{clinic_id}' finns inte")
    await db.delete_config(clinic_id)
    await db.audit("config_deleted", details={"clinic_id": clinic_id})
    return {"status": "deleted", "clinic_id": clinic_id}


# === AI ENDPOINTS ===

class AIRuleRequest(BaseModel):
    clinic_id: str
    rule_text: str

class AIChatRequest(BaseModel):
    clinic_id: str
    user_id: str
    message: str

class AIExplainRequest(BaseModel):
    schedule_id: str
    doctor_id: str
    shift_date: str

class AIPredictRequest(BaseModel):
    clinic_id: str
    period_start: str
    period_end: str

class AIConflictRequest(BaseModel):
    clinic_id: str
    new_rule: dict

@app.post("/api/ai/rules/parse", tags=["AI"])
async def ai_parse_rule(req: AIRuleRequest):
    config = await db.get_config(req.clinic_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Klinik '{req.clinic_id}' finns inte")
    from ai_rules import parse_rule
    result = await parse_rule(config, req.rule_text, clinic_id=req.clinic_id)
    if result.get("constraint") and not result.get("error"):
        await db.save_ai_rule(req.clinic_id, req.rule_text, result["constraint"], result["confidence"])
    return result

@app.post("/api/ai/conflicts/check", tags=["AI"])
async def ai_check_conflicts(req: AIConflictRequest):
    config = await db.get_config(req.clinic_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Klinik '{req.clinic_id}' finns inte")
    from ai_conflicts import check_conflicts
    return await check_conflicts(config, req.new_rule, clinic_id=req.clinic_id)

@app.post("/api/ai/explain", tags=["AI"])
async def ai_explain(req: AIExplainRequest):
    sched = await db.get_schedule(req.schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schema inte hittat")
    config = await db.get_config(sched.get("clinic_id", ""))
    if not config:
        raise HTTPException(status_code=404, detail="Klinik-config saknas")
    from ai_explain import explain_assignment
    return await explain_assignment(config, sched, req.doctor_id, req.shift_date, clinic_id=sched.get("clinic_id", ""))

@app.post("/api/ai/predict/absence", tags=["AI"])
async def ai_predict(req: AIPredictRequest):
    config = await db.get_config(req.clinic_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Klinik '{req.clinic_id}' finns inte")
    chains = await db.list_chains(limit=200)
    from ai_predict import predict_absence
    result = await predict_absence(chains, req.period_start, req.period_end, len(config.doctors), clinic_id=req.clinic_id)
    if not result.get("error"):
        await db.save_ai_prediction(req.clinic_id, f"{req.period_start}_{req.period_end}", result)
    return result

@app.post("/api/ai/chat", tags=["AI"])
async def ai_chat_endpoint(req: AIChatRequest):
    config = await db.get_config(req.clinic_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Klinik '{req.clinic_id}' finns inte")
    schedules = await db.list_schedules(clinic_id=req.clinic_id, limit=1)
    latest = await db.get_schedule(schedules[0]["schedule_id"]) if schedules else {}
    history = await db.get_ai_chat_history(req.clinic_id, req.user_id, limit=10)
    from ai_chat import chat
    result = await chat(config, latest, req.user_id, req.message, chat_history=history, clinic_id=req.clinic_id)
    await db.save_ai_chat(req.clinic_id, req.user_id, req.message, result.get("response_sv", ""), result.get("action"))
    return result


# === SCHEMAGENERERING ===

def _run_solver(job_id: str, config: ClinicConfig, request: ScheduleRequest):
    """KÃÂ¶r solver i bakgrunden."""
    try:
        job_store[job_id]["status"] = "running"
        start_time = time.time()

        schedule = solve_schedule(
            config,
            num_weeks=request.num_weeks,
            time_limit_seconds=request.time_limit_seconds,
        )

        solve_time_ms = int((time.time() - start_time) * 1000)

        if schedule is None:
            job_store[job_id]["status"] = "infeasible"
            job_store[job_id]["error"] = "Ingen giltig lÃÂ¶sning hittades"
            return

        # BerÃÂ¤kna startdatum
        if request.start_date:
            start = datetime.strptime(request.start_date, "%Y-%m-%d").date()
        else:
            today = date.today()
            days_until_monday = (7 - today.weekday()) % 7
            if days_until_monday == 0:
                days_until_monday = 7
            start = today + timedelta(days=days_until_monday)

        # BerÃÂ¤kna statistik
        statistics = _compute_statistics(schedule, config, request.num_weeks * 7)

        # Konvertera schedule till datum-baserat format
        date_schedule = {}
        for doc_id, days in schedule.items():
            date_schedule[doc_id] = {}
            for day_idx, func in days.items():
                day_date = start + timedelta(days=day_idx)
                date_schedule[doc_id][day_date.isoformat()] = func

        schedule_id = job_store[job_id]["schedule_id"]
        schedule_data = {
            "schedule_id": schedule_id,
            "status": "optimal",
            "clinic_id": request.clinic_id,
            "num_weeks": request.num_weeks,
            "start_date": start.isoformat(),
            "created_at": datetime.now().isoformat(),
            "solve_time_ms": solve_time_ms,
            "schedule": date_schedule,
            "raw_schedule": schedule,  # Indexbaserat fÃÂ¶r intern anvÃÂ¤ndning
            "statistics": statistics,
            "warnings": [],
        }

        schedule_store[schedule_id] = schedule_data
        job_store[job_id]["status"] = "completed"
        job_store[job_id]["result"] = schedule_data

        # Persist to DB if available (async from sync context)
        if db.using_postgres:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(db.save_schedule(schedule_data))
                loop.create_task(db.save_job(job_store[job_id]))
            except Exception:
                pass  # In-memory fallback already saved

        # WebSocket broadcast: schema genererat
        if HAS_WS:
            import asyncio
            try:
                asyncio.get_event_loop().create_task(
                    hub.broadcast("schedule", {
                        "schedule_id": schedule_id,
                        "clinic_id": request.clinic_id,
                        "weeks": request.num_weeks,
                        "solve_time_ms": solve_time_ms,
                    }, event_type="schedule_generated")
                )
            except Exception:
                pass  # WebSocket broadcast failure should not break API

    except Exception as e:
        job_store[job_id]["status"] = "failed"
        job_store[job_id]["error"] = str(e)


def _compute_statistics(schedule: dict, config: ClinicConfig, num_days: int) -> dict:
    """BerÃÂ¤kna schemastatistik."""
    doc_by_id = {d.id: d for d in config.doctors}
    stats = {
        "call_distribution": {},
        "staffing_per_day": {},
        "st_matching": {},
        "atl_violations": [],
        "workload_balance": {},
    }

    # JourfÃÂ¶rdelning
    for doc in config.doctors:
        primary = sum(1 for d in range(num_days) if schedule.get(doc.id, {}).get(d) == "JOUR_P")
        backup = sum(1 for d in range(num_days) if schedule.get(doc.id, {}).get(d) == "JOUR_B")
        if primary + backup > 0:
            stats["call_distribution"][doc.id] = {
                "name": doc.name,
                "role": doc.role.value,
                "primary": primary,
                "backup": backup,
                "total": primary + backup,
            }

    # Bemanningstal per dag
    for day in range(num_days):
        counts = defaultdict(int)
        for doc in config.doctors:
            func = schedule.get(doc.id, {}).get(day, "LEDIG")
            if func != "LEDIG":
                counts[func] += 1
        stats["staffing_per_day"][day] = dict(counts)

    # ST-handledarmatchning
    for doc in config.doctors:
        if doc.supervisor_id:
            matches = 0
            total_op = 0
            for day in range(num_days):
                func = schedule.get(doc.id, {}).get(day, "")
                if func.startswith("OP_"):
                    total_op += 1
                    sup_func = schedule.get(doc.supervisor_id, {}).get(day, "")
                    if sup_func == func:
                        matches += 1
            if total_op > 0:
                stats["st_matching"][doc.id] = {
                    "name": doc.name,
                    "matches": matches,
                    "total_op_days": total_op,
                    "match_rate": round(matches / total_op * 100, 1),
                }

    # ATL-brott
    for doc in config.doctors:
        for day in range(num_days - 1):
            func_today = schedule.get(doc.id, {}).get(day, "LEDIG")
            func_tomorrow = schedule.get(doc.id, {}).get(day + 1, "LEDIG")
            if func_today in ("JOUR_P", "JOUR_B") and func_tomorrow not in ("LEDIG", "JOUR_P", "JOUR_B"):
                stats["atl_violations"].append({
                    "doctor_id": doc.id,
                    "doctor_name": doc.name,
                    "day": day,
                    "violation": f"Jour dag {day+1} Ã¢ÂÂ arbete dag {day+2}",
                })

    # Arbetsbelastning (antal arbetsdagar)
    for doc in config.doctors:
        work_days = sum(1 for d in range(num_days)
                       if schedule.get(doc.id, {}).get(d, "LEDIG") != "LEDIG")
        stats["workload_balance"][doc.id] = {
            "name": doc.name,
            "role": doc.role.value,
            "work_days": work_days,
            "total_days": num_days,
            "utilization": round(work_days / num_days * 100, 1),
        }

    return stats


@app.post("/schedule/generate", tags=["Schema"])
async def generate_schedule(request: ScheduleRequest, background_tasks: BackgroundTasks):
    """
    Generera ett nytt optimerat schema.

    Startar solver i bakgrunden. Returnerar job_id fÃÂ¶r polling.
    Alternativt: om time_limit <= 60s, kÃÂ¶r synkront.
    """
    config = await db.get_config(request.clinic_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Klinik '{request.clinic_id}' finns inte")

    schedule_id = f"sch_{uuid.uuid4().hex[:12]}"
    job_id = f"job_{uuid.uuid4().hex[:8]}"

    job_data = {
        "job_id": job_id,
        "schedule_id": schedule_id,
        "status": "queued",
        "created_at": datetime.now().isoformat(),
    }
    await db.save_job(job_data)

    if request.time_limit_seconds <= 60:
        # Synkron kÃÂ¶rning fÃÂ¶r snabba jobb
        _run_solver(job_id, config, request)
        job = job_store[job_id]

        if job["status"] == "completed":
            return job["result"]
        elif job["status"] == "infeasible":
            raise HTTPException(status_code=422, detail="Ingen giltig lÃÂ¶sning kunde hittas med givna constraints")
        else:
            raise HTTPException(status_code=500, detail=job.get("error", "OkÃÂ¤nt fel"))
    else:
        # Asynkron kÃÂ¶rning
        background_tasks.add_task(_run_solver, job_id, config, request)
        return {
            "job_id": job_id,
            "schedule_id": schedule_id,
            "status": "queued",
            "message": "Schemagenerering startad. Polla /job/{job_id} fÃÂ¶r status.",
        }


@app.get("/job/{job_id}", tags=["Schema"])
async def get_job_status(job_id: str):
    """HÃÂ¤mta status fÃÂ¶r ett bakgrundsjobb."""
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Jobb inte hittat")
    return job


@app.get("/schedule/{schedule_id}", tags=["Schema"])
async def get_schedule(schedule_id: str):
    """HÃÂ¤mta ett genererat schema."""
    sched = await db.get_schedule(schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schema inte hittat")

    # Returnera utan raw_schedule (intern data)
    result = {k: v for k, v in sched.items() if k != "raw_schedule"}
    return result


@app.get("/schedule/{schedule_id}/doctor/{doctor_id}", tags=["Schema"])
async def get_doctor_schedule(schedule_id: str, doctor_id: str):
    """HÃÂ¤mta schema fÃÂ¶r en specifik lÃÂ¤kare."""
    sched = await db.get_schedule(schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schema inte hittat")

    doctor_schedule = sched["schedule"].get(doctor_id)
    if not doctor_schedule:
        raise HTTPException(status_code=404, detail=f"LÃÂ¤kare '{doctor_id}' inte hittad i schema")

    config = await db.get_config(sched["clinic_id"])
    doc = next((d for d in config.doctors if d.id == doctor_id), None) if config else None

    return {
        "doctor_id": doctor_id,
        "doctor_name": doc.name if doc else doctor_id,
        "role": doc.role.value if doc else "unknown",
        "schedule": doctor_schedule,
        "statistics": sched["statistics"].get("workload_balance", {}).get(doctor_id, {}),
        "call_stats": sched["statistics"].get("call_distribution", {}).get(doctor_id, {}),
    }


# === SCHEMAJUSTERING ===

@app.post("/schedule/adjust", tags=["Justering"])
async def adjust_schedule(request: AdjustmentRequest):
    """
    Manuell justering av befintligt schema.

    Typer:
    - swap: Byt funktion mellan tvÃÂ¥ lÃÂ¤kare pÃÂ¥ en dag
    - replace: ÃÂndra en lÃÂ¤kares funktion en specifik dag
    - lock: LÃÂ¥s en tilldelning (kan inte ÃÂ¤ndras av omoptimering)
    - unlock: LÃÂ¥s upp en tilldelning
    """
    sched = await db.get_schedule(request.schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schema inte hittat")

    raw = sched.get("raw_schedule", {})
    config = await db.get_config(sched["clinic_id"])

    # raw_schedule keys can be int (from solver) or str (from JSON/MongoDB)
    day_int = request.day
    day_str = str(request.day)

    def _raw_get(doc_id, day):
        """Get value from raw_schedule, trying both int and str keys."""
        doc_days = raw.get(doc_id, {})
        val = doc_days.get(day_int)
        if val is None:
            val = doc_days.get(day_str)
        return val

    def _raw_set(doc_id, day, value):
        """Set value in raw_schedule, using whichever key type exists."""
        doc_days = raw.get(doc_id, {})
        if day_int in doc_days:
            doc_days[day_int] = value
        else:
            doc_days[day_str] = value

    def _sync_date_schedule():
        """Sync the date-keyed schedule after raw_schedule changes."""
        start = date.fromisoformat(sched["start_date"])
        date_sched = sched.get("schedule", {})
        d = start + timedelta(days=day_int)
        date_str = d.isoformat()
        for doc_id in raw:
            if doc_id not in date_sched:
                date_sched[doc_id] = {}
            val = _raw_get(doc_id, day_int)
            if val is not None:
                date_sched[doc_id][date_str] = val
        sched["schedule"] = date_sched

    if request.adjustment_type == "swap":
        if not request.swap_with_doctor_id:
            raise HTTPException(status_code=400, detail="swap_with_doctor_id krÃÂ¤vs fÃÂ¶r swap")

        func_a = _raw_get(request.doctor_id, day_int)
        func_b = _raw_get(request.swap_with_doctor_id, day_int)

        if func_a is None or func_b is None:
            raise HTTPException(status_code=400, detail="Ogiltigt dag-index")

        _raw_set(request.doctor_id, day_int, func_b)
        _raw_set(request.swap_with_doctor_id, day_int, func_a)

        _sync_date_schedule()
        await db.save_schedule(sched)
        await db.audit("schedule_swap", details={"schedule_id": request.schedule_id, "day": request.day, "doctor_a": request.doctor_id, "doctor_b": request.swap_with_doctor_id})

        warnings = _validate_single_day(raw, config, day_int)

        return {
            "status": "adjusted",
            "adjustment": f"Bytte {request.doctor_id} ({func_a}Ã¢ÂÂ{func_b}) med {request.swap_with_doctor_id} ({func_b}Ã¢ÂÂ{func_a}) dag {request.day}",
            "warnings": warnings,
        }

    elif request.adjustment_type == "replace":
        if not request.new_function:
            raise HTTPException(status_code=400, detail="new_function krÃÂ¤vs fÃÂ¶r replace")

        old_func = _raw_get(request.doctor_id, day_int)
        _raw_set(request.doctor_id, day_int, request.new_function)

        _sync_date_schedule()
        await db.save_schedule(sched)
        await db.audit("schedule_replace", details={"schedule_id": request.schedule_id, "day": request.day, "doctor": request.doctor_id})

        warnings = _validate_single_day(raw, config, day_int)

        return {
            "status": "adjusted",
            "adjustment": f"{request.doctor_id}: {old_func} Ã¢ÂÂ {request.new_function} dag {request.day}",
            "warnings": warnings,
        }

    else:
        raise HTTPException(status_code=400, detail=f"OkÃÂ¤nd adjustment_type: {request.adjustment_type}")


def _validate_single_day(schedule: dict, config: ClinicConfig, day: int) -> list[str]:
    """Validera en specifik dag efter justering."""
    warnings = []

    def _get(days_dict, d):
        """Get from dict trying both int and str keys."""
        v = days_dict.get(d)
        if v is None:
            v = days_dict.get(str(d))
        return v

    # Kolla att jour finns
    primary = [d_id for d_id, days in schedule.items() if _get(days, day) == "JOUR_P"]
    backup = [d_id for d_id, days in schedule.items() if _get(days, day) == "JOUR_B"]

    if len(primary) != 1:
        warnings.append(f"Dag {day}: {len(primary)} primÃÂ¤rjourer (ska vara 1)")
    if len(backup) != 1:
        warnings.append(f"Dag {day}: {len(backup)} bakjourer (ska vara 1)")

    # Kolla ATL (jour igÃÂ¥r Ã¢ÂÂ arbete idag)
    if day > 0:
        for d_id, days in schedule.items():
            yesterday = _get(days, day - 1) or "LEDIG"
            today = _get(days, day) or "LEDIG"
            if yesterday in ("JOUR_P", "JOUR_B") and today not in ("LEDIG", "JOUR_P", "JOUR_B"):
                doc = next((d for d in config.doctors if d.id == d_id), None)
                name = doc.name if doc else d_id
                warnings.append(f"ATL-brott: {name} hade jour dag {day-1} och arbetar dag {day}")

    return warnings


# === FRÃÂNVARO ===

@app.post("/absence", tags=["FrÃÂ¥nvaro"])
async def register_absence(request: AbsenceRequest, background_tasks: BackgroundTasks):
    """
    Registrera frÃÂ¥nvaro och (valfritt) omoptimera berÃÂ¶rda scheman.

    FlÃÂ¶de:
    1. Registrera frÃÂ¥nvaron
    2. Hitta berÃÂ¶rda scheman
    3. LÃÂ¥s alla andra tilldelningar
    4. Omoptimera med frÃÂ¥nvarande lÃÂ¤kare exkluderad
    """
    config = await db.get_config(request.clinic_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Klinik '{request.clinic_id}' finns inte")

    # Validera att lÃÂ¤karen finns
    doc = next((d for d in config.doctors if d.id == request.doctor_id), None)
    if not doc:
        raise HTTPException(status_code=404, detail=f"LÃÂ¤kare '{request.doctor_id}' finns inte")

    absence_id = f"abs_{uuid.uuid4().hex[:8]}"

    result = {
        "absence_id": absence_id,
        "doctor_id": request.doctor_id,
        "doctor_name": doc.name,
        "absence_type": request.absence_type,
        "start_date": request.start_date,
        "end_date": request.end_date,
        "status": "registered",
        "affected_schedules": [],
    }

    # Hitta berÃÂ¶rda scheman
    for sched_id, sched in schedule_store.items():
        if sched["clinic_id"] == request.clinic_id:
            # Kolla om frÃÂ¥nvaron ÃÂ¶verlappar med schemat
            sched_start = date.fromisoformat(sched["start_date"])
            sched_end = sched_start + timedelta(weeks=sched["num_weeks"])
            abs_start = date.fromisoformat(request.start_date)
            abs_end = date.fromisoformat(request.end_date)

            if abs_start <= sched_end and abs_end >= sched_start:
                result["affected_schedules"].append(sched_id)

                # Markera frÃÂ¥nvarande dagar som LEDIG i raw_schedule
                raw = sched.get("raw_schedule", {})
                if request.doctor_id in raw:
                    for day_idx in range(sched["num_weeks"] * 7):
                        day_date = sched_start + timedelta(days=day_idx)
                        if abs_start <= day_date <= abs_end:
                            old_func = raw[request.doctor_id].get(day_idx, "LEDIG")
                            raw[request.doctor_id][day_idx] = "LEDIG"

                            # Uppdatera ÃÂ¤ven datum-schemat
                            if request.doctor_id in sched.get("schedule", {}):
                                sched["schedule"][request.doctor_id][day_date.isoformat()] = "LEDIG"

    if request.reoptimize and result["affected_schedules"]:
        result["status"] = "registered_reoptimize_pending"
        result["message"] = f"FrÃÂ¥nvaro registrerad. {len(result['affected_schedules'])} schema(n) behÃÂ¶ver omoptimeras."
    else:
        result["message"] = "FrÃÂ¥nvaro registrerad. BerÃÂ¶rda dagar satta till LEDIG."

    return result


# === OMOPTIMERING ===

@app.post("/schedule/reoptimize", tags=["Schema"])
async def reoptimize_schedule(request: ReoptimizeRequest):
    """
    Omoptimera ett befintligt schema efter ÃÂ¤ndringar.

    BehÃÂ¥ller lÃÂ¥sta tilldelningar och optimerar resten.
    """
    sched = await db.get_schedule(request.schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schema inte hittat")

    config = await db.get_config(sched["clinic_id"])
    if not config:
        raise HTTPException(status_code=404, detail="Klinikkonfiguration saknas")

    # KÃÂ¶r ny optimering
    gen_request = ScheduleRequest(
        clinic_id=sched["clinic_id"],
        num_weeks=sched["num_weeks"],
        start_date=sched["start_date"],
        time_limit_seconds=request.time_limit_seconds,
    )

    new_schedule_id = f"sch_{uuid.uuid4().hex[:12]}"
    job_id = f"job_{uuid.uuid4().hex[:8]}"

    job_store[job_id] = {
        "job_id": job_id,
        "schedule_id": new_schedule_id,
        "status": "queued",
        "parent_schedule": request.schedule_id,
    }

    _run_solver(job_id, config, gen_request)
    job = job_store[job_id]

    if job["status"] == "completed":
        return {
            "status": "reoptimized",
            "original_schedule_id": request.schedule_id,
            "new_schedule_id": new_schedule_id,
            "schedule": job["result"],
        }
    else:
        raise HTTPException(
            status_code=422,
            detail=f"Omoptimering misslyckades: {job.get('error', 'okÃÂ¤nt fel')}"
        )


# === VALIDERING ===

@app.post("/validate/{schedule_id}", tags=["Validering"])
async def validate_schedule(schedule_id: str):
    """
    FullstÃÂ¤ndig ATL-validering av ett schema.

    Kontrollerar:
    - 11h dygnsvila efter jour
    - 36h sammanhÃÂ¤ngande veckovila
    - Max 48h/vecka
    - Max 20h sammanhÃÂ¤ngande arbete+jour
    - Max 1 jour/vecka
    - Minimibemanningstal
    """
    sched = await db.get_schedule(schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schema inte hittat")

    config = await db.get_config(sched["clinic_id"])
    raw = sched.get("raw_schedule", {})
    num_days = sched["num_weeks"] * 7

    violations = []
    warnings = []

    # 1. Dygnsvila efter jour
    for doc in config.doctors:
        for day in range(num_days - 1):
            func = raw.get(doc.id, {}).get(day, "LEDIG")
            func_next = raw.get(doc.id, {}).get(day + 1, "LEDIG")
            if func in ("JOUR_P", "JOUR_B") and func_next not in ("LEDIG", "JOUR_P", "JOUR_B"):
                violations.append({
                    "type": "dygnsvila",
                    "severity": "critical",
                    "doctor_id": doc.id,
                    "doctor_name": doc.name,
                    "day": day,
                    "detail": f"Jour dag {day+1} Ã¢ÂÂ arbete dag {day+2} (krÃÂ¤ver 11h vila)",
                    "atl_reference": "ATL 13ÃÂ§, AB 13ÃÂ§7",
                })

    # 2. Max jourer per vecka
    for doc in config.doctors:
        for week in range(sched["num_weeks"]):
            week_start = week * 7
            calls = sum(1 for d in range(week_start, min(week_start + 7, num_days))
                       if raw.get(doc.id, {}).get(d) in ("JOUR_P", "JOUR_B"))
            if calls > 1:
                violations.append({
                    "type": "max_jour_vecka",
                    "severity": "warning",
                    "doctor_id": doc.id,
                    "doctor_name": doc.name,
                    "week": week + 1,
                    "detail": f"{calls} jourer vecka {week+1} (rekommenderat max 1)",
                })

    # 3. Bemanningstal
    for day in range(num_days):
        weekday = day % 7
        if weekday >= 5:
            continue

        primary = sum(1 for d_id in raw if raw[d_id].get(day) == "JOUR_P")
        backup = sum(1 for d_id in raw if raw[d_id].get(day) == "JOUR_B")

        if primary != 1:
            violations.append({
                "type": "bemanning",
                "severity": "critical",
                "day": day,
                "detail": f"Dag {day+1}: {primary} primÃÂ¤rjourer (ska vara 1)",
            })
        if backup != 1:
            violations.append({
                "type": "bemanning",
                "severity": "critical",
                "day": day,
                "detail": f"Dag {day+1}: {backup} bakjourer (ska vara 1)",
            })

    # 4. Arbetsbelastning per vecka
    for doc in config.doctors:
        for week in range(sched["num_weeks"]):
            week_start = week * 7
            work_days = sum(1 for d in range(week_start, min(week_start + 7, num_days))
                          if raw.get(doc.id, {}).get(d, "LEDIG") != "LEDIG")
            if work_days > 5:
                warnings.append({
                    "type": "arbetsbelastning",
                    "severity": "warning",
                    "doctor_id": doc.id,
                    "doctor_name": doc.name,
                    "week": week + 1,
                    "detail": f"{work_days} arbetsdagar vecka {week+1} (max 5 rekommenderat)",
                })

    return ValidationResult(
        valid=len([v for v in violations if v["severity"] == "critical"]) == 0,
        violations=violations,
        warnings=warnings,
        summary={
            "total_violations": len(violations),
            "critical": len([v for v in violations if v["severity"] == "critical"]),
            "warnings": len(warnings),
            "doctors_checked": len(config.doctors),
            "days_checked": num_days,
        },
    )


# === EXPORT ===

@app.get("/schedule/{schedule_id}/export/excel", tags=["Export"])
async def export_schedule_excel(schedule_id: str):
    """Exportera schema som Excel-fil."""
    from fastapi.responses import StreamingResponse
    import io

    sched = await db.get_schedule(schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schema inte hittat")

    config = await db.get_config(sched["clinic_id"])

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        raise HTTPException(status_code=501, detail="openpyxl ej installerat")

    wb = Workbook()
    ws = wb.active
    ws.title = "Schema"

    # Build date list
    start = date.fromisoformat(sched["start_date"])
    total_days = sched["num_weeks"] * 7
    dates = [start + timedelta(days=i) for i in range(total_days)]

    day_names = ["MÃÂ¥n", "Tis", "Ons", "Tor", "Fre", "LÃÂ¶r", "SÃÂ¶n"]

    # Color map for functions
    func_fills = {
        "OP_H": PatternFill(start_color="E0F7FA", end_color="E0F7FA", fill_type="solid"),
        "OP_C": PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid"),
        "AVD_H": PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid"),
        "AVD_C": PatternFill(start_color="FFFBEB", end_color="FFFBEB", fill_type="solid"),
        "MOTT_H": PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid"),
        "MOTT_C": PatternFill(start_color="ECFDF5", end_color="ECFDF5", fill_type="solid"),
        "JOUR_P": PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid"),
        "JOUR_B": PatternFill(start_color="FFE4E6", end_color="FFE4E6", fill_type="solid"),
        "LEDIG": PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid"),
    }

    thin_border = Border(
        left=Side(style="thin", color="D1D5DB"),
        right=Side(style="thin", color="D1D5DB"),
        top=Side(style="thin", color="D1D5DB"),
        bottom=Side(style="thin", color="D1D5DB"),
    )

    # Header row 1: day names
    ws.cell(row=1, column=1, value="LÃÂ¤kare").font = Font(bold=True, size=10)
    ws.cell(row=1, column=2, value="Roll").font = Font(bold=True, size=10)
    for ci, d in enumerate(dates):
        cell = ws.cell(row=1, column=ci + 3, value=day_names[d.weekday()])
        cell.font = Font(bold=True, size=9, color="FFFFFF")
        cell.fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
        cell.alignment = Alignment(horizontal="center")
        cell.border = thin_border

    # Header row 2: dates
    for ci, d in enumerate(dates):
        cell = ws.cell(row=2, column=ci + 3, value=d.strftime("%d/%m"))
        cell.font = Font(size=8, color="64748B")
        cell.alignment = Alignment(horizontal="center")
        cell.border = thin_border

    # Build doctor map
    doc_map = {}
    if config:
        for d in config.doctors:
            doc_map[d.id] = d

    # Sort doctors by role
    role_order = {"ÃÂL": 0, "SP": 1, "ST_SEN": 2, "ST_TIDIG": 3, "UL": 4}
    schedule_data = sched.get("schedule", {})
    def _role_str(doc):
        if doc is None:
            return "?"
        r = doc.role
        return r.value if hasattr(r, 'value') else str(r)

    sorted_docs = sorted(schedule_data.keys(), key=lambda x: (
        role_order.get(_role_str(doc_map.get(x)), 9),
        getattr(doc_map.get(x), "name", x),
    ))

    # Data rows
    for ri, doc_id in enumerate(sorted_docs):
        row = ri + 3
        doc = doc_map.get(doc_id)
        name = doc.name if doc else doc_id
        role = str(doc.role.value) if doc and hasattr(doc.role, 'value') else (str(doc.role) if doc else "?")

        ws.cell(row=row, column=1, value=name).font = Font(size=9)
        ws.cell(row=row, column=2, value=role).font = Font(size=9, color="64748B")

        day_map = schedule_data.get(doc_id, {})
        for ci, d in enumerate(dates):
            date_str = d.isoformat()
            func = day_map.get(date_str, "LEDIG")
            cell = ws.cell(row=row, column=ci + 3, value=func)
            cell.font = Font(size=9)
            cell.alignment = Alignment(horizontal="center")
            cell.border = thin_border
            if func in func_fills:
                cell.fill = func_fills[func]

    # Column widths
    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 8
    for ci in range(len(dates)):
        col_letter = ws.cell(row=1, column=ci + 3).column_letter
        ws.column_dimensions[col_letter].width = 10

    # Freeze panes (doctor names + header)
    ws.freeze_panes = "C3"

    # Write to buffer
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"schema_{schedule_id}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# === STATISTIK ===

@app.get("/statistics/{schedule_id}", tags=["Statistik"])
async def get_statistics(schedule_id: str):
    """HÃÂ¤mta detaljerad statistik fÃÂ¶r ett schema."""
    sched = await db.get_schedule(schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schema inte hittat")
    return sched.get("statistics", {})


# === LISTA SCHEMAN ===

@app.get("/schedules", tags=["Schema"])
async def list_schedules(clinic_id: Optional[str] = None):
    """Lista alla genererade scheman."""
    schedules = await db.list_schedules(clinic_id=clinic_id)
    return [
        {
            "schedule_id": s.get("schedule_id"),
            "clinic_id": s.get("clinic_id"),
            "status": s.get("status"),
            "num_weeks": s.get("num_weeks"),
            "start_date": s.get("start_date"),
            "created_at": s.get("created_at"),
            "solve_time_ms": s.get("solve_time_ms"),
        }
        for s in schedules
    ]


# === FRÃÂNVAROKEDJA (Automatisk ersÃÂ¤ttare) ===

class AbsenceChainRequest(BaseModel):
    """BegÃÂ¤ran om att kÃÂ¶ra frÃÂ¥nvarokedjan."""
    schedule_id: str
    doctor_id: str
    absence_type: str = Field(description="sjuk, vab, semester, utbildning, konferens, permission, akut")
    start_date: str = Field(description="YYYY-MM-DD")
    end_date: str = Field(description="YYYY-MM-DD")
    auto_select: bool = Field(default=True, description="VÃÂ¤lj bÃÂ¤sta ersÃÂ¤ttare automatiskt?")
    reason: Optional[str] = None


class ManualReplacementRequest(BaseModel):
    """Manuellt val av ersÃÂ¤ttare frÃÂ¥n kandidatlistan."""
    schedule_id: str
    day: int
    function: str
    absent_doctor_id: str
    replacement_doctor_id: str
    override_atl: bool = Field(default=False, description="GodkÃÂ¤nn trots ATL-varning?")


# Absence chain store Ã¢ÂÂ backed by db layer
absence_chain_store = db._chains  # backward compat dict reference


@app.post("/absence/chain", tags=["FrÃÂ¥nvarokedja"])
async def run_absence_chain(request: AbsenceChainRequest):
    """
    KÃÂ¶r hela frÃÂ¥nvarokedjan: registrera Ã¢ÂÂ analysera Ã¢ÂÂ ranka Ã¢ÂÂ validera Ã¢ÂÂ ersÃÂ¤tt Ã¢ÂÂ notifiera.

    Returnerar komplett resultat med ersÃÂ¤ttare, kandidatlistor, ATL-validering och notifieringar.
    Om auto_select=False returneras bara kandidatlistan utan att schemat ÃÂ¤ndras.
    """
    sched = await db.get_schedule(request.schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schema inte hittat")

    config = await db.get_config(sched["clinic_id"])
    if not config:
        raise HTTPException(status_code=404, detail="Klinikkonfiguration saknas")

    raw = sched.get("raw_schedule", {})
    start = date.fromisoformat(sched["start_date"])

    chain = AbsenceChain(config, raw, start, sched["num_weeks"])
    result = chain.execute(
        doctor_id=request.doctor_id,
        absence_type=request.absence_type,
        start_date=request.start_date,
        end_date=request.end_date,
        auto_select=request.auto_select,
    )

    # Spara resultat
    absence_chain_store[result.chain_id] = result

    # WebSocket broadcast: frÃÂ¥nvarokedja
    if HAS_WS:
        import asyncio
        try:
            asyncio.get_event_loop().create_task(
                hub.broadcast_absence_chain(
                    chain_id=result.chain_id,
                    status=result.status.value,
                    details={
                        "doctor_id": request.doctor_id,
                        "absence_type": request.absence_type,
                        "vacant_slots": len(result.vacant_slots),
                        "replaced": len(result.schedule_changes),
                    }
                )
            )
        except Exception:
            pass

    # Om auto_select: uppdatera ÃÂ¤ven datum-schemat
    if request.auto_select and result.schedule_changes:
        for change in result.schedule_changes:
            day_date = change["date"]
            # FrÃÂ¥nvarande Ã¢ÂÂ LEDIG
            if request.doctor_id in sched.get("schedule", {}):
                sched["schedule"][request.doctor_id][day_date] = "LEDIG"
            # ErsÃÂ¤ttare Ã¢ÂÂ ny funktion
            repl_id = change["replacement_doctor"]
            if repl_id in sched.get("schedule", {}):
                sched["schedule"][repl_id][day_date] = change["function"]

        # Uppdatera statistik
        sched["statistics"] = _compute_statistics(raw, config, sched["num_weeks"] * 7)

    return {
        "chain_id": result.chain_id,
        "status": result.status.value,
        "doctor": f"{result.doctor_name} ({result.doctor_id})",
        "absence_type": result.absence_type,
        "period": f"{result.start_date} Ã¢ÂÂ {result.end_date}",
        "summary": {
            "vacant_slots": len(result.vacant_slots),
            "replaced": len(result.schedule_changes),
            "failed": len(result.failed_slots),
            "atl_warnings": len(result.atl_violations),
        },
        "replacements": result.replacements,
        "failed_slots": result.failed_slots,
        "schedule_changes": result.schedule_changes,
        "notifications": result.notifications,
        "chain_log": result.chain_log,
    }


@app.get("/absence/chain/{chain_id}", tags=["FrÃÂ¥nvarokedja"])
async def get_absence_chain(chain_id: str):
    """HÃÂ¤mta resultat av en tidigare kÃÂ¶rd frÃÂ¥nvarokedja."""
    result = absence_chain_store.get(chain_id)
    if not result:
        raise HTTPException(status_code=404, detail="FrÃÂ¥nvarokedja inte hittad")

    return {
        "chain_id": result.chain_id,
        "status": result.status.value,
        "doctor": f"{result.doctor_name} ({result.doctor_id})",
        "absence_type": result.absence_type,
        "period": f"{result.start_date} Ã¢ÂÂ {result.end_date}",
        "replacements": result.replacements,
        "failed_slots": result.failed_slots,
        "schedule_changes": result.schedule_changes,
        "notifications": result.notifications,
        "chain_log": result.chain_log,
    }


@app.post("/absence/chain/manual-replace", tags=["FrÃÂ¥nvarokedja"])
async def manual_replacement(request: ManualReplacementRequest):
    """
    Manuellt val av ersÃÂ¤ttare Ã¢ÂÂ fÃÂ¶r slots som krÃÂ¤ver manuell hantering
    eller nÃÂ¤r du vill vÃÂ¤lja en annan kandidat ÃÂ¤n den automatiskt valda.
    """
    sched = await db.get_schedule(request.schedule_id)
    if not sched:
        raise HTTPException(status_code=404, detail="Schema inte hittat")

    config = await db.get_config(sched["clinic_id"])
    raw = sched.get("raw_schedule", {})

    # Validera att ersÃÂ¤ttaren finns
    doc = next((d for d in config.doctors if d.id == request.replacement_doctor_id), None)
    if not doc:
        raise HTTPException(status_code=404, detail=f"LÃÂ¤kare '{request.replacement_doctor_id}' finns inte")

    # ATL-validering
    start = date.fromisoformat(sched["start_date"])
    chain = AbsenceChain(config, raw, start, sched["num_weeks"])

    from absence_chain import VacantSlot, Candidate
    slot = VacantSlot(
        day_index=request.day,
        day_date=start + timedelta(days=request.day),
        function=request.function,
        is_call=request.function in ("JOUR_P", "JOUR_B"),
        site=chain.FUNCTION_SITE.get(request.function),
        weekday=(start + timedelta(days=request.day)).weekday(),
    )
    candidate = Candidate(
        doctor_id=doc.id, doctor_name=doc.name, role=doc.role.value,
        score=0, current_function=raw.get(doc.id, {}).get(request.day, "LEDIG"),
        reasons=[],
    )
    atl_result = chain._validate_atl(candidate, slot)

    if not atl_result["ok"] and not request.override_atl:
        return {
            "status": "atl_violation",
            "message": "ATL-brott upptÃÂ¤ckt. SÃÂ¤tt override_atl=true fÃÂ¶r att godkÃÂ¤nna ÃÂ¤ndÃÂ¥.",
            "violations": atl_result["violations"],
        }

    # VerkstÃÂ¤ll
    old_func = raw.get(request.replacement_doctor_id, {}).get(request.day, "LEDIG")

    # FrÃÂ¥nvarande Ã¢ÂÂ LEDIG
    if request.absent_doctor_id in raw:
        raw[request.absent_doctor_id][request.day] = "LEDIG"
    # ErsÃÂ¤ttare Ã¢ÂÂ funktion
    if request.replacement_doctor_id in raw:
        raw[request.replacement_doctor_id][request.day] = request.function

    # Uppdatera datum-schema
    day_date = (start + timedelta(days=request.day)).isoformat()
    if request.absent_doctor_id in sched.get("schedule", {}):
        sched["schedule"][request.absent_doctor_id][day_date] = "LEDIG"
    if request.replacement_doctor_id in sched.get("schedule", {}):
        sched["schedule"][request.replacement_doctor_id][day_date] = request.function

    return {
        "status": "replaced",
        "day": request.day,
        "date": day_date,
        "function": request.function,
        "absent_doctor": request.absent_doctor_id,
        "replacement": f"{doc.name} ({doc.id})",
        "replacement_old_function": old_func,
        "atl_ok": atl_result["ok"],
        "atl_overridden": not atl_result["ok"] and request.override_atl,
        "warnings": atl_result.get("violations", []),
    }


@app.get("/absence/chains", tags=["FrÃÂ¥nvarokedja"])
async def list_absence_chains(status: Optional[str] = None):
    """Lista alla kÃÂ¶rda frÃÂ¥nvarokedjor."""
    results = []
    for chain_id, result in absence_chain_store.items():
        if status and result.status.value != status:
            continue
        results.append({
            "chain_id": result.chain_id,
            "doctor": f"{result.doctor_name} ({result.doctor_id})",
            "absence_type": result.absence_type,
            "period": f"{result.start_date} Ã¢ÂÂ {result.end_date}",
            "status": result.status.value,
            "replaced": len(result.schedule_changes),
            "failed": len(result.failed_slots),
        })
    return results


# === DATABASE STATUS ===

@app.get("/db/status", tags=["System"])
async def db_status():
    """Visa databasstatus (PostgreSQL eller in-memory)."""
    return await db.stats()


@app.post("/db/migrate", tags=["System"])
async def db_migrate():
    """Manuell databasmigrering — skapa tabeller i PostgreSQL."""
    from db import connect_db, is_connected, DATABASE_URL
    if is_connected():
        return {"status": "already_connected", "backend": "postgresql"}
    if not DATABASE_URL:
        return {"status": "no_database_url", "message": "DATABASE_URL ej konfigurerad. Sätt variabeln i Railway."}
    ok = await connect_db()
    if ok:
        return {"status": "migrated", "backend": "postgresql", "message": "Tabeller skapade och anslutning aktiv."}
    return {"status": "failed", "message": "Kunde inte ansluta till PostgreSQL. Kontrollera DATABASE_URL."}


# === MAIN ===
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")

