"""
COP Engine — PostgreSQL Persistence Layer
==========================================
Async PostgreSQL-lager med asyncpg.

Driftslägen (env COP_DB_MODE):
  - (standard)  PostgreSQL är enda datakällan. HTTP 503 om DB är nere.
  - memory      In-memory dicts (för tester/dev). Sätt COP_DB_MODE=memory.

Tabeller:
  - users          — autentisering & RBAC
  - schedules      — genererade scheman (JSON)
  - jobs           — solver-jobb (status tracking)
  - absence_chains — frånvarokedjor
  - clinic_configs — klinikkonfigurationer
  - revoked_tokens — utloggade JWT-tokens
  - audit_log      — ändringslogg
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

logger = logging.getLogger("cop.db")

DATABASE_URL = os.getenv("DATABASE_URL", "")
_use_memory = os.getenv("COP_DB_MODE", "").lower() == "memory"

_pool = None


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    email TEXT,
    full_name TEXT,
    role TEXT NOT NULL DEFAULT 'viewer',
    doctor_id TEXT,
    hashed_password TEXT NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_login TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS schedules (
    schedule_id TEXT PRIMARY KEY,
    clinic_id TEXT,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS absence_chains (
    chain_id TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    status TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS clinic_configs (
    clinic_id TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    version INT DEFAULT 1,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS revoked_tokens (
    token TEXT PRIMARY KEY,
    revoked_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    action TEXT NOT NULL,
    user_id TEXT,
    details JSONB,
    timestamp TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_schedules_clinic ON schedules(clinic_id);
CREATE INDEX IF NOT EXISTS idx_schedules_created ON schedules(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chains_status ON absence_chains(status);
CREATE INDEX IF NOT EXISTS idx_chains_created ON absence_chains(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_revoked_expires ON revoked_tokens(expires_at);

CREATE TABLE IF NOT EXISTS ai_rules (
    id SERIAL PRIMARY KEY,
    clinic_id TEXT NOT NULL,
    rule_text TEXT NOT NULL,
    parsed JSONB,
    confidence REAL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ai_chat_history (
    id SERIAL PRIMARY KEY,
    clinic_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    message TEXT NOT NULL,
    response TEXT,
    action JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ai_predictions (
    id SERIAL PRIMARY KEY,
    clinic_id TEXT NOT NULL,
    period TEXT,
    predictions JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_rules_clinic ON ai_rules(clinic_id);
CREATE INDEX IF NOT EXISTS idx_ai_chat_clinic ON ai_chat_history(clinic_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_pred_clinic ON ai_predictions(clinic_id);

-- Migration: ensure ALL users columns exist (old schemas may miss any of these)
-- Using EXCEPTION WHEN OTHERS to catch any error, not just duplicate_column
DO $$ BEGIN ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT; EXCEPTION WHEN OTHERS THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT; EXCEPTION WHEN OTHERS THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'viewer'; EXCEPTION WHEN OTHERS THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE users ADD COLUMN IF NOT EXISTS doctor_id TEXT; EXCEPTION WHEN OTHERS THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE users ADD COLUMN IF NOT EXISTS hashed_password TEXT DEFAULT ''; EXCEPTION WHEN OTHERS THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE; EXCEPTION WHEN OTHERS THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(); EXCEPTION WHEN OTHERS THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMPTZ; EXCEPTION WHEN OTHERS THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE users ADD COLUMN IF NOT EXISTS password_change_required BOOLEAN DEFAULT FALSE; EXCEPTION WHEN OTHERS THEN NULL; END $$;

-- Migration: byt namn på token → token_hash i revoked_tokens (lagrar SHA-256-hash)
DO $$ BEGIN
  ALTER TABLE revoked_tokens RENAME COLUMN token TO token_hash;
EXCEPTION WHEN undefined_column THEN NULL;
END $$;

-- Migration: ensure columns exist for upgraded schema
DO $$ BEGIN
  ALTER TABLE jobs ADD COLUMN IF NOT EXISTS data JSONB;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
  ALTER TABLE schedules ADD COLUMN IF NOT EXISTS data JSONB;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

DO $$ BEGIN
  ALTER TABLE absence_chains ADD COLUMN IF NOT EXISTS data JSONB;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- Drop old columns if they exist (from initial schema)
DO $$ BEGIN
  ALTER TABLE jobs DROP COLUMN IF EXISTS params;
  ALTER TABLE jobs DROP COLUMN IF EXISTS result;
  ALTER TABLE jobs DROP COLUMN IF EXISTS status;
  ALTER TABLE jobs DROP COLUMN IF EXISTS updated_at;
EXCEPTION WHEN undefined_column THEN NULL;
END $$;
"""


