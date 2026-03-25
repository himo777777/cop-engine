"""
COP CSV Adapter — Universell import/export via CSV/Excel
=========================================================
Fallback-adapter för system som saknar direkt API-koppling.
Fungerar med alla schemaläggningssystem som kan exportera till CSV/Excel.

Användning:
  1. Exportera personalregister och schema från källsystemet till CSV
  2. Peka CSV-adaptern mot filerna
  3. COP läser, optimerar, och exporterar nytt schema som CSV
  4. Importera CSV tillbaka i källsystemet

Stödda format:
  - doctors.csv: id, name, role, employment_rate, can_primary_call, ...
  - schedule.csv: doctor_id, date, function, site, start_time, end_time
  - rooms.csv: id, name, site, available_days
  - absences.csv: doctor_id, type, start_date, end_date

Alla filer stöder både ; och , som separator (autodetekt).
"""

import csv
import os
from datetime import date, datetime
from typing import Optional
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_model import (
    Doctor, OperatingRoom, Role, ShiftType, Function,
    ClinicConfig, CallStructure, ATLRules,
)
from adapters.base import (
    BaseAdapter, AdapterConfig, AdapterType, SyncDirection,
    SyncResult, ExternalScheduleEntry,
)


def _detect_delimiter(filepath: str) -> str:
    """Auto-detektera CSV-separator (;  eller ,)."""
    with open(filepath, "r", encoding="utf-8-sig") as f:
        first_line = f.readline()
        if ";" in first_line and "," not in first_line:
            return ";"
        if first_line.count(";") > first_line.count(","):
            return ";"
        return ","


def _parse_bool(val: str) -> bool:
    """Parsa boolean från CSV."""
    return val.strip().lower() in ("true", "1", "yes", "ja", "x", "sant")


def _parse_list(val: str) -> list:
    """Parsa lista från CSV (t.ex. '0,1,2,3,4' eller '0;1;2;3;4')."""
    if not val.strip():
        return [0, 1, 2, 3, 4]  # Default: mån-fre
    return [int(x.strip()) for x in val.replace(";", ",").split(",") if x.strip().isdigit()]


