"""
COP Adapter Base — Abstract interface för alla källsystem
==========================================================
Varje schemaläggningssystem (Tessa, Time Care, Heroma, Medvind)
implementerar denna abstraktion. COP-motorn pratar BARA med adaptern,
aldrig direkt med källsystemet.

Arkitektur:
  [Tessa MongoDB] → [TessaAdapter] → [COP Standard Data Model] → [COP Solver]
  [Time Care API] → [TimeCareAdapter] → [COP Standard Data Model] → [COP Solver]
  [Excel/CSV]     → [CSVAdapter]     → [COP Standard Data Model] → [COP Solver]

Varje adapter måste implementera:
  - pull_doctors()     → Hämta läkarlista
  - pull_rooms()       → Hämta operationssalar
  - pull_schedule()    → Hämta befintligt schema
  - pull_absences()    → Hämta registrerad frånvaro
  - push_schedule()    → Skriva tillbaka optimerat schema
  - push_absence()     → Registrera frånvaro i källsystemet
  - test_connection()  → Testa anslutning
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional

from data_model import (
    ClinicConfig, Doctor, OperatingRoom, Role,
    StaffingRequirement, CallStructure, ATLRules, Preference
)


class AdapterType(Enum):
    """Stödda källsystem."""
    TESSA = "tessa"
    TIME_CARE = "time_care"
    HEROMA = "heroma"
    MEDVIND = "medvind"
    CSV = "csv"
    MANUAL = "manual"


class SyncDirection(Enum):
    """Synkroniseringsriktning."""
    PULL = "pull"       # Källsystem → COP
    PUSH = "push"       # COP → Källsystem
    BIDIRECTIONAL = "bidirectional"


@dataclass
class SyncResult:
    """Resultat av en synkroniseringsoperation."""
    success: bool
    direction: SyncDirection
    adapter_type: AdapterType
    timestamp: datetime = field(default_factory=datetime.now)
    items_synced: int = 0
    items_failed: int = 0
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    details: dict = field(default_factory=dict)


@dataclass
class AdapterConfig:
    """Konfiguration för en adapter."""
    adapter_type: AdapterType
    clinic_id: str = ""

    # Anslutningsuppgifter (varierar per adapter)
    host: str = ""
    port: int = 0
    database: str = ""
    username: str = ""
    password: str = ""
    api_key: str = ""
    api_url: str = ""

    # Filsökvägar (för CSV-adapter)
    doctors_file: str = ""
    schedule_file: str = ""
    rooms_file: str = ""

    # Mappningar (källsystem-ID → COP-ID)
    role_mapping: dict = field(default_factory=dict)
    site_mapping: dict = field(default_factory=dict)
    function_mapping: dict = field(default_factory=dict)

    # Sync-inställningar
    sync_direction: SyncDirection = SyncDirection.BIDIRECTIONAL
    auto_sync_interval_minutes: int = 0  # 0 = manuell
    dry_run: bool = False  # True = visa ändringar utan att skriva


@dataclass
class ExternalScheduleEntry:
    """En schemapost från källsystemet (innan konvertering till COP-format)."""
    external_id: str           # ID i källsystemet
    doctor_external_id: str    # Läkar-ID i källsystemet
    date: date
    function_code: str         # Funktionskod i källsystemets format
    site_code: str = ""
    start_time: str = ""       # HH:MM
    end_time: str = ""         # HH:MM
    status: str = "confirmed"  # confirmed, tentative, cancelled
    notes: str = ""
    raw_data: dict = field(default_factory=dict)  # Rå data från källsystemet


class BaseAdapter(ABC):
    """
    Abstrakt bas för alla COP-adaptrar.

    Varje källsystem (Tessa, Time Care, etc.) implementerar denna klass.
    COP-motorn anropar adapterns metoder utan att veta vilket system
    som finns bakom.
    """

    def __init__(self, config: AdapterConfig):
        self.config = config
        self._connected = False

    @property
    def adapter_type(self) -> AdapterType:
        return self.config.adapter_type

    @property
    def is_connected(self) -> bool:
        return self._connected

    # === ANSLUTNING ===

    @abstractmethod
    async def connect(self) -> bool:
        """Upprätta anslutning till källsystemet."""
        pass

    @abstractmethod
    async def disconnect(self):
        """Stäng anslutning."""
        pass

    @abstractmethod
    async def test_connection(self) -> dict:
        """
        Testa anslutning och returnera status.

        Returns:
            dict med:
                connected: bool
                system_name: str
                system_version: str
                database_size: int
                last_sync: datetime | None
        """
        pass

    # === PULL (Källsystem → COP) ===

    @abstractmethod
    async def pull_doctors(self) -> list[Doctor]:
        """
        Hämta alla läkare från källsystemet.

        Konverterar från källsystemets format till COP Doctor-objekt.
        Mappar roller, anställningsgrad, jourregler etc.
        """
        pass

    @abstractmethod
    async def pull_rooms(self) -> list[OperatingRoom]:
        """Hämta operationssalar."""
        pass

    @abstractmethod
    async def pull_schedule(self, start_date: date, end_date: date) -> list[ExternalScheduleEntry]:
        """
        Hämta befintligt schema för en period.

        Args:
            start_date: Startdatum
            end_date: Slutdatum

        Returns:
            Lista av schemaposterna i externt format
        """
        pass

    @abstractmethod
    async def pull_absences(self, start_date: date, end_date: date) -> list[dict]:
        """
        Hämta registrerad frånvaro.

        Returns:
            Lista av {doctor_id, type, start_date, end_date, ...}
        """
        pass

    async def pull_config(self) -> ClinicConfig:
        """
        Hämta komplett klinikkonfiguration.

        Standardimplementering som sammanställer från pull_doctors() och pull_rooms().
        Kan överridas av specifika adaptrar.
        """
        doctors = await self.pull_doctors()
        rooms = await self.pull_rooms()

        return ClinicConfig(
            name=f"Klinik {self.config.clinic_id}",
            sites=[],
            doctors=doctors,
            operating_rooms=rooms,
            staffing_requirements=[],
            call_structure=CallStructure(
                primary_roles=[Role.SPECIALIST, Role.ST_SEN],
                backup_roles=[Role.ÖVERLÄKARE, Role.SPECIALIST],
            ),
            atl_rules=ATLRules(),
            preferences=[],
        )

    # === PUSH (COP → Källsystem) ===

    @abstractmethod
    async def push_schedule(self, schedule: dict, start_date: date) -> SyncResult:
        """
        Skriva tillbaka COP-genererat schema till källsystemet.

        Args:
            schedule: COP-schema {doctor_id: {date_str: function_id}}
            start_date: Schemats startdatum

        Returns:
            SyncResult med antal synkade poster
        """
        pass

    @abstractmethod
    async def push_absence(self, doctor_id: str, absence_type: str,
                          start_date: date, end_date: date) -> SyncResult:
        """Registrera frånvaro i källsystemet."""
        pass

    # === MAPPNING ===

    def map_role(self, external_role: str) -> Role:
        """Mappa källsystemets rollkod till COP Role."""
        mapping = self.config.role_mapping or self._default_role_mapping()
        role_str = mapping.get(external_role, external_role)
        try:
            return Role(role_str)
        except ValueError:
            return Role.UNDERLÄKARE  # Fallback

    def map_site(self, external_site: str) -> str:
        """Mappa källsystemets platskod till COP site-sträng."""
        mapping = self.config.site_mapping or self._default_site_mapping()
        return mapping.get(external_site, external_site)

    def map_function(self, external_func: str) -> str:
        """Mappa källsystemets funktionskod till COP-funktion."""
        mapping = self.config.function_mapping or self._default_function_mapping()
        return mapping.get(external_func, external_func)

    def _default_role_mapping(self) -> dict:
        return {}

    def _default_site_mapping(self) -> dict:
        return {}

    def _default_function_mapping(self) -> dict:
        return {}

    # === FULL SYNC ===

    async def full_pull(self, start_date: date, end_date: date) -> tuple[ClinicConfig, list[ExternalScheduleEntry]]:
        """
        Gör en fullständig pull: config + schema + frånvaro.

        Returns:
            (ClinicConfig, list[ExternalScheduleEntry])
        """
        config = await self.pull_config()
        schedule_entries = await self.pull_schedule(start_date, end_date)
        absences = await self.pull_absences(start_date, end_date)

        return config, schedule_entries