# ---------------------------------------------------------------------------
# Connection Management
# ---------------------------------------------------------------------------

async def connect_db():
    global _pool
    if _use_memory:
        logger.info("COP_DB_MODE=memory — kör utan PostgreSQL (dev/test-läge)")
        return False
    if not DATABASE_URL:
        logger.warning("DATABASE_URL ej satt — faller tillbaka till in-memory")
        return False
    try:
        import asyncpg
        import asyncio
        # Use min_size=1 to avoid blocking on multiple connections at startup.
        # timeout=15 ensures we fail fast if PostgreSQL is unreachable.
        _pool = await asyncio.wait_for(
            asyncpg.create_pool(
                DATABASE_URL,
                min_size=1,
                max_size=10,
                command_timeout=15,
                timeout=10,           # per-connection acquisition timeout
            ),
            timeout=20,  # total timeout for pool creation
        )
        # Run schema migrations separately so pool survives even if migrations fail
        try:
            async with _pool.acquire() as conn:
                await asyncio.wait_for(conn.execute(SCHEMA_SQL), timeout=30)
            logger.info("PostgreSQL ansluten (pool min=1 max=10), schema OK")
        except asyncio.TimeoutError:
            logger.warning("Schema-migration timeout (30s) — appen fortsätter utan migration")
        except Exception as schema_err:
            logger.warning("Schema-migration hade problem (appen fortsätter): %s", schema_err)
        return True
    except asyncio.TimeoutError:
        logger.error("PostgreSQL pool creation timeout (20s) — faller tillbaka till in-memory")
        _pool = None
        return False
    except Exception as e:
        logger.error("PostgreSQL anslutning misslyckades: %s", e, exc_info=True)
        _pool = None
        return False


async def close_db():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL disconnected")


def is_connected():
    return _pool is not None


