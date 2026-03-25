"""
COP Tessa Adapter — Koppling till Tessa (Sematic AB)
=====================================================
Tessa kör på Meteor 2.12 med MongoDB. Denna adapter läser och skriver
direkt till Tessas MongoDB-databas.

Två anslutningsmetoder:
  1. Direkt MongoDB-access (rekommenderat för server-to-server)
  2. DDP-protokollet (Meteors websocket-protokoll, för realtidssynk)

Kända Meteor/MongoDB-mönster som Tessa sannolikt följer:
  - Users-collection med Meteor.users-schema
  - Prefix-namngivning: t.ex. 'schedules', 'shifts', 'departments'
  - _id-fält som String (Meteor Random.id()), inte ObjectId
  - createdAt/updatedAt timestamps
  - Meteor-specifika fält: services, profile, etc i Users

Adapterkonfiguration kräver:
  - MongoDB connection string (t.ex. mongodb://tessa-server:27017/tessa)
  - Alternativt: DDP-endpoint (t.ex. wss://tessa.example.com/websocket)
  - Mappning av Tessas roll/funktionskoder → COP-format
"""

import asyncio
from datetime import date, datetime, timedelta
from typing import Optional
import sys
import os

# Lägg till parent dir i path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_model import (
    Doctor, OperatingRoom, Role, ShiftType, Function,
    StaffingRequirement, CallStructure, ATLRules, ClinicConfig,
)
from adapters.base import (
    BaseAdapter, AdapterConfig, AdapterType, SyncDirection,
    SyncResult, ExternalScheduleEntry,
)


# === TESSA COLLECTION-NAMN (bästa gissningar baserat på Meteor-patterns) ===
# Dessa konfigureras per installation — varje sjukhus kan ha anpassningar
TESSA_COLLECTIONS = {
    "users": "users",                 # Meteor.users (alltid detta namn)
    "staff": "staff",                 # Personalregister (utökat)
    "schedules": "schedules",         # Schemaperioder
    "shifts": "shifts",               # Enskilda skift/pass
    "departments": "departments",     # Avdelningar/kliniker
    "rooms": "rooms",                 # Salar/resurser
    "absences": "absences",           # Frånvaro
    "shift_types": "shiftTypes",      # Skifttyper (dag, natt, jour, etc.)
    "templates": "scheduleTemplates", # Schemamallar
}


