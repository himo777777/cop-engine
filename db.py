"""
COP Engine -- PostgreSQL Persistence Layer
==========================================
Async PostgreSQL-lager med asyncpg.

Tabeller:
  - users       -- autentisering & RBAC
  - schedules   -- genererade scheman (JSON)
  - jobs        -- solver-jobb (status tracking)
  - absence_chains -- franvarokedjor
  - revoked_tokens -- utloggade JWT-tokens
  - audit_log   -- andringslogg

Fallback: om PostgreSQL inte ar tillganglig, anvands in-memory dicts.
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("cop.db")

DATABASE_URL = (
  os.getenv("DATABASE_URL")
  or os.getenv("DATABASE_PRIVATE_URL")
  or os.getenv("DATABASE_PUBLIC_URL")
  or os.getenv("POSTGRES_URL")
  or ""
)

if DATABASE_URL.startswith("postgres://"):
  DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

_pool = None

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
  user_id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'viewer',
  clinic_id TEXT, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS schedules (
  schedule_id TEXT PRIMARY KEY, clinic_id TEXT NOT NULL,
  week TEXT NOT NULL, data JSONB NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(), created_by TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
  params JSONB, result JSONB,
  created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS absence_chains (
  chain_id TEXT PRIMARY KEY, data JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS revoked_tokens (
  token TEXT PRIMARY KEY, expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id SERIAL PRIMARY KEY, action TEXT NOT NULL,
  user_id TEXT, details JSONB, timestamp TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_schedules_clinic ON schedules(clinic_id);
CREATE INDEX IF NOT EXISTS idx_schedules_created ON schedules(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chains_status ON absence_chains(status);
CREATE INDEX IF NOT EXISTS idx_chains_created ON absence_chains(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_revoked_expires ON revoked_tokens(expires_at);
"""

async def _get_pool():
  global _pool
  if _pool is None and DATABASE_URL:
    try:
      import asyncpg
      _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
      logger.info("PostgreSQL pool created")
    except Exception as e:
      logger.warning("Could not create PG pool: %s", e)
  return _pool

async def init_schema():
  pool = await _get_pool()
  if pool:
    async with pool.acquire() as conn:
      await conn.execute(SCHEMA_SQL)
    logger.info("Schema initialized")
    return True
  return False

