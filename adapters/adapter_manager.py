"""
COP Adapter Manager — Orchestrerar alla systemkopplingar
==========================================================
Central komponent som hanterar vilken adapter som används,
synkroniserar data, och loggar alla operationer.

Flöde:
  1. AdapterManager skapas med AdapterConfig
  2. Manager instantierar rätt adapter (Tessa/CSV/etc)
  3. Manager exponerar unified interface till resten av COP
  4. Alla sync-operationer loggas för audit

Används av:
  - api.py (REST API endpoints)
  - agent.py (LLM-agenten)
  - CLI-verktyg
"""

import asyncio
import os
import sys
from datetime import date, datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_model import ClinicConfig, Doctor, OperatingRoom
from adapters.base import (
    BaseAdapter, AdapterConfig, AdapterType, SyncDirection, SyncResult
)
from adapters.tessa_adapter import TessaAdapter
from adapters.csv_adapter import CSVAdapter


# Registry: adapter-typ → klass
ADAPTER_REGISTRY = {
    AdapterType.TESSA: TessaAdapter,
    AdapterType.CSV: CSVAdapter,
    # Framtida:
    # AdapterType.TIME_CARE: TimeCareAdapter,
    # AdapterType.HEROMA: HeromaAdapter,
    # AdapterType.MEDVIND: MedvindAdapter,
}


class AdapterManager:
    """
    Hanterar alla adapterkopplingar för en COP-installation.

    En klinik kan ha:
      - En primär adapter (t.ex. Tessa)
      - Fallback-adapter (CSV) om primär saknas
    """

    def __init__(self, config: AdapterConfig):
        self.config = config
        self.adapter: Optional[BaseAdapter] = None
        self.sync_log: list[SyncResult] = []

    async def initialize(self) -> bool:
        """Skapa och anslut adapter."""
        adapter_class = ADAPTER_REGISTRY.get(self.config.adapter_type)
        if not adapter_class:
            print(f"❌ Okänd adaptertyp: {self.config.adapter_type}")
            return False

        self.adapter = adapter_class(self.config)
        connected = await self.adapter.connect()

        if connected:
            print(f"✅ {self.config.adapter_type.value}-adapter ansluten")
        else:
            print(f"⚠️ Kunde inte ansluta {self.config.adapter_type.value}-adapter")

        return connected

    async def test(self) -> dict:
        """Testa adaptern."""
        if not self.adapter:
            return {"error": "Ingen adapter initialiserad"}
        return await self.adapter.test_connection()

    # === PULL ===

    async def pull_config(self) -> Optional[ClinicConfig]:
        """Hämta klinikkonfiguration från källsystemet."""
        if not self.adapter:
            return None
        return await self.adapter.pull_config()

    async def pull_schedule(self, start_date: date, end_date: date) -> list:
        """Hämta befintligt schema."""
        if not self.adapter:
            return []
        entries = await self.adapter.pull_schedule(start_date, end_date)
        self._log_sync(SyncResult(
            success=True,
            direction=SyncDirection.PULL,
            adapter_type=self.config.adapter_type,
            items_synced=len(entries),
            details={"start": str(start_date), "end": str(end_date)},
        ))
        return entries

    # === PUSH ===

    async def push_schedule(self, schedule: dict, start_date: date) -> SyncResult:
        """Skicka schema till källsystemet."""
        if not self.adapter:
            return SyncResult(success=False, direction=SyncDirection.PUSH,
                            adapter_type=self.config.adapter_type,
                            errors=["Ingen adapter ansluten"])

        result = await self.adapter.push_schedule(schedule, start_date)
        self._log_sync(result)
        return result

    async def push_absence(self, doctor_id: str, absence_type: str,
                          start_date: date, end_date: date) -> SyncResult:
        """Registrera frånvaro i källsystemet."""
        if not self.adapter:
            return SyncResult(success=False, direction=SyncDirection.PUSH,
                            adapter_type=self.config.adapter_type,
                            errors=["Ingen adapter ansluten"])

        result = await self.adapter.push_absence(doctor_id, absence_type, start_date, end_date)
        self._log_sync(result)
        return result

    # === FULL SYNC ===

    async def full_sync(self, start_date: date, end_date: date) -> dict:
        """
        Fullständig synk: pull allt, returnera COP-format.

        Returns:
            {
                "config": ClinicConfig,
                "schedule_entries": list,
                "absences": list,
                "sync_stats": dict,
            }
        """
        if not self.adapter:
            return {"error": "Ingen adapter"}

        config = await self.adapter.pull_config()
        entries = await self.adapter.pull_schedule(start_date, end_date)
        absences = await self.adapter.pull_absences(start_date, end_date)

        return {
            "config": config,
            "schedule_entries": entries,
            "absences": absences,
            "sync_stats": {
                "doctors": len(config.doctors) if config else 0,
                "rooms": len(config.operating_rooms) if config else 0,
                "schedule_entries": len(entries),
                "absences": len(absences),
                "adapter": self.config.adapter_type.value,
                "timestamp": datetime.now().isoformat(),
            },
        }

    # === AUDIT LOG ===

    def _log_sync(self, result: SyncResult):
        """Logga sync-operation."""
        self.sync_log.append(result)
        # Behåll max 1000 poster i minne
        if len(self.sync_log) > 1000:
            self.sync_log = self.sync_log[-500:]

    def get_sync_history(self, limit: int = 20) -> list[dict]:
        """Hämta sync-historik."""
        return [
            {
                "timestamp": r.timestamp.isoformat(),
                "direction": r.direction.value,
                "adapter": r.adapter_type.value,
                "success": r.success,
                "items_synced": r.items_synced,
                "items_failed": r.items_failed,
                "errors": r.errors[:3],
            }
            for r in reversed(self.sync_log[-limit:])
        ]

    async def close(self):
        """Stäng adapter."""
        if self.adapter:
            await self.adapter.disconnect()