class TessaAdapter(BaseAdapter):
    """
    Adapter för Tessa schemaläggningssystem.

    Ansluter direkt till Tessas MongoDB-databas och mappar
    Tessas dataformat till COP:s universella datamodell.
    """
    # Kristianstad-specifik adapter för Tessa-systemet

    def __init__(self, config: AdapterConfig):
        super().__init__(config)
        self.db = None
        self.client = None
        self._collection_cache = {}

        # Tessa-specifik konfiguration
        self.department_filter = config.role_mapping.get("_department", None)

    # === ANSLUTNING ===

    async def connect(self) -> bool:
        """Anslut till Tessas MongoDB."""
        try:
            from motor.motor_asyncio import AsyncIOMotorClient

            conn_string = f"mongodb://{self.config.host}:{self.config.port}"
            if self.config.username and self.config.password:
                conn_string = f"mongodb://{self.config.username}:{self.config.password}@{self.config.host}:{self.config.port}"

            self.client = AsyncIOMotorClient(conn_string, serverSelectionTimeoutMS=5000)
            self.db = self.client[self.config.database]

            # Testa anslutningen
            await self.client.admin.command("ping")
            self._connected = True
            return True

        except ImportError:
            print("⚠️  Motor (async MongoDB driver) ej installerat.")
            print("   pip install motor --break-system-packages")
            return False
        except Exception as e:
            print(f"❌ Kunde inte ansluta till Tessa MongoDB: {e}")
            self._connected = False
            return False

    async def disconnect(self):
        """Stäng MongoDB-anslutning."""
        if self.client:
            self.client.close()
            self._connected = False

    async def test_connection(self) -> dict:
        """Testa anslutning och samla systeminfo."""
        if not self._connected:
            connected = await self.connect()
            if not connected:
                return {"connected": False, "error": "Kunde inte ansluta"}

        try:
            # Hämta grundläggande info
            server_info = await self.client.server_info()
            db_stats = await self.db.command("dbstats")

            # Räkna poster i nyckelcollections
            collections_info = {}
            for name, coll_name in TESSA_COLLECTIONS.items():
                try:
                    count = await self.db[coll_name].estimated_document_count()
                    collections_info[name] = count
                except Exception:
                    collections_info[name] = -1  # Collection finns inte

            return {
                "connected": True,
                "system_name": "Tessa (Sematic AB)",
                "system_version": f"MongoDB {server_info.get('version', '?')}",
                "database": self.config.database,
                "database_size_mb": round(db_stats.get("dataSize", 0) / 1024 / 1024, 1),
                "collections": collections_info,
                "last_sync": None,
            }

        except Exception as e:
            return {"connected": False, "error": str(e)}

    # === COLLECTION-ACCESS ===

    def _coll(self, name: str):
        """Hämta MongoDB-collection med cachning."""
        coll_name = TESSA_COLLECTIONS.get(name, name)
        if coll_name not in self._collection_cache:
            self._collection_cache[coll_name] = self.db[coll_name]
        return self._collection_cache[coll_name]

    # === PULL: Läkare ===

    async def pull_doctors(self) -> list[Doctor]:
        """
        Hämta läkare från Tessas databas.

        Tessa lagrar personal i antingen 'users' (Meteor.users) eller
        en separat 'staff'-collection. Vi provar båda.

        Förväntad Meteor-struktur i users:
        {
            _id: "random17chars",
            username: "eriksson.m",
            emails: [{address: "...", verified: true}],
            profile: {
                firstName: "Maria",
                lastName: "Eriksson",
                title: "Överläkare",
                department: "Ortopedi",
                employmentRate: 100,
                personnummer: "...",
            },
            roles: ["doctor", "overläkare"],  // Meteor-roles package
        }

        Eller i 'staff':
        {
            _id: "...",
            userId: "...",          // Referens till Meteor.users
            firstName: "Maria",
            lastName: "Eriksson",
            role: "ÖL",
            department: "Ortopedi",
            site: "CSK",
            employmentRate: 100,
            canPrimaryCall: false,
            canBackupCall: true,
            exemptFromCall: false,
            supervisorId: null,
        }
        """
        doctors = []

        # Försök 1: 'staff' collection (vanligare i vårdappar)
        staff_coll = self._coll("staff")
        query = {}
        if self.department_filter:
            query["department"] = self.department_filter

        try:
            cursor = staff_coll.find(query)
            async for doc in cursor:
                doctor = self._staff_doc_to_doctor(doc)
                if doctor:
                    doctors.append(doctor)
        except Exception:
            pass

        # Försök 2: Om staff var tom, prova users med profile-data
        if not doctors:
            users_coll = self._coll("users")
            query = {}
            if self.department_filter:
                query["profile.department"] = self.department_filter

            try:
                cursor = users_coll.find(query)
                async for doc in cursor:
                    doctor = self._user_doc_to_doctor(doc)
                    if doctor:
                        doctors.append(doctor)
            except Exception as e:
                print(f"⚠️ Kunde inte läsa users: {e}")

        return doctors

    def _staff_doc_to_doctor(self, doc: dict) -> Optional[Doctor]:
        """Konvertera Tessa staff-dokument till COP Doctor."""
        try:
            role = self.map_role(doc.get("role", "UL"))
            site = self.map_site(doc.get("site", "CSK"))

            return Doctor(
                id=str(doc["_id"]),
                name=f"Dr {doc.get('lastName', doc.get('name', 'Okänd'))}",
                role=role,
                employment_rate=doc.get("employmentRate", 100),
                can_primary_call=doc.get("canPrimaryCall", role in (Role.SPECIALIST, Role.ST_SEN)),
                can_backup_call=doc.get("canBackupCall", role in (Role.ÖVERLÄKARE, Role.SPECIALIST)),
                exempt_from_call=doc.get("exemptFromCall", False),
                supervisor_id=doc.get("supervisorId"),
                site_preference=site,
                required_procedures=doc.get("requiredProcedures", {}),
                completed_procedures=doc.get("completedProcedures", {}),
            )
        except Exception as e:
            print(f"⚠️ Kunde inte konvertera staff-doc {doc.get('_id')}: {e}")
            return None

    def _user_doc_to_doctor(self, doc: dict) -> Optional[Doctor]:
        """Konvertera Meteor users-dokument till COP Doctor."""
        try:
            profile = doc.get("profile", {})
            roles = doc.get("roles", [])

            # Mappa Meteor-roller till COP-roller
            role = Role.UNDERLÄKARE
            for r in roles:
                r_lower = r.lower()
                if "överlä" in r_lower or "overla" in r_lower:
                    role = Role.ÖVERLÄKARE
                elif "specialist" in r_lower and "st" not in r_lower:
                    role = Role.SPECIALIST
                elif "st" in r_lower and ("sen" in r_lower or "4" in r_lower or "5" in r_lower):
                    role = Role.ST_SEN
                elif "st" in r_lower:
                    role = Role.ST_TIDIG

            name = f"Dr {profile.get('lastName', doc.get('username', 'Okänd'))}"

            return Doctor(
                id=str(doc["_id"]),
                name=name,
                role=role,
                employment_rate=profile.get("employmentRate", 100),
                can_primary_call=role in (Role.SPECIALIST, Role.ST_SEN, Role.ST_TIDIG),
                can_backup_call=role in (Role.ÖVERLÄKARE, Role.SPECIALIST),
                exempt_from_call=profile.get("exemptFromCall", False),
                supervisor_id=profile.get("supervisorId"),
            )
        except Exception as e:
            print(f"⚠️ Kunde inte konvertera user-doc {doc.get('_id')}: {e}")
            return None

    # === PULL: Salar ===

    async def pull_rooms(self) -> list[OperatingRoom]:
        """Hämta operationssalar från Tessa."""
        rooms = []
        rooms_coll = self._coll("rooms")

        query = {}
        if self.department_filter:
            query["department"] = self.department_filter

        try:
            cursor = rooms_coll.find(query)
            async for doc in cursor:
                site = self.map_site(doc.get("site", doc.get("location", "CSK")))
                rooms.append(OperatingRoom(
                    id=str(doc["_id"]),
                    name=doc.get("name", f"Sal {doc['_id']}"),
                    site=site,
                    available_days=doc.get("availableDays", [0, 1, 2, 3, 4]),
                    specializations=doc.get("specializations", []),
                ))
        except Exception as e:
            print(f"⚠️ Kunde inte läsa rooms: {e}")

        return rooms

    # === PULL: Schema ===

    async def pull_schedule(self, start_date: date, end_date: date) -> list[ExternalScheduleEntry]:
        """
        Hämta befintligt schema från Tessa.

        Tessa lagrar sannolikt skift i 'shifts' collection:
        {
            _id: "...",
            userId: "...",
            date: ISODate("2026-03-30"),
            shiftType: "dag",         // dag, natt, jour, bakjour, etc.
            function: "operation",     // operation, avdelning, mottagning, etc.
            site: "hässleholm",
            startTime: "07:30",
            endTime: "16:00",
            status: "confirmed",
            scheduleId: "...",         // Koppling till schemaperiod
        }
        """
        entries = []
        shifts_coll = self._coll("shifts")

        try:
            cursor = shifts_coll.find({
                "date": {
                    "$gte": datetime.combine(start_date, datetime.min.time()),
                    "$lte": datetime.combine(end_date, datetime.max.time()),
                }
            })

            async for doc in cursor:
                doc_date = doc.get("date")
                if isinstance(doc_date, datetime):
                    doc_date = doc_date.date()

                func_code = self._map_tessa_function(
                    doc.get("shiftType", ""),
                    doc.get("function", ""),
                    doc.get("site", ""),
                )

                entries.append(ExternalScheduleEntry(
                    external_id=str(doc["_id"]),
                    doctor_external_id=str(doc.get("userId", doc.get("staffId", ""))),
                    date=doc_date,
                    function_code=func_code,
                    site_code=doc.get("site", ""),
                    start_time=doc.get("startTime", ""),
                    end_time=doc.get("endTime", ""),
                    status=doc.get("status", "confirmed"),
                    raw_data=doc,
                ))

        except Exception as e:
            print(f"⚠️ Kunde inte läsa shifts: {e}")

        return entries

    def _map_tessa_function(self, shift_type: str, function: str, site: str) -> str:
        """Mappa Tessas shift/function-kombination till COP-funktion."""
        st = shift_type.lower()
        fn = function.lower()
        s = site.lower()

        # Jour
        if "primärjour" in st or "primary" in st:
            return "JOUR_P"
        if "bakjour" in st or "backup" in st:
            return "JOUR_B"
        if "jour" in st:
            return "JOUR_P"  # Default

        # Funktion + plats → COP-funktionskod
        site_suffix = "H" if "häss" in s or "hässle" in s else "C"

        if "oper" in fn or "op" in fn:
            return f"OP_{site_suffix}"
        if "avd" in fn or "ward" in fn:
            return f"AVD_{site_suffix}"
        if "mott" in fn or "mottagn" in fn or "clinic" in fn:
            return f"MOTT_{site_suffix}"

        return "LEDIG"

    # === PULL: Frånvaro ===

    async def pull_absences(self, start_date: date, end_date: date) -> list[dict]:
        """Hämta frånvaro från Tessa."""
        absences = []
        abs_coll = self._coll("absences")

        try:
            cursor = abs_coll.find({
                "$or": [
                    {"startDate": {"$lte": datetime.combine(end_date, datetime.max.time()),
                                   "$gte": datetime.combine(start_date, datetime.min.time())}},
                    {"endDate": {"$gte": datetime.combine(start_date, datetime.min.time()),
                                 "$lte": datetime.combine(end_date, datetime.max.time())}},
                ]
            })

            async for doc in cursor:
                absences.append({
                    "external_id": str(doc["_id"]),
                    "doctor_id": str(doc.get("userId", doc.get("staffId", ""))),
                    "type": doc.get("type", doc.get("absenceType", "sjuk")),
                    "start_date": doc.get("startDate"),
                    "end_date": doc.get("endDate"),
                    "approved": doc.get("approved", True),
                    "notes": doc.get("notes", ""),
                })

        except Exception as e:
            print(f"⚠️ Kunde inte läsa absences: {e}")

        return absences

    # === PUSH: Schema → Tessa ===

    async def push_schedule(self, schedule: dict, start_date: date) -> SyncResult:
        """
        Skriva COP-schema tillbaka till Tessas MongoDB.

        Strategi:
          1. Skapa/uppdatera en scheduleperiod i 'schedules'
          2. Upsert varje dag/läkare-kombination i 'shifts'
          3. Behåll Tessa-specifika fält (createdBy, etc.)
        """
        result = SyncResult(
            success=True,
            direction=SyncDirection.PUSH,
            adapter_type=AdapterType.TESSA,
        )

        if self.config.dry_run:
            result.details["mode"] = "dry_run"
            result.items_synced = sum(len(days) for days in schedule.values())
            return result

        shifts_coll = self._coll("shifts")

        for doctor_id, days in schedule.items():
            for date_str, function_code in days.items():
                try:
                    shift_date = datetime.strptime(date_str, "%Y-%m-%d")
                    tessa_data = self._cop_to_tessa_shift(doctor_id, shift_date, function_code)

                    if tessa_data:
                        # Upsert: uppdatera om finns, skapa annars
                        await shifts_coll.update_one(
                            {
                                "userId": doctor_id,
                                "date": shift_date,
                            },
                            {"$set": tessa_data},
                            upsert=True,
                        )
                        result.items_synced += 1

                except Exception as e:
                    result.items_failed += 1
                    result.errors.append(f"{doctor_id}/{date_str}: {str(e)}")

        result.success = result.items_failed == 0
        return result

    def _cop_to_tessa_shift(self, doctor_id: str, shift_date: datetime, function_code: str) -> dict:
        """Konvertera COP-funktion tillbaka till Tessa-format."""
        if function_code == "LEDIG":
            return None  # Ta bort skiftet istället

        # Mappa COP → Tessa
        func_map = {
            "OP_H": ("dag", "operation", "hässleholm"),
            "OP_C": ("dag", "operation", "csk"),
            "AVD_H": ("dag", "avdelning", "hässleholm"),
            "AVD_C": ("dag", "avdelning", "csk"),
            "MOTT_H": ("dag", "mottagning", "hässleholm"),
            "MOTT_C": ("dag", "mottagning", "csk"),
            "JOUR_P": ("primärjour", "jour", "csk"),
            "JOUR_B": ("bakjour", "jour", "csk"),
        }

        shift_type, function, site = func_map.get(function_code, ("dag", "övrig", "csk"))

        return {
            "userId": doctor_id,
            "date": shift_date,
            "shiftType": shift_type,
            "function": function,
            "site": site,
            "status": "confirmed",
            "source": "cop-engine",  # Markerar att COP skrev denna
            "updatedAt": datetime.now(),
            "updatedBy": "cop-engine-v0.1",
        }

    # === PUSH: Frånvaro ===

    async def push_absence(self, doctor_id: str, absence_type: str,
                          start_date: date, end_date: date) -> SyncResult:
        """Registrera frånvaro i Tessa."""
        result = SyncResult(
            success=True,
            direction=SyncDirection.PUSH,
            adapter_type=AdapterType.TESSA,
        )

        abs_coll = self._coll("absences")

        try:
            await abs_coll.insert_one({
                "userId": doctor_id,
                "absenceType": absence_type,
                "startDate": datetime.combine(start_date, datetime.min.time()),
                "endDate": datetime.combine(end_date, datetime.max.time()),
                "approved": False,  # Kräver godkännande
                "source": "cop-engine",
                "createdAt": datetime.now(),
            })
            result.items_synced = 1
        except Exception as e:
            result.success = False
            result.errors.append(str(e))

        return result

    # === MAPPNINGAR ===

    def _default_role_mapping(self) -> dict:
        """Standard rollmappning Tessa → COP."""
        return {
            # Tessa-format → COP Role.value
            "ÖL": "ÖL",
            "överläkare": "ÖL",
            "Överläkare": "ÖL",
            "SP": "SP",
            "specialist": "SP",
            "Specialist": "SP",
            "specialistläkare": "SP",
            "ST": "ST_TIDIG",
            "ST-läkare": "ST_TIDIG",
            "ST tidig": "ST_TIDIG",
            "ST sen": "ST_SEN",
            "ST1": "ST_TIDIG",
            "ST2": "ST_TIDIG",
            "ST3": "ST_TIDIG",
            "ST4": "ST_SEN",
            "ST5": "ST_SEN",
            "UL": "UL",
            "underläkare": "UL",
            "Underläkare": "UL",
            "AT": "UL",
            "AT-läkare": "UL",
            "vikarie": "UL",
        }

    def _default_site_mapping(self) -> dict:
        """Standard platsmappning Tessa → COP."""
        return {
            "CSK": "CSK",
            "csk": "CSK",
            "Kristianstad": "CSK",
            "kristianstad": "CSK",
            "centralsjukhuset": "CSK",
            "Hässleholm": "Hässleholm",
            "hässleholm": "Hässleholm",
            "hassleholm": "Hässleholm",
            "HÄSS": "Hässleholm",
        }

    def _default_function_mapping(self) -> dict:
        """Standard funktionsmappning Tessa → COP."""
        return {
            "operation": "OPERATION",
            "op": "OPERATION",
            "avdelning": "AVDELNING",
            "avd": "AVDELNING",
            "mottagning": "MOTTAGNING",
            "mott": "MOTTAGNING",
            "jour": "PRIMÄRJOUR",
            "primärjour": "PRIMÄRJOUR",
            "bakjour": "BAKJOUR",
            "ledig": "LEDIG",
            "semester": "SEMESTER",
            "sjuk": "LEDIG",
        }
