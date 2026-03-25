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


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

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
"""


# ---------------------------------------------------------------------------
# Connection Management
# ---------------------------------------------------------------------------

async def connect_db():
    """Anslut till PostgreSQL. Anropas vid app-startup."""
    global _pool
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not set, using in-memory fallback")
        return False
    try:
        import asyncpg
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        async with _pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        logger.info(f"PostgreSQL connected, tables created")
        return True
    except Exception as e:
        logger.warning(f"PostgreSQL unavailable ({e}), using in-memory fallback")
        _pool = None
        return False


async def close_db():
    """Stäng PostgreSQL-anslutning."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL disconnected")


def is_connected() -> bool:
    return _pool is not None


# ---------------------------------------------------------------------------
# Database Interface
# ---------------------------------------------------------------------------

class CopDatabase:
    def __init__(self):
        # In-memory fallback stores
        self._schedules: dict = {}
        self._jobs: dict = {}
        self._configs: dict = {}
        self._users: dict = {}
        self._chains: dict = {}
        self._revoked: set = set()

    @property
    def using_postgres(self) -> bool:
        return _pool is not None

    @property
    def using_mongo(self) -> bool:
        return False  # Backward compat

    # ---- SCHEDULES --------------------------------------------------------

    async def save_schedule(self, data: dict) -> str:
        sid = data["schedule_id"]
        data["_updated_at"] = datetime.now(timezone.utc).isoformat()

        if _pool:
            async with _pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO schedules (schedule_id, clinic_id, data, updated_at)
                       VALUES ($1, $2, $3, NOW())
                       ON CONFLICT (schedule_id)
                       DO UPDATE SET data = $3, updated_at = NOW()""",
                    sid, data.get("clinic_id"), json.dumps(data),
                )
        # Always keep in memory too (for fast access)
        self._schedules[sid] = data
        return sid

    async def get_schedule(self, schedule_id: str) -> Optional[dict]:
        # Check memory first
        if schedule_id in self._schedules:
            return self._schedules[schedule_id]
        if _pool:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT data FROM schedules WHERE schedule_id = $1", schedule_id
                )
                if row:
                    data = json.loads(row["data"])
                    self._schedules[schedule_id] = data
                    return data
        return None

    async def list_schedules(self, clinic_id: str = None, limit: int = 50) -> list[dict]:
        if _pool:
            async with _pool.acquire() as conn:
                if clinic_id:
                    rows = await conn.fetch(
                        "SELECT data FROM schedules WHERE clinic_id = $1 ORDER BY created_at DESC LIMIT $2",
                        clinic_id, limit,
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT data FROM schedules ORDER BY created_at DESC LIMIT $1", limit
                    )
                results = []
                for row in rows:
                    d = json.loads(row["data"])
                    d.pop("raw_schedule", None)
                    results.append(d)
                return results
        items = list(self._schedules.values())
        if clinic_id:
            items = [s for s in items if s.get("clinic_id") == clinic_id]
        return [{k: v for k, v in s.items() if k != "raw_schedule"} for s in items[-limit:]]

    async def delete_schedule(self, schedule_id: str) -> bool:
        self._schedules.pop(schedule_id, None)
        if _pool:
            async with _pool.acquire() as conn:
                r = await conn.execute("DELETE FROM schedules WHERE schedule_id = $1", schedule_id)
                return "DELETE 1" in r
        return True

    async def get_all_schedules_raw(self) -> dict:
        if _pool:
            async with _pool.acquire() as conn:
                rows = await conn.fetch("SELECT data FROM schedules")
                return {json.loads(r["data"])["schedule_id"]: json.loads(r["data"]) for r in rows}
        return dict(self._schedules)

    # ---- JOBS -------------------------------------------------------------

    async def save_job(self, data: dict) -> str:
        jid = data["job_id"]
        if _pool:
            async with _pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO jobs (job_id, data) VALUES ($1, $2)
                       ON CONFLICT (job_id) DO UPDATE SET data = $2""",
                    jid, json.dumps(data),
                )
        self._jobs[jid] = data
        return jid

    async def get_job(self, job_id: str) -> Optional[dict]:
        if job_id in self._jobs:
            return self._jobs[job_id]
        if _pool:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow("SELECT data FROM jobs WHERE job_id = $1", job_id)
                if row:
                    return json.loads(row["data"])
        return None

    async def update_job(self, job_id: str, updates: dict):
        if job_id in self._jobs:
            self._jobs[job_id].update(updates)
        if _pool:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow("SELECT data FROM jobs WHERE job_id = $1", job_id)
                if row:
                    data = json.loads(row["data"])
                    data.update(updates)
                    await conn.execute("UPDATE jobs SET data = $1 WHERE job_id = $2", json.dumps(data), job_id)

    # ---- CONFIGS ----------------------------------------------------------

    async def save_config(self, clinic_id: str, config_data) -> str:
        self._configs[clinic_id] = config_data
        return clinic_id

    async def get_config(self, clinic_id: str):
        return self._configs.get(clinic_id)

    async def list_configs(self) -> list[dict]:
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

    # ---- USERS ------------------------------------------------------------

    async def save_user(self, user_data: dict) -> str:
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
                    user_data.get("is_active", True),
                )
        self._users[uid] = user_data
        return uid

    async def get_user(self, user_id: str) -> Optional[dict]:
        if user_id in self._users:
            return self._users[user_id]
        if _pool:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
                if row:
                    return dict(row)
        return None

    async def get_user_by_username(self, username: str) -> Optional[dict]:
        for u in self._users.values():
            if u.get("username") == username:
                return u
        if _pool:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM users WHERE username = $1", username)
                if row:
                    return dict(row)
        return None

    async def list_users(self) -> list[dict]:
        if _pool:
            async with _pool.acquire() as conn:
                rows = await conn.fetch("SELECT user_id, username, email, full_name, role, doctor_id, is_active FROM users")
                return [dict(r) for r in rows]
        return [
            {k: v for k, v in u.items() if k not in ("hashed_password",)}
            for u in self._users.values()
        ]

    async def delete_user(self, user_id: str) -> bool:
        self._users.pop(user_id, None)
        if _pool:
            async with _pool.acquire() as conn:
                r = await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
                return "DELETE 1" in r
        return True

    # ---- ABSENCE CHAINS ---------------------------------------------------

    async def save_chain(self, data: dict) -> str:
        cid = data.get("chain_id", data.get("id", "unknown"))
        data["chain_id"] = cid
        if _pool:
            async with _pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO absence_chains (chain_id, data, status) VALUES ($1, $2, $3)
                       ON CONFLICT (chain_id) DO UPDATE SET data = $2, status = $3""",
                    cid, json.dumps(data), data.get("status"),
                )
        self._chains[cid] = data
        return cid

    async def get_chain(self, chain_id: str) -> Optional[dict]:
        if chain_id in self._chains:
            return self._chains[chain_id]
        if _pool:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow("SELECT data FROM absence_chains WHERE chain_id = $1", chain_id)
                if row:
                    return json.loads(row["data"])
        return None

    async def list_chains(self, status: str = None, limit: int = 100) -> list[dict]:
        if _pool:
            async with _pool.acquire() as conn:
                if status:
                    rows = await conn.fetch(
                        "SELECT data FROM absence_chains WHERE status = $1 ORDER BY created_at DESC LIMIT $2",
                        status, limit,
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT data FROM absence_chains ORDER BY created_at DESC LIMIT $1", limit
                    )
                return [json.loads(r["data"]) for r in rows]
        items = list(self._chains.values())
        if status:
            items = [c for c in items if c.get("status") == status]
        return items[-limit:]

    # ---- REVOKED TOKENS ---------------------------------------------------

    async def revoke_token(self, token: str, expires_at: datetime = None):
        if _pool:
            async with _pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO revoked_tokens (token, expires_at) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    token, expires_at,
                )
        self._revoked.add(token)

    async def is_token_revoked(self, token: str) -> bool:
        if token in self._revoked:
            return True
        if _pool:
            async with _pool.acquire() as conn:
                row = await conn.fetchrow("SELECT 1 FROM revoked_tokens WHERE token = $1", token)
                return row is not None
        return False

    # ---- AUDIT LOG --------------------------------------------------------

    async def audit(self, action: str, user_id: str = None, details: dict = None):
        if _pool:
            async with _pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO audit_log (action, user_id, details) VALUES ($1, $2, $3)",
                    action, user_id, json.dumps(details or {}),
                )

    # ---- STATS ------------------------------------------------------------

    async def stats(self) -> dict:
        if _pool:
            async with _pool.acquire() as conn:
                s_count = await conn.fetchval("SELECT COUNT(*) FROM schedules")
                u_count = await conn.fetchval("SELECT COUNT(*) FROM users")
                c_count = await conn.fetchval("SELECT COUNT(*) FROM absence_chains")
                return {
                    "backend": "postgresql",
                    "schedules": s_count,
                    "users": u_count,
                    "chains": c_count,
                }
        return {
            "backend": "in-memory",
            "schedules": len(self._schedules),
            "users": len(self._users),
            "chains": len(self._chains),
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[CopDatabase] = None


def get_db() -> CopDatabase:
    global _instance
    if _instance is None:
        _instance = CopDatabase()
    return _instance