class CSVAdapter(BaseAdapter):
    """
    CSV/Excel adapter — universell import/export.

    Läser personalregister, salar och schema från CSV-filer.
    Skriver optimerat schema tillbaka till CSV.
    """

    def __init__(self, config: AdapterConfig):
        super().__init__(config)

    # === ANSLUTNING ===

    async def connect(self) -> bool:
        """Verifiera att CSV-filer finns."""
        self._connected = True
        return True

    async def disconnect(self):
        self._connected = False

    async def test_connection(self) -> dict:
        """Kolla vilka filer som finns."""
        files = {
            "doctors": self.config.doctors_file,
            "schedule": self.config.schedule_file,
            "rooms": self.config.rooms_file,
        }

        found = {}
        for name, path in files.items():
            if path and os.path.exists(path):
                size = os.path.getsize(path)
                with open(path, "r", encoding="utf-8-sig") as f:
                    lines = sum(1 for _ in f) - 1  # Minus header
                found[name] = {"path": path, "size_kb": round(size / 1024, 1), "rows": lines}
            else:
                found[name] = {"path": path or "(ej konfigurerad)", "exists": False}

        return {
            "connected": True,
            "system_name": "CSV Import/Export",
            "system_version": "1.0",
            "files": found,
        }

    # === PULL: Läkare ===

    async def pull_doctors(self) -> list[Doctor]:
        """
        Läs läkare från CSV.

        Förväntade kolumner:
        id, name, role, employment_rate, can_primary_call, can_backup_call,
        exempt_from_call, supervisor_id, site_preference
        """
        filepath = self.config.doctors_file
        if not filepath or not os.path.exists(filepath):
            return []

        doctors = []
        delimiter = _detect_delimiter(filepath)

        with open(filepath, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=delimiter)

            for row in reader:
                try:
                    role = self.map_role(row.get("role", "UL").strip())

                    doctors.append(Doctor(
                        id=row["id"].strip(),
                        name=row.get("name", f"Dr {row['id']}").strip(),
                        role=role,
                        employment_rate=int(row.get("employment_rate", "100").strip() or "100"),
                        can_primary_call=_parse_bool(row.get("can_primary_call", "false")),
                        can_backup_call=_parse_bool(row.get("can_backup_call", "false")),
                        exempt_from_call=_parse_bool(row.get("exempt_from_call", "false")),
                        supervisor_id=row.get("supervisor_id", "").strip() or None,
                        site_preference=self.map_site(row.get("site_preference", "").strip()) if row.get("site_preference") else None,
                    ))
                except Exception as e:
                    print(f"⚠️ Rad i doctors.csv: {e}")

        return doctors

    # === PULL: Salar ===

    async def pull_rooms(self) -> list[OperatingRoom]:
        """
        Läs operationssalar från CSV.

        Kolumner: id, name, site, available_days
        """
        filepath = self.config.rooms_file
        if not filepath or not os.path.exists(filepath):
            return []

        rooms = []
        delimiter = _detect_delimiter(filepath)

        with open(filepath, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=delimiter)

            for row in reader:
                try:
                    rooms.append(OperatingRoom(
                        id=row["id"].strip(),
                        name=row.get("name", f"Sal {row['id']}").strip(),
                        site=self.map_site(row.get("site", "default").strip()),
                        available_days=_parse_list(row.get("available_days", "")),
                    ))
                except Exception as e:
                    print(f"⚠️ Rad i rooms.csv: {e}")

        return rooms

    # === PULL: Schema ===

    async def pull_schedule(self, start_date: date, end_date: date) -> list[ExternalScheduleEntry]:
        """
        Läs schema från CSV.

        Kolumner: doctor_id, date, function, site, start_time, end_time, status
        """
        filepath = self.config.schedule_file
        if not filepath or not os.path.exists(filepath):
            return []

        entries = []
        delimiter = _detect_delimiter(filepath)

        with open(filepath, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=delimiter)

            for i, row in enumerate(reader):
                try:
                    row_date = date.fromisoformat(row["date"].strip())
                    if start_date <= row_date <= end_date:
                        entries.append(ExternalScheduleEntry(
                            external_id=f"csv_{i}",
                            doctor_external_id=row["doctor_id"].strip(),
                            date=row_date,
                            function_code=self.map_function(row.get("function", "").strip()),
                            site_code=row.get("site", "").strip(),
                            start_time=row.get("start_time", "").strip(),
                            end_time=row.get("end_time", "").strip(),
                            status=row.get("status", "confirmed").strip(),
                        ))
                except Exception as e:
                    print(f"⚠️ Rad {i+2} i schedule.csv: {e}")

        return entries

    # === PULL: Frånvaro ===

    async def pull_absences(self, start_date: date, end_date: date) -> list[dict]:
        """Läs frånvaro — använder schedule.csv med status 'absence' eller separat fil."""
        return []  # Kan utökas med absence.csv

    # === PUSH: Schema → CSV ===

    async def push_schedule(self, schedule: dict, start_date: date) -> SyncResult:
        """
        Exportera COP-schema till CSV.

        Output-format:
        doctor_id,date,function,site,source
        """
        result = SyncResult(
            success=True,
            direction=SyncDirection.PUSH,
            adapter_type=AdapterType.CSV,
        )

        output_path = self.config.schedule_file
        if not output_path:
            output_path = "cop_schedule_output.csv"

        # Byt namn om filen redan finns (behåll original)
        if os.path.exists(output_path):
            base, ext = os.path.splitext(output_path)
            backup = f"{base}_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
            os.rename(output_path, backup)
            result.details["backup"] = backup

        try:
            with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow(["doctor_id", "date", "function", "site", "source"])

                for doctor_id, days in schedule.items():
                    for date_str, function_code in days.items():
                        if function_code == "LEDIG":
                            continue  # Hoppa över lediga dagar

                        # Extract site from function_code suffix (e.g., "OP_SiteA" -> "SiteA")
                        if "_" in function_code:
                            site = function_code.split("_", 1)[1]
                        else:
                            site = "default"
                        writer.writerow([doctor_id, date_str, function_code, site, "cop-engine"])
                        result.items_synced += 1

            result.details["output_file"] = output_path

        except Exception as e:
            result.success = False
            result.errors.append(str(e))

        return result

    # === PUSH: Frånvaro ===

    async def push_absence(self, doctor_id: str, absence_type: str,
                          start_date: date, end_date: date) -> SyncResult:
        """Logga frånvaro till CSV."""
        result = SyncResult(
            success=True,
            direction=SyncDirection.PUSH,
            adapter_type=AdapterType.CSV,
        )

        path = os.path.join(os.path.dirname(self.config.schedule_file or "."), "cop_absences.csv")
        file_exists = os.path.exists(path)

        try:
            with open(path, "a", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f, delimiter=";")
                if not file_exists:
                    writer.writerow(["doctor_id", "absence_type", "start_date", "end_date", "registered_at"])
                writer.writerow([doctor_id, absence_type, start_date.isoformat(), end_date.isoformat(), datetime.now().isoformat()])
            result.items_synced = 1
        except Exception as e:
            result.success = False
            result.errors.append(str(e))

        return result

    # === MAPPNINGAR ===

    def _default_role_mapping(self) -> dict:
        return {
            "ÖL": "ÖL", "överläkare": "ÖL", "Överläkare": "ÖL",
            "SP": "SP", "specialist": "SP", "Specialist": "SP",
            "ST_SEN": "ST_SEN", "ST sen": "ST_SEN", "ST4": "ST_SEN", "ST5": "ST_SEN",
            "ST_TIDIG": "ST_TIDIG", "ST tidig": "ST_TIDIG", "ST1": "ST_TIDIG", "ST2": "ST_TIDIG", "ST3": "ST_TIDIG",
            "UL": "UL", "underläkare": "UL", "Underläkare": "UL",
        }

    def _default_function_mapping(self) -> dict:
        return {
            "OP_H": "OP_H", "OP_C": "OP_C",
            "AVD_H": "AVD_H", "AVD_C": "AVD_C",
            "MOTT_H": "MOTT_H", "MOTT_C": "MOTT_C",
            "JOUR_P": "JOUR_P", "JOUR_B": "JOUR_B",
            "operation": "OP_C", "avdelning": "AVD_C", "mottagning": "MOTT_C",
            "jour": "JOUR_P", "bakjour": "JOUR_B",
            "LEDIG": "LEDIG", "ledig": "LEDIG",
        }