class CopDatabase:
  def __init__(self):
    self._schedules = {}
    self._jobs = {}
    self._users = {}
    self._chains = {}
    self._configs = {}
    self._revoked = set()

  async def save_schedule(self, sid, clinic_id, week, data, user=None):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        await conn.execute("INSERT INTO schedules (schedule_id, clinic_id, week, data, created_by) VALUES ($1,$2,$3,$4,$5) ON CONFLICT (schedule_id) DO UPDATE SET data=$4, created_by=$5", sid, clinic_id, week, json.dumps(data), user)
    else:
      self._schedules[sid] = {"schedule_id": sid, "clinic_id": clinic_id, "week": week, "data": data, "created_by": user, "created_at": datetime.now(timezone.utc).isoformat()}

  async def get_schedule(self, sid):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM schedules WHERE schedule_id=$1", sid)
        if row:
          r = dict(row)
          if isinstance(r.get("data"), str): r["data"] = json.loads(r["data"])
          return r
        return None
    return self._schedules.get(sid)

  async def list_schedules(self, clinic_id=None, limit=50):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        if clinic_id:
          rows = await conn.fetch("SELECT * FROM schedules WHERE clinic_id=$1 ORDER BY created_at DESC LIMIT $2", clinic_id, limit)
        else:
          rows = await conn.fetch("SELECT * FROM schedules ORDER BY created_at DESC LIMIT $1", limit)
        out = []
        for row in rows:
          r = dict(row)
          if isinstance(r.get("data"), str): r["data"] = json.loads(r["data"])
          out.append(r)
        return out
    vals = list(self._schedules.values())
    if clinic_id: vals = [v for v in vals if v.get("clinic_id") == clinic_id]
    return vals[:limit]

  async def delete_schedule(self, sid):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        await conn.execute("DELETE FROM schedules WHERE schedule_id=$1", sid)
    else:
      self._schedules.pop(sid, None)

  async def get_all_schedules_raw(self):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        return [dict(r) for r in await conn.fetch("SELECT * FROM schedules ORDER BY created_at DESC")]
    return list(self._schedules.values())

  async def save_job(self, jid, status="pending", params=None, result=None):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        await conn.execute("INSERT INTO jobs (job_id,status,params,result) VALUES ($1,$2,$3,$4) ON CONFLICT (job_id) DO UPDATE SET status=$2,result=$4,updated_at=now()", jid, status, json.dumps(params) if params else None, json.dumps(result) if result else None)
    else:
      self._jobs[jid] = {"job_id": jid, "status": status, "params": params, "result": result}

  async def get_job(self, jid):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM jobs WHERE job_id=$1", jid)
        return dict(row) if row else None
    return self._jobs.get(jid)

  async def update_job(self, jid, status=None, result=None):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        if status and result:
          await conn.execute("UPDATE jobs SET status=$2,result=$3,updated_at=now() WHERE job_id=$1", jid, status, json.dumps(result))
        elif status:
          await conn.execute("UPDATE jobs SET status=$2,updated_at=now() WHERE job_id=$1", jid, status)
    else:
      if jid in self._jobs:
        if status: self._jobs[jid]["status"] = status
        if result: self._jobs[jid]["result"] = result

  async def save_config(self, key, value): self._configs[key] = value
  async def get_config(self, key): return self._configs.get(key)
  async def list_configs(self): return dict(self._configs)

  async def save_user(self, uid, username, pw_hash, role="viewer", clinic_id=None):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id,username,password_hash,role,clinic_id) VALUES ($1,$2,$3,$4,$5) ON CONFLICT (user_id) DO UPDATE SET password_hash=$3,role=$4,clinic_id=$5", uid, username, pw_hash, role, clinic_id)
    else:
      self._users[uid] = {"user_id": uid, "username": username, "password_hash": pw_hash, "role": role, "clinic_id": clinic_id}

  async def get_user(self, uid):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)
        return dict(row) if row else None
    return self._users.get(uid)

  async def get_user_by_username(self, username):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE username=$1", username)
        return dict(row) if row else None
    for u in self._users.values():
      if u.get("username") == username: return u
    return None

  async def list_users(self):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        return [dict(r) for r in await conn.fetch("SELECT user_id,username,role,clinic_id,created_at FROM users ORDER BY created_at")]
    return list(self._users.values())

  async def delete_user(self, uid):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE user_id=$1", uid)
    else:
      self._users.pop(uid, None)

  async def save_chain(self, cid, data, status="active"):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        await conn.execute("INSERT INTO absence_chains (chain_id,data,status) VALUES ($1,$2,$3) ON CONFLICT (chain_id) DO UPDATE SET data=$2,status=$3", cid, json.dumps(data), status)
    else:
      self._chains[cid] = {"chain_id": cid, "data": data, "status": status, "created_at": datetime.now(timezone.utc).isoformat()}

  async def get_chain(self, cid):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM absence_chains WHERE chain_id=$1", cid)
        if row:
          r = dict(row)
          if isinstance(r.get("data"), str): r["data"] = json.loads(r["data"])
          return r
        return None
    return self._chains.get(cid)

  async def list_chains(self, status=None, limit=100):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        if status:
          rows = await conn.fetch("SELECT * FROM absence_chains WHERE status=$1 ORDER BY created_at DESC LIMIT $2", status, limit)
        else:
          rows = await conn.fetch("SELECT * FROM absence_chains ORDER BY created_at DESC LIMIT $1", limit)
        out = []
        for row in rows:
          r = dict(row)
          if isinstance(r.get("data"), str): r["data"] = json.loads(r["data"])
          out.append(r)
        return out
    vals = list(self._chains.values())
    if status: vals = [v for v in vals if v.get("status") == status]
    return vals[:limit]

  async def revoke_token(self, token, expires_at):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        await conn.execute("INSERT INTO revoked_tokens (token,expires_at) VALUES ($1,$2) ON CONFLICT DO NOTHING", token, expires_at)
    else:
      self._revoked.add(token)

  async def is_token_revoked(self, token):
    if token in self._revoked: return True
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM revoked_tokens WHERE token=$1", token)
        return row is not None
    return False

  async def audit(self, action, user_id=None, details=None):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        await conn.execute("INSERT INTO audit_log (action,user_id,details) VALUES ($1,$2,$3)", action, user_id, json.dumps(details or {}))

  async def stats(self):
    pool = await _get_pool()
    if pool:
      async with pool.acquire() as conn:
        s = await conn.fetchval("SELECT COUNT(*) FROM schedules")
        u = await conn.fetchval("SELECT COUNT(*) FROM users")
        c = await conn.fetchval("SELECT COUNT(*) FROM absence_chains")
        return {"backend": "postgresql", "schedules": s, "users": u, "chains": c}
    return {"backend": "in-memory", "schedules": len(self._schedules), "users": len(self._users), "chains": len(self._chains)}

_instance = None

def get_db():
  global _instance
  if _instance is None: _instance = CopDatabase()
  return _instance

async def connect_db():
  pool = await _get_pool()
  if pool:
    await init_schema()
    logger.info("DB connected and schema ready")
    return True
  logger.warning("No DATABASE_URL -- using in-memory fallback")
  return False

async def close_db():
  global _pool
  if _pool:
    await _pool.close()
    _pool = None
    logger.info("DB pool closed")

def is_connected():
  return _pool is not None