# === FACTORY ===

def create_tessa_manager(
    host: str = "localhost",
    port: int = 3001,  # Meteor-default MongoDB port
    database: str = "meteor",
    department: str = "Ortopedi",
) -> AdapterManager:
    """Skapa en Tessa-adapter snabbt."""
    config = AdapterConfig(
        adapter_type=AdapterType.TESSA,
        clinic_id="demo",
        host=host,
        port=port,
        database=database,
        role_mapping={"_department": department},
    )
    return AdapterManager(config)


def create_csv_manager(
    doctors_file: str = "",
    schedule_file: str = "",
    rooms_file: str = "",
) -> AdapterManager:
    """Skapa en CSV-adapter snabbt."""
    config = AdapterConfig(
        adapter_type=AdapterType.CSV,
        clinic_id="demo",
        doctors_file=doctors_file,
        schedule_file=schedule_file,
        rooms_file=rooms_file,
    )
    return AdapterManager(config)


# === TEST ===

async def demo_csv_adapter():
    """Demo: CSV-adapter end-to-end."""
    import tempfile, csv

    # Skapa test-CSV:er
    tmpdir = tempfile.mkdtemp()
    doctors_path = os.path.join(tmpdir, "doctors.csv")
    rooms_path = os.path.join(tmpdir, "rooms.csv")
    schedule_path = os.path.join(tmpdir, "schedule.csv")

    # Doctors CSV
    with open(doctors_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "name", "role", "employment_rate", "can_primary_call", "can_backup_call", "exempt_from_call", "supervisor_id"])
        w.writerow(["D1", "Dr Testsson", "ÖL", "100", "false", "true", "false", ""])
        w.writerow(["D2", "Dr Provare", "SP", "100", "true", "true", "false", ""])
        w.writerow(["D3", "Dr Student", "ST_TIDIG", "100", "true", "false", "false", "D2"])

    # Rooms CSV
    with open(rooms_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "name", "site", "available_days"])
        w.writerow(["R1", "Sal 1", "Sjukhus_A", "0,1,2,3,4"])
        w.writerow(["R2", "Akutsal", "Sjukhus_B", "0,1,2,3,4"])

    # Test!
    manager = create_csv_manager(
        doctors_file=doctors_path,
        schedule_file=schedule_path,
        rooms_file=rooms_path,
    )

    await manager.initialize()

    # Test connection
    status = await manager.test()
    print(f"\n📋 Adapter-status:")
    for k, v in status.items():
        print(f"  {k}: {v}")

    # Pull config
    config = await manager.pull_config()
    print(f"\n👥 Läkare: {len(config.doctors)}")
    for d in config.doctors:
        print(f"  {d.id}: {d.name} ({d.role.value})")

    print(f"\n🏥 Salar: {len(config.operating_rooms)}")
    for r in config.operating_rooms:
        print(f"  {r.id}: {r.name} ({r.site})")

    # Push test-schema
    test_schedule = {
        "D1": {"2026-03-30": "MOTT_C", "2026-03-31": "JOUR_B"},
        "D2": {"2026-03-30": "OP_H", "2026-03-31": "JOUR_P"},
        "D3": {"2026-03-30": "OP_H", "2026-03-31": "AVD_C"},
    }

    result = await manager.push_schedule(test_schedule, date(2026, 3, 30))
    print(f"\n📤 Push-resultat: {'✅' if result.success else '❌'}")
    print(f"  Synkade: {result.items_synced}")
    print(f"  Output: {result.details.get('output_file', '?')}")

    # Verifiera
    if os.path.exists(schedule_path):
        with open(schedule_path, "r", encoding="utf-8-sig") as f:
            print(f"\n📄 Genererad CSV:")
            print(f.read())

    # Historik
    history = manager.get_sync_history()
    print(f"\n📊 Sync-historik: {len(history)} poster")

    await manager.close()
    print("\n✅ CSV-adapter demo klar!")


if __name__ == "__main__":
    asyncio.run(demo_csv_adapter())
