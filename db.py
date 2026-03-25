"""
COP Engine — PostgreSQL Persistence Layer
==========================================
Async PostgreSQL-lager med asyncpg.

Tabeller:
  - users          — autentisering & RBAC
  - schedules      — genererade scheman (JSON)
  - jobs           — solver-jobb (status tracking)
  - absence_chains — frånvarokedjor
  - revoked_tokens — utloggade JWT-tokens
  - audit_log      — ändringslogg

Fallback: om PostgreSQL inte är tillgänglig, används in-memory dicts.
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("cop.db")

DATABASE_URL = os.getenv("DATABASE_URL", "")

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
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not set, using in-memory fallback")
        return False
    try:
        import asyncpg
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10, command_timeout=30)
        async with _pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        logger.info("PostgreSQL connected, tables created")
        return True
    except Exception as e:
        logger.warning(f"PostgreSQL unavailable ({e}), using in-memory fallback")
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

    @property
    def using_postgres(self):
        return _pool is not None

    @property
    def using_mongo(self):
        return False

    async def save_schedule(self, data):
        sid = data["schedule_id"]
        data["_updated_at"] = datetime.now(timezone.utc).isoformat()
        if _pool:
            async with _pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO schedules (schedule_id, clinic_id, data, updated_at)
                       VALUES ($1, $2, $3, NOW())
                       ON CONFLICT (schedule_id) DO UPDATE SET data = $3, updated_at = NOW()""",
                    sid, data.get("clinic_id"), json.dumps(data))
        self._schedules[sid] = data
        return sid

    async def get_schedule(self, schedule_id):
        if schedule_id in self._schedules:
            return self._schedules[schedule_id]
        if _pool:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow("SELECT data FROM schedules WHERE schedule_id = $1", schedule_id)
                if row:
                    data = json.loads(row["data"])
                    self._schedules[schedule_id] = data
                    return data
        return None

    async def list_schedules(self, clinic_id=None, limit=50):
        if _pool:
            async with _pool.acquire() as conn:
                if clinic_id:
                    rows = await conn.fetch("SELECT data FROM schedules WHERE clinic_id = $1 ORDER BY created_at DESC LIMIT $2", clinic_id, limit)
                else:
                    rows = await conn.fetch("SELECT data FROM schedules ORDER BY created_at DESC LIMIT $1", limit)
                return [json.loads(r["data"]) for r in rows]
        items = list(self._schedules.values())
        if clinic_id:
            items = [s for s in items if s.get("clinic_id") == clinic_id]
        return [{k: v for k, v in s.items() if k != "raw_schedule"} for s in items[-limit:]]

    async def delete_schedule(self, schedule_id):
        self._schedules.pop(schedule_id, None)
        if _pool:
            async with _pool.acquire() as conn:
                r = await conn.execute("DELETE FROM schedules WHERE schedule_id = $1", schedule_id)
                return "DELETE 1" in r
        return True

    async def get_all_schedules_raw(self):
        if _pool:
            async with _pool.acquire() as conn:
                rows = await conn.fetch("SELECT data FROM schedules")
                return {json.loads(r["data"])["schedule_id"]: json.loads(r["data"]) for r in rows}
        return dict(self._schedules)

    async def save_job(self, data):
        jid = data["job_id"]
        if _pool:
            async with _pool.acquire() as conn:
                await conn.execute("INSERT INTO jobs (job_id, data) VALUES ($1, $2) ON CONFLICT (job_id) DO UPDATE SET data = $2", jid, json.dumps(data))
        self._jobs[jid] = data
        return jid

    async def get_job(self, job_id):
        if job_id in self._jobs:
            return self._jobs[job_id]
        if _pool:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow("SELECT data FROM jobs WHERE job_id = $1", job_id)
                if row:
                    return json.loads(row["data"])
        return None

    async def update_job(self, job_id, updates):
        if job_id in self._jobs:
            self._jobs[job_id].update(updates)
        if _pool:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow("SELECT data FROM jobs WHERE job_id = $1", job_id)
                if row:
                    data = json.loads(row["data"])
                    data.update(updates)
                    await conn.execute("UPDATE jobs SET data = $1 WHERE job_id = $2", json.dumps(data), job_id)

    async def save_config(self, clinic_id, config_data, user_id=None):
        """Spara klinikkonfiguration. config_data kan vara ClinicConfig-objekt eller dict."""
        self._configs[clinic_id] = config_data
        # Persist to PostgreSQL
        if _pool:
            from data_model import config_to_dict, ClinicConfig
            data_dict = config_to_dict(config_data) if isinstance(config_data, ClinicConfig) else config_data
            async with _pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO clinic_configs (clinic_id, data, updated_by, updated_at)
                       VALUES ($1, $2, $3, NOW())
                       ON CONFLICT (clinic_id)
                       DO UPDATE SET data = $2, version = clinic_configs.version + 1,
                                     updated_by = $3, updated_at = NOW()""",
                    clinic_id, json.dumps(data_dict), user_id,
                )
        return clinic_id

    async def get_config(self, clinic_id):
        """Hämta config (minne först, PG som fallback)."""
        if clinic_id in self._configs:
            return self._configs[clinic_id]
        if _pool:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT data FROM clinic_configs WHERE clinic_id = $1", clinic_id
                )
                if row:
                    from data_model import dict_to_config
                    config = dict_to_config(json.loads(row["data"]))
                    self._configs[clinic_id] = config
                    return config
        return None

    async def delete_config(self, clinic_id):
        """Ta bort klinikkonfiguration."""
        self._configs.pop(clinic_id, None)
        if _pool:
            async with _pool.acquire() as conn:
                await conn.execute("DELETE FROM clinic_configs WHERE clinic_id = $1", clinic_id)

    async def list_configs(self):
        """Lista alla klinikkonfigurationer."""
        # Load from PG if memory is empty
        if not self._configs and _pool:
            async with _pool.acquire() as conn:
                rows = await conn.fetch("SELECT clinic_id, data FROM clinic_configs")
                for row in rows:
                    from data_model import dict_to_config
                    self._configs[row["clinic_id"]] = dict_to_config(json.loads(row["data"]))
        result = []
        for clinic_id, config in self._configs.items():
            result.append({
                "clinic_id": clinic_id,
                "name": config.name if hasattr(config, 'name') else clinic_id,
                "num_doctors": len(config.doctors) if hasattr(config, 'doctors') else 0,
                "num_rooms": len(config.operating_rooms) if hasattr(config, 'operating_rooms') else 0,
                "sites": config.sites if hasattr(config, 'sites') else [],
            })
        return result

    async def save_user(self, user_data):
        uid = user_data["user_id"]
        if _pool:
            async with _pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO users (user_id, username, email, full_name, role, doctor_id, hashed_password, is_active)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                       ON CONFLICT (user_id) DO UPDATE SET
                         email = EXCLUDED.email, full_name = EXCLUDED.full_name,
                         role = EXCLUDED.role, doctor_id = EXCLUDED.doctor_id,
                         hashed_password = EXCLUDED.hashed_password, is_active = EXCLUDED.is_active""",
                    uid, user_data.get("username"), user_data.get("email"),
                    user_data.get("full_name"), user_data.get("role", "viewer"),
                    user_data.get("doctor_id"), user_data.get("hashed_password"),
                    user_data.get("is_active", True))
        self._users[uid] = user_data
        return uid

    async def get_user(self, user_id):
        if user_id in self._users:
            return self._users[user_id]
        if _pool:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
                if row:
                    return dict(row)
        return None

    async def get_user_by_username(self, username):
        for u in self._users.values():
            if u.get("username") == username:
                return u
        if _pool:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM users WHERE username = $1", username)
                if row:
                    return dict(row)
        return None

    async def list_users(self):
        if _pool:
            async with _pool.acquire() as conn:
                rows = await conn.fetch("SELECT user_id, username, email, full_name, role, doctor_id, is_active FROM users")
                return [dict(r) for r in rows]
        return [{k: v for k, v in u.items() if k not in ("hashed_password",)} for u in self._users.values()]

    async def delete_user(self, user_id):
        self._users.pop(user_id, None)
        if _pool:
            async with _pool.acquire() as conn:
                r = await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
                return "DELETE 1" in r
        return True

    async def save_chain(self, data):
        cid = data.get("chain_id", data.get("id", "unknown"))
        data["chain_id"] = cid
        if _pool:
            async with _pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO absence_chains (chain_id, data, status) VALUES ($1, $2, $3) ON CONFLICT (chain_id) DO UPDATE SET data = $2, status = $3",
                    cid, json.dumps(data), data.get("status"))
        self._chains[cid] = data
        return cid

    async def get_chain(self, chain_id):
        if chain_id in self._chains:
            return self._chains[chain_id]
        if _pool:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow("SELECT data FROM absence_chains WHERE chain_id = $1", chain_id)
                if row:
                    return json.loads(row["data"])
        return None

    async def list_chains(self, status=None, limit=100):
        if _pool:
            async with _pool.acquire() as conn:
                if status:
                    rows = await conn.fetch("SELECT data FROM absence_chains WHERE status = $1 ORDER BY created_at DESC LIMIT $2", status, limit)
                else:
                    rows = await conn.fetch("SELECT data FROM absence_chains ORDER BY created_at DESC LIMIT $1", limit)
                return [json.loads(r["data"]) for r in rows]
        items = list(self._chains.values())
        if status:
            items = [c for c in items if c.get("status") == status]
        return items[-limit:]

    async def revoke_token(self, token, expires_at=None):
        if _pool:
            async with _pool.acquire() as conn:
                await conn.execute("INSERT INTO revoked_tokens (token, expires_at) VALUES ($1, $2) ON CONFLICT DO NOTHING", token, expires_at)
        self._revoked.add(token)

    async def is_token_revoked(self, token):
        if token in self._revoked:
            return True
        if _pool:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow("SELECT 1 FROM revoked_tokens WHERE token = $1", token)
                return row is not None
        return False

    async def audit(self, action, user_id=None, details=None):
        if _pool:
            async with _pool.acquire() as conn:
                await conn.execute("INSERT INTO audit_log (action, user_id, details) VALUES ($1, $2, $3)", action, user_id, json.dumps(details or {}))

    async def stats(self):
        if _pool:
            async with _pool.acquire() as conn:
                s_count = await conn.fetchval("SELECT COUNT(*) FROM schedules")
                u_count = await conn.fetchval("SELECT COUNT(*) FROM users")
                c_count = await conn.fetchval("SELECT COUNT(*) FROM absence_chains")
                return {"backend": "postgresql", "schedules": s_count, "users": u_count, "chains": c_count}
        return {"backend": "in-memory", "schedules": len(self._schedules), "users": len(self._users), "chains": len(self._chains)}


_instance = None


def get_db():
    global _instance
    if _instance is None:
        _instance = CopDatabase()
    return _instance

