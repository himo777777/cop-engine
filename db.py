"""
COP Engine — MongoDB Persistence Layer
=======================================
Async databaslager med motor (MongoDB async driver).

Alla collections:
  - schedules    — genererade scheman
  - jobs         — solver-jobb (status tracking)
  - configs      — klinikkonfigurationer
  - users        — autentisering & RBAC
  - absence_chains — frånvarokedjor
  - revoked_tokens — utloggade JWT-tokens
  - audit_log    — ändringslogg

Fallback: om MongoDB inte är tillgänglig, används in-memory dicts
(bakåtkompatibelt med befintliga tester).

Användning:
    from db import get_db
    db = get_db()
    await db.save_schedule(schedule_data)
    schedule = await db.get_schedule(schedule_id)
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("cop.db")

# ---------------------------------------------------------------------------
# MongoDB Connection
# ---------------------------------------------------------------------------

MONGO_URI = os.getenv("COP_MONGO_URI", os.getenv("MONGO_URI", "mongodb://localhost:27017"))
MONGO_DB = os.getenv("COP_MONGO_DB", os.getenv("MONGO_DB", "cop"))

_client = None
_database = None


async def connect_db():
    """Anslut till MongoDB. Anropas vid app-startup."""
    global _client, _database
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        _client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        # Verify connection
        await _client.admin.command("ping")
        _database = _client[MONGO_DB]

        # Create indexes
        await _database.schedules.create_index("schedule_id", unique=True)
        await _database.schedules.create_index("created_at")
        await _database.jobs.create_index("job_id", unique=True)
        await _database.configs.create_index("clinic_id", unique=True)
        await _database.users.create_index("user_id", unique=True)
        await _database.users.create_index("username", unique=True)
        await _database.absence_chains.create_index("chain_id", unique=True)
        await _database.absence_chains.create_index("created_at")
        await _database.revoked_tokens.create_index("token")
        await _database.revoked_tokens.create_index("expires_at", expireAfterSeconds=0)  # TTL
        await _database.audit_log.create_index("timestamp")

        logger.info(f"MongoDB connected: {MONGO_URI}/{MONGO_DB}")
        return True
    except Exception as e:
        logger.warning(f"MongoDB unavailable ({e}), using in-memory fallback")
        _client = None
        _database = None
        return False


async def close_db():
    """Stäng MongoDB-anslutning."""
    global _client, _database
    if _client:
        _client.close()
        _client = None
        _database = None
        logger.info("MongoDB disconnected")


def is_connected() -> bool:
    """Returnera True om MongoDB är anslutet."""
    return _database is not None


# ---------------------------------------------------------------------------
# Database Interface — abstraktion som fungerar med MongoDB ELLER in-memory
# ---------------------------------------------------------------------------

class CopDatabase:
    """
    Unified database interface.
    Uses MongoDB when available, falls back to in-memory dicts.
    """

    def __init__(self):
        # In-memory fallback stores
        self._schedules: dict = {}
        self._jobs: dict = {}
        self._configs: dict = {}
        self._users: dict = {}
        self._chains: dict = {}
        self._revoked: set = set()

    @property
    def _db(self):
        return _database

    @property
    def using_mongo(self) -> bool:
        return _database is not None

    # ---- SCHEDULES --------------------------------------------------------

    async def save_schedule(self, data: dict) -> str:
        """Spara genererat schema. Returnerar schedule_id."""
        sid = data["schedule_id"]
        data["_updated_at"] = datetime.now(timezone.utc).isoformat()

        if self._db:
            await self._db.schedules.replace_one(
                {"schedule_id": sid}, data, upsert=True
            )
        else:
            self._schedules[sid] = data
        return sid

    async def get_schedule(self, schedule_id: str) -> Optional[dict]:
        if self._db:
            doc = await self._db.schedules.find_one(
                {"schedule_id": schedule_id}, {"_id": 0}
            )
            return doc
        return self._schedules.get(schedule_id)

    async def list_schedules(self, clinic_id: str = None, limit: int = 50) -> list[dict]:
        if self._db:
            query = {"clinic_id": clinic_id} if clinic_id else {}
            cursor = self._db.schedules.find(query, {"_id": 0, "raw_schedule": 0}) \
                .sort("created_at", -1).limit(limit)
            return await cursor.to_list(length=limit)
        else:
            items = list(self._schedules.values())
            if clinic_id:
                items = [s for s in items if s.get("clinic_id") == clinic_id]
            # Return without raw_schedule
            return [
                {k: v for k, v in s.items() if k != "raw_schedule"}
                for s in items[-limit:]
            ]

    async def delete_schedule(self, schedule_id: str) -> bool:
        if self._db:
            r = await self._db.schedules.delete_one({"schedule_id": schedule_id})
            return r.deleted_count > 0
        return self._schedules.pop(schedule_id, None) is not None

    async def get_all_schedules_raw(self) -> dict:
        """Returnera alla scheman som dict (för intern användning)."""
        if self._db:
            cursor = self._db.schedules.find({}, {"_id": 0})
            docs = await cursor.to_list(length=1000)
            return {d["schedule_id"]: d for d in docs}
        return dict(self._schedules)

    # ---- JOBS -------------------------------------------------------------

    async def save_job(self, data: dict) -> str:
        jid = data["job_id"]
        if self._db:
            await self._db.jobs.replace_one(
                {"job_id": jid}, data, upsert=True
            )
        else:
            self._jobs[jid] = data
        return jid

    async def get_job(self, job_id: str) -> Optional[dict]:
        if self._db:
            return await self._db.jobs.find_one({"job_id": job_id}, {"_id": 0})
        return self._jobs.get(job_id)

    async def update_job(self, job_id: str, updates: dict):
        if self._db:
            await self._db.jobs.update_one({"job_id": job_id}, {"$set": updates})
        else:
            if job_id in self._jobs:
                self._jobs[job_id].update(updates)

    # ---- CONFIGS ----------------------------------------------------------

    async def save_config(self, clinic_id: str, config_data) -> str:
        """Spara klinikkonfiguration. config_data kan vara ClinicConfig eller dict."""
        data = {"clinic_id": clinic_id, "config": config_data, "_updated_at": datetime.now(timezone.utc).isoformat()}
        if self._db:
            # ClinicConfig is not JSON-serializable, store the clinic_id as reference
            # and keep the actual object in memory too
            self._configs[clinic_id] = config_data
            await self._db.configs.replace_one(
                {"clinic_id": clinic_id},
                {"clinic_id": clinic_id, "_updated_at": data["_updated_at"]},
                upsert=True
            )
        else:
            self._configs[clinic_id] = config_data
        return clinic_id

    async def get_config(self, clinic_id: str):
        """Returnera ClinicConfig-objekt (alltid från minne, pga komplext objekt)."""
        return self._configs.get(clinic_id)

    async def list_configs(self) -> list[dict]:
        """Lista alla klinikkonfigurationer (ID + namn)."""
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
        if self._db:
            await self._db.users.replace_one(
                {"user_id": uid}, user_data, upsert=True
            )
        else:
            self._users[uid] = user_data
        return uid

    async def get_user(self, user_id: str) -> Optional[dict]:
        if self._db:
            return await self._db.users.find_one({"user_id": user_id}, {"_id": 0})
        return self._users.get(user_id)

    async def get_user_by_username(self, username: str) -> Optional[dict]:
        if self._db:
            return await self._db.users.find_one({"username": username}, {"_id": 0})
        for u in self._users.values():
            if u.get("username") == username:
                return u
        return None

    async def list_users(self) -> list[dict]:
        if self._db:
            cursor = self._db.users.find({}, {"_id": 0, "password_hash": 0, "password_salt": 0})
            return await cursor.to_list(length=500)
        return [
            {k: v for k, v in u.items() if k not in ("password_hash", "password_salt")}
            for u in self._users.values()
        ]

    async def delete_user(self, user_id: str) -> bool:
        if self._db:
            r = await self._db.users.delete_one({"user_id": user_id})
            self._users.pop(user_id, None)
            return r.deleted_count > 0
        return self._users.pop(user_id, None) is not None

    # ---- ABSENCE CHAINS ---------------------------------------------------

    async def save_chain(self, data: dict) -> str:
        cid = data.get("chain_id", data.get("id", "unknown"))
        data_copy = dict(data)
        data_copy["chain_id"] = cid
        if self._db:
            await self._db.absence_chains.replace_one(
                {"chain_id": cid}, data_copy, upsert=True
            )
        else:
            self._chains[cid] = data_copy
        return cid

    async def get_chain(self, chain_id: str) -> Optional[dict]:
        if self._db:
            return await self._db.absence_chains.find_one({"chain_id": chain_id}, {"_id": 0})
        return self._chains.get(chain_id)

    async def list_chains(self, status: str = None, limit: int = 100) -> list[dict]:
        if self._db:
            query = {"status": status} if status else {}
            cursor = self._db.absence_chains.find(query, {"_id": 0}) \
                .sort("created_at", -1).limit(limit)
            return await cursor.to_list(length=limit)
        items = list(self._chains.values())
        if status:
            items = [c for c in items if c.get("status") == status]
        return items[-limit:]

    # ---- REVOKED TOKENS ---------------------------------------------------

    async def revoke_token(self, token: str, expires_at: datetime = None):
        if self._db:
            doc = {"token": token, "revoked_at": datetime.now(timezone.utc)}
            if expires_at:
                doc["expires_at"] = expires_at
            await self._db.revoked_tokens.insert_one(doc)
        else:
            self._revoked.add(token)

    async def is_token_revoked(self, token: str) -> bool:
        if self._db:
            doc = await self._db.revoked_tokens.find_one({"token": token})
            return doc is not None
        return token in self._revoked

    # ---- AUDIT LOG --------------------------------------------------------

    async def audit(self, action: str, user_id: str = None, details: dict = None):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "user_id": user_id,
            "details": details or {},
        }
        if self._db:
            await self._db.audit_log.insert_one(entry)
        # In-memory: skip audit log (not critical)

    # ---- STATS ------------------------------------------------------------

    async def stats(self) -> dict:
        if self._db:
            return {
                "backend": "mongodb",
                "uri": MONGO_URI.split("@")[-1] if "@" in MONGO_URI else MONGO_URI,
                "database": MONGO_DB,
                "schedules": await self._db.schedules.count_documents({}),
                "users": await self._db.users.count_documents({}),
                "chains": await self._db.absence_chains.count_documents({}),
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
    """Hämta databas-instans (singleton)."""
    global _instance
    if _instance is None:
        _instance = CopDatabase()
    return _instance