class CopDatabase:
    def __init__(self):
        self._schedules = {}
        self._jobs = {}
        self._configs = {}
        self._users = {}
        self._chains = {}
        self._revoked = set()
        self._versions = []       # Schema versions
        self._audit_log = []      # In-memory audit fallback
        self._notifications = {}  # {user_id: [notification_dicts]}
        self._base_schedules = {} # {id: BaseSchedule}
        self._deviations = []     # [ScheduleDeviation]
        self._comp_accounts = {}  # {doctor_id: CompTimeAccount}
        self._swap_requests = []  # [SwapRequest]

    @property
    def using_postgres(self):
        return _pool is not None

    @property
    def using_memory(self):
        return _use_memory

    @property
    def using_mongo(self):
        return False  # Bakåtkompatibilitet

    def _require_pool(self, op: str):
        """Kasta HTTP 503 om PostgreSQL ej tillgänglig och vi inte är i memory-mode."""
        if not _use_memory and not _pool:
            logger.error("PostgreSQL otillgänglig", extra={"operation": op})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")

    # ---- SCHEDULES -------------------------------------------------------

    async def save_schedule(self, data: dict) -> str:
        sid = data["schedule_id"]
        data["_updated_at"] = datetime.now(timezone.utc).isoformat()
        if _use_memory:
            self._schedules[sid] = data
            return sid
        self._require_pool("save_schedule")
        try:
            async with _pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """INSERT INTO schedules (schedule_id, clinic_id, data, updated_at)
                           VALUES ($1, $2, $3, NOW())
                           ON CONFLICT (schedule_id) DO UPDATE SET data = $3, updated_at = NOW()""",
                        sid, data.get("clinic_id"), json.dumps(data))
        except Exception as e:
            logger.error("save_schedule misslyckades", extra={"error": str(e), "schedule_id": sid})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")
        return sid

    async def get_schedule(self, schedule_id: str) -> Optional[dict]:
        if _use_memory:
            return self._schedules.get(schedule_id)
        self._require_pool("get_schedule")
        try:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT data FROM schedules WHERE schedule_id = $1", schedule_id)
                return json.loads(row["data"]) if row else None
        except Exception as e:
            logger.error("get_schedule misslyckades", extra={"error": str(e), "schedule_id": schedule_id})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")

    async def list_schedules(self, clinic_id: str = None, limit: int = 50) -> list:
        if _use_memory:
            items = list(self._schedules.values())
            if clinic_id:
                items = [s for s in items if s.get("clinic_id") == clinic_id]
            return items[-limit:]
        self._require_pool("list_schedules")
        try:
            async with _pool.acquire() as conn:
                if clinic_id:
                    rows = await conn.fetch(
                        "SELECT data FROM schedules WHERE clinic_id=$1 ORDER BY created_at DESC LIMIT $2",
                        clinic_id, limit)
                else:
                    rows = await conn.fetch(
                        "SELECT data FROM schedules ORDER BY created_at DESC LIMIT $1", limit)
                return [json.loads(r["data"]) for r in rows]
        except Exception as e:
            logger.error("list_schedules misslyckades", extra={"error": str(e)})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")

    async def delete_schedule(self, schedule_id: str) -> bool:
        if _use_memory:
            self._schedules.pop(schedule_id, None)
            return True
        self._require_pool("delete_schedule")
        try:
            async with _pool.acquire() as conn:
                r = await conn.execute(
                    "DELETE FROM schedules WHERE schedule_id = $1", schedule_id)
                return "DELETE 1" in r
        except Exception as e:
            logger.error("delete_schedule misslyckades", extra={"error": str(e)})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")

    async def get_all_schedules_raw(self) -> dict:
        if _use_memory:
            return dict(self._schedules)
        self._require_pool("get_all_schedules_raw")
        try:
            async with _pool.acquire() as conn:
                rows = await conn.fetch("SELECT data FROM schedules")
                return {json.loads(r["data"])["schedule_id"]: json.loads(r["data"]) for r in rows}
        except Exception as e:
            logger.error("get_all_schedules_raw misslyckades", extra={"error": str(e)})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")

    async def count_schedules(self) -> int:
        if _use_memory:
            return len(self._schedules)
        if not _pool:
            return 0
        try:
            return await _pool.fetchval("SELECT COUNT(*) FROM schedules")
        except Exception:
            return 0

    # ---- JOBS ------------------------------------------------------------

    async def save_job(self, data: dict) -> str:
        jid = data["job_id"]
        if _use_memory:
            self._jobs[jid] = data
            return jid
        self._require_pool("save_job")
        try:
            async with _pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO jobs (job_id, data) VALUES ($1, $2) ON CONFLICT (job_id) DO UPDATE SET data = $2",
                        jid, json.dumps(data))
        except Exception as e:
            logger.error("save_job misslyckades", extra={"error": str(e), "job_id": jid})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")
        return jid

    async def get_job(self, job_id: str) -> Optional[dict]:
        if _use_memory:
            return self._jobs.get(job_id)
        self._require_pool("get_job")
        try:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT data FROM jobs WHERE job_id = $1", job_id)
                return json.loads(row["data"]) if row else None
        except Exception as e:
            logger.error("get_job misslyckades", extra={"error": str(e), "job_id": job_id})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")

    async def update_job(self, job_id: str, updates: dict):
        if _use_memory:
            if job_id in self._jobs:
                self._jobs[job_id].update(updates)
            return
        self._require_pool("update_job")
        try:
            async with _pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "SELECT data FROM jobs WHERE job_id = $1", job_id)
                    if row:
                        data = json.loads(row["data"])
                        data.update(updates)
                        await conn.execute(
                            "UPDATE jobs SET data = $1 WHERE job_id = $2",
                            json.dumps(data), job_id)
        except Exception as e:
            logger.error("update_job misslyckades", extra={"error": str(e), "job_id": job_id})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")

    # ---- CONFIGS ---------------------------------------------------------

    async def save_config(self, clinic_id: str, config_data, user_id: str = None) -> str:
        """Spara klinikkonfiguration. config_data kan vara ClinicConfig-objekt eller dict."""
        if _use_memory:
            self._configs[clinic_id] = config_data
            return clinic_id
        self._require_pool("save_config")
        try:
            from data_model import config_to_dict, ClinicConfig
            data_dict = config_to_dict(config_data) if isinstance(config_data, ClinicConfig) else config_data
            async with _pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """INSERT INTO clinic_configs (clinic_id, data, updated_by, updated_at)
                           VALUES ($1, $2, $3, NOW())
                           ON CONFLICT (clinic_id) DO UPDATE SET
                             data = $2, version = clinic_configs.version + 1,
                             updated_by = $3, updated_at = NOW()""",
                        clinic_id, json.dumps(data_dict), user_id)
        except Exception as e:
            logger.error("save_config misslyckades", extra={"error": str(e), "clinic_id": clinic_id})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")
        return clinic_id

    async def get_config(self, clinic_id: str):
        """Hämta klinikkonfiguration."""
        if _use_memory:
            return self._configs.get(clinic_id)
        self._require_pool("get_config")
        try:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT data FROM clinic_configs WHERE clinic_id = $1", clinic_id)
                if row:
                    from data_model import dict_to_config
                    return dict_to_config(json.loads(row["data"]))
                return None
        except Exception as e:
            logger.error("get_config misslyckades", extra={"error": str(e), "clinic_id": clinic_id})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")

    async def delete_config(self, clinic_id: str):
        if _use_memory:
            self._configs.pop(clinic_id, None)
            return
        self._require_pool("delete_config")
        try:
            async with _pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM clinic_configs WHERE clinic_id = $1", clinic_id)
        except Exception as e:
            logger.error("delete_config misslyckades", extra={"error": str(e)})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")

    async def list_configs(self) -> list:
        """Lista alla klinikkonfigurationer."""
        if _use_memory:
            result = []
            for clinic_id, config in self._configs.items():
                result.append({
                    "clinic_id": clinic_id,
                    "name": config.name if hasattr(config, "name") else clinic_id,
                    "num_doctors": len(config.doctors) if hasattr(config, "doctors") else 0,
                    "num_rooms": len(config.operating_rooms) if hasattr(config, "operating_rooms") else 0,
                    "sites": config.sites if hasattr(config, "sites") else [],
                })
            return result
        self._require_pool("list_configs")
        try:
            from data_model import dict_to_config
            async with _pool.acquire() as conn:
                rows = await conn.fetch("SELECT clinic_id, data FROM clinic_configs")
            result = []
            for row in rows:
                config = dict_to_config(json.loads(row["data"]))
                result.append({
                    "clinic_id": row["clinic_id"],
                    "name": config.name if hasattr(config, "name") else row["clinic_id"],
                    "num_doctors": len(config.doctors) if hasattr(config, "doctors") else 0,
                    "num_rooms": len(config.operating_rooms) if hasattr(config, "operating_rooms") else 0,
                    "sites": config.sites if hasattr(config, "sites") else [],
                })
            return result
        except Exception as e:
            logger.error("list_configs misslyckades", extra={"error": str(e)})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")

    # ---- USERS -----------------------------------------------------------

    async def save_user(self, user_data: dict) -> str:
        uid = user_data["user_id"]
        if _use_memory:
            self._users[uid] = user_data
            return uid
        self._require_pool("save_user")
        try:
            async with _pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """INSERT INTO users (user_id, username, email, full_name, role, doctor_id,
                                             hashed_password, is_active, password_change_required)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                           ON CONFLICT (user_id) DO UPDATE SET
                             email = EXCLUDED.email, full_name = EXCLUDED.full_name,
                             role = EXCLUDED.role, doctor_id = EXCLUDED.doctor_id,
                             hashed_password = EXCLUDED.hashed_password,
                             is_active = EXCLUDED.is_active,
                             password_change_required = EXCLUDED.password_change_required""",
                        uid, user_data.get("username"), user_data.get("email"),
                        user_data.get("full_name"), user_data.get("role", "viewer"),
                        user_data.get("doctor_id"), user_data.get("hashed_password"),
                        user_data.get("is_active", True),
                        user_data.get("password_change_required", False))
        except Exception as e:
            logger.error("save_user misslyckades", extra={"error": str(e), "user_id": uid})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")
        return uid

    async def get_user(self, user_id: str) -> Optional[dict]:
        if _use_memory:
            return self._users.get(user_id)
        self._require_pool("get_user")
        try:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM users WHERE user_id = $1", user_id)
                return dict(row) if row else None
        except Exception as e:
            logger.error("get_user misslyckades", extra={"error": str(e)})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")

    async def get_user_by_username(self, username: str) -> Optional[dict]:
        if _use_memory:
            for u in self._users.values():
                if u.get("username") == username:
                    return u
            return None
        self._require_pool("get_user_by_username")
        try:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM users WHERE username = $1", username)
                return dict(row) if row else None
        except Exception as e:
            logger.error("get_user_by_username misslyckades", extra={"error": str(e)})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")

    async def list_users(self) -> list:
        if _use_memory:
            return [{k: v for k, v in u.items() if k != "hashed_password"}
                    for u in self._users.values()]
        self._require_pool("list_users")
        try:
            async with _pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT user_id, username, email, full_name, role, doctor_id, is_active FROM users")
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error("list_users misslyckades", extra={"error": str(e)})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")

    async def list_users_full(self) -> list:
        """Lista alla användare inklusive hashed_password (för cache-uppvärmning)."""
        if _use_memory:
            return list(self._users.values())
        self._require_pool("list_users_full")
        try:
            async with _pool.acquire() as conn:
                rows = await conn.fetch("SELECT * FROM users")
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error("list_users_full misslyckades", extra={"error": str(e)})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")

    async def delete_user(self, user_id: str) -> bool:
        if _use_memory:
            self._users.pop(user_id, None)
            return True
        self._require_pool("delete_user")
        try:
            async with _pool.acquire() as conn:
                r = await conn.execute(
                    "DELETE FROM users WHERE user_id = $1", user_id)
                return "DELETE 1" in r
        except Exception as e:
            logger.error("delete_user misslyckades", extra={"error": str(e)})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")

    # ---- ABSENCE CHAINS --------------------------------------------------

    async def save_chain(self, data: dict) -> str:
        cid = data.get("chain_id", data.get("id", "unknown"))
        data["chain_id"] = cid
        if _use_memory:
            self._chains[cid] = data
            return cid
        self._require_pool("save_chain")
        try:
            async with _pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """INSERT INTO absence_chains (chain_id, data, status)
                           VALUES ($1, $2, $3)
                           ON CONFLICT (chain_id) DO UPDATE SET data = $2, status = $3""",
                        cid, json.dumps(data), data.get("status"))
        except Exception as e:
            logger.error("save_chain misslyckades", extra={"error": str(e), "chain_id": cid})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")
        return cid

    async def get_chain(self, chain_id: str) -> Optional[dict]:
        if _use_memory:
            return self._chains.get(chain_id)
        self._require_pool("get_chain")
        try:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT data FROM absence_chains WHERE chain_id = $1", chain_id)
                return json.loads(row["data"]) if row else None
        except Exception as e:
            logger.error("get_chain misslyckades", extra={"error": str(e)})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")

    async def list_chains(self, status: str = None, limit: int = 100) -> list:
        if _use_memory:
            items = list(self._chains.values())
            if status:
                items = [c for c in items if c.get("status") == status]
            return items[-limit:]
        self._require_pool("list_chains")
        try:
            async with _pool.acquire() as conn:
                if status:
                    rows = await conn.fetch(
                        "SELECT data FROM absence_chains WHERE status=$1 ORDER BY created_at DESC LIMIT $2",
                        status, limit)
                else:
                    rows = await conn.fetch(
                        "SELECT data FROM absence_chains ORDER BY created_at DESC LIMIT $1", limit)
                return [json.loads(r["data"]) for r in rows]
        except Exception as e:
            logger.error("list_chains misslyckades", extra={"error": str(e)})
            raise HTTPException(503, "Databasen är temporärt otillgänglig")

    # ---- REVOKED TOKENS --------------------------------------------------

    async def revoke_token(self, token_hash: str, expires_at=None):
        self._revoked.add(token_hash)
        if _use_memory:
            return
        if not _pool:
            return  # Token-revokering är non-critical om DB är borta
        try:
            async with _pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO revoked_tokens (token_hash, expires_at) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    token_hash, expires_at)
        except Exception as e:
            logger.warning("revoke_token DB-fel (ignoreras): %s", e)

    async def is_token_revoked(self, token_hash: str) -> bool:
        if token_hash in self._revoked:
            return True
        if not _pool:
            return False
        try:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT 1 FROM revoked_tokens WHERE token_hash = $1", token_hash)
                if row:
                    self._revoked.add(token_hash)
                return row is not None
        except Exception as e:
            logger.warning("is_token_revoked DB-fel: %s", e)
            return False

    async def cleanup_expired_tokens(self):
        if not _pool:
            return
        try:
            async with _pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM revoked_tokens WHERE expires_at IS NOT NULL AND expires_at < NOW()")
        except Exception as e:
            logger.warning("cleanup_expired_tokens misslyckades: %s", e)

    # ---- AUDIT LOG -------------------------------------------------------

    async def audit(self, action: str, user_id: str = None, details: dict = None):
        entry = {
            "action": action, "user_id": user_id,
            "details": details or {}, "timestamp": datetime.now().isoformat()
        }
        self._audit_log.append(entry)
        if not _pool:
            return
        try:
            async with _pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO audit_log (action, user_id, details) VALUES ($1, $2, $3)",
                    action, user_id, json.dumps(details or {}))
        except Exception as e:
            logger.warning("audit DB-fel (ignoreras): %s", e)

    async def get_audit_log(self, action: str = None, user_id: str = None, limit: int = 50) -> list:
        if _pool:
            try:
                conditions = []
                params = []
                if action:
                    conditions.append(f"action = ${len(params)+1}")
                    params.append(action)
                if user_id:
                    conditions.append(f"user_id = ${len(params)+1}")
                    params.append(user_id)
                where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
                async with _pool.acquire() as conn:
                    rows = await conn.fetch(
                        f"SELECT action, user_id, details, timestamp FROM audit_log {where} ORDER BY timestamp DESC LIMIT ${len(params)+1}",
                        *params, limit)
                    return [{"action": r["action"], "user_id": r["user_id"],
                             "details": json.loads(r["details"]) if r["details"] else {},
                             "timestamp": r["timestamp"].isoformat()} for r in rows]
            except Exception as e:
                logger.warning("get_audit_log DB-fel: %s", e)
        logs = list(self._audit_log)
        if action:
            logs = [l for l in logs if l["action"] == action]
        if user_id:
            logs = [l for l in logs if l.get("user_id") == user_id]
        return list(reversed(logs[-limit:]))

    async def get_audit_stats(self) -> dict:
        from collections import Counter
        actions = Counter(l["action"] for l in self._audit_log)
        users = Counter(l.get("user_id", "system") for l in self._audit_log)
        return {
            "total": len(self._audit_log),
            "by_action": dict(actions),
            "by_user": dict(users.most_common(10))
        }

    # ---- VERSIONS (in-memory) --------------------------------------------

    async def save_version(self, schedule_id: str, version_data: dict) -> int:
        existing = [v for v in self._versions if v.get("schedule_id") == schedule_id]
        version_num = len(existing) + 1
        version_data["schedule_id"] = schedule_id
        version_data["version"] = version_num
        version_data["created_at"] = datetime.now().isoformat()
        self._versions.append(version_data)
        return version_num

    async def get_versions(self, schedule_id: str) -> list:
        return [v for v in self._versions if v.get("schedule_id") == schedule_id]

    async def get_version(self, schedule_id: str, version_num: int) -> Optional[dict]:
        for v in self._versions:
            if v.get("schedule_id") == schedule_id and v.get("version") == version_num:
                return v
        return None

    # ---- NOTIFICATIONS (in-memory) ----------------------------------------

    async def add_notification(self, user_id: str, notification: dict) -> dict:
        if user_id not in self._notifications:
            self._notifications[user_id] = []
        notification["id"] = f"notif_{len(self._notifications[user_id]) + 1}"
        notification["read"] = False
        notification["created_at"] = datetime.now().isoformat()
        self._notifications[user_id].append(notification)
        return notification

    async def get_notifications(self, user_id: str, unread_only: bool = False) -> list:
        notifs = self._notifications.get(user_id, [])
        if unread_only:
            notifs = [n for n in notifs if not n.get("read")]
        return list(reversed(notifs))

    async def mark_notification_read(self, user_id: str, notif_id: str) -> bool:
        for n in self._notifications.get(user_id, []):
            if n.get("id") == notif_id:
                n["read"] = True
                return True
        return False

    # ---- STATS -----------------------------------------------------------

    async def stats(self) -> dict:
        if _pool:
            try:
                async with _pool.acquire() as conn:
                    s_count = await conn.fetchval("SELECT COUNT(*) FROM schedules")
                    u_count = await conn.fetchval("SELECT COUNT(*) FROM users")
                    c_count = await conn.fetchval("SELECT COUNT(*) FROM absence_chains")
                return {
                    "backend": "postgresql",
                    "schedules": s_count, "users": u_count, "chains": c_count
                }
            except Exception as e:
                logger.warning("stats DB-fel: %s", e)
        return {
            "backend": "memory" if _use_memory else "unavailable",
            "schedules": len(self._schedules),
            "users": len(self._users),
            "chains": len(self._chains)
        }

    # ---- AI TABLES -------------------------------------------------------

    async def save_ai_rule(self, clinic_id: str, rule_text: str, parsed, confidence: float):
        if not _pool:
            return
        try:
            async with _pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO ai_rules (clinic_id, rule_text, parsed, confidence) VALUES ($1,$2,$3,$4)",
                    clinic_id, rule_text, json.dumps(parsed), confidence)
        except Exception as e:
            logger.warning("save_ai_rule misslyckades: %s", e)

    async def save_ai_chat(self, clinic_id: str, user_id: str, message: str, response: str, action=None):
        if not _pool:
            return
        try:
            async with _pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO ai_chat_history (clinic_id, user_id, message, response, action) VALUES ($1,$2,$3,$4,$5)",
                    clinic_id, user_id, message, response,
                    json.dumps(action) if action else None)
        except Exception as e:
            logger.warning("save_ai_chat misslyckades: %s", e)

    async def get_ai_chat_history(self, clinic_id: str, user_id: str, limit: int = 20) -> list:
        if not _pool:
            return []
        try:
            async with _pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT message, response, action FROM ai_chat_history "
                    "WHERE clinic_id=$1 AND user_id=$2 ORDER BY created_at DESC LIMIT $3",
                    clinic_id, user_id, limit)
                return [{"message": r["message"], "response": r["response"],
                         "action": json.loads(r["action"]) if r["action"] else None}
                        for r in reversed(rows)]
        except Exception as e:
            logger.warning("get_ai_chat_history misslyckades: %s", e)
            return []

    async def save_ai_prediction(self, clinic_id: str, period: str, predictions):
        if not _pool:
            return
        try:
            async with _pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO ai_predictions (clinic_id, period, predictions) VALUES ($1,$2,$3)",
                    clinic_id, period, json.dumps(predictions))
        except Exception as e:
            logger.warning("save_ai_prediction misslyckades: %s", e)


_instance = None


def get_db():
    global _instance
    if _instance is None:
        _instance = CopDatabase()
    return _instance

