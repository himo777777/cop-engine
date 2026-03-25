"""
COP Standard Data Model v0.2
=============================
Universellt dataformat för schemaoptimering.
Systemagnostiskt — fungerar med Tessa, Time Care, Heroma, Medvind eller CSV.
Generisk modell — fungerar för alla sjukhus/kliniker.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Role(Enum):
    """Läkarroller i hierarkin."""
    UNDERLÄKARE = "UL"
    ST_TIDIG = "ST_TIDIG"      # ST år 1-3
    ST_SEN = "ST_SEN"          # ST år 4-5
    SPECIALIST = "SP"
    ÖVERLÄKARE = "ÖL"


class ShiftType(Enum):
    """Skifttyper."""
    DAG = "DAG"                # 07:00-16:30
    KVÄLLSJOUR = "JOUR_KVÄLL"  # 16:30-22:00
    NATTJOUR = "JOUR_NATT"     # 22:00-07:00
    HELGDAG = "JOUR_HELGDAG"   # 07:00-22:00 helg
    HELGNATT = "JOUR_HELGNATT" # 22:00-07:00 helg


class Function(Enum):
    """Kliniska funktioner/stationer."""
    OPERATION = "OP"
    AVDELNING = "AVD"
    MOTTAGNING = "MOTT"
    AKUTMOTTAGNING = "AKUT"
    PRIMÄRJOUR = "JOUR_PRIMÄR"
    BAKJOUR = "JOUR_BAK"
    ADMIN = "ADMIN"            # MDT, rond, utbildning
    LEDIG = "LEDIG"
    SEMESTER = "SEMESTER"


# Site is now a plain string, not an enum.
# Examples: "CSK", "Hässleholm", "Karolinska Solna", "Huddinge"


@dataclass
class Doctor:
    """En läkare med alla relevanta attribut."""
    id: str
    name: str
    role: Role
    site_preference: Optional[str] = None   # None = roterar mellan alla sites
    employment_rate: float = 1.0            # 1.0 = heltid, 0.8 = 80%
    can_primary_call: bool = False
    can_backup_call: bool = False
    exempt_from_call: bool = False          # Undantagen från jour
    supervisor_id: Optional[str] = None     # Handledare (för ST)
    competencies: list[str] = field(default_factory=list)

    # ST-specifika fält
    required_procedures: dict[str, int] = field(default_factory=dict)
    completed_procedures: dict[str, int] = field(default_factory=dict)


@dataclass
class OperatingRoom:
    """En operationssal."""
    id: str
    site: str
    name: str
    available_days: list[int] = field(default_factory=lambda: [0,1,2,3,4])  # 0=mån, 4=fre
    requires_senior: bool = True
    requires_assistant: bool = True


@dataclass
class StaffingRequirement:
    """Minimibemanningstal per funktion, skift och plats."""
    function: Function
    shift_type: ShiftType
    site: str
    min_count: int
    required_roles: list[Role]
    min_senior: int = 0


@dataclass
class CallStructure:
    """Jourlinjestruktur."""
    primary_roles: list[Role]
    backup_roles: list[Role]
    max_calls_per_month: int = 4
    max_consecutive_nights: int = 1
    max_weekend_frequency: int = 4
    rest_after_night: bool = True
    backup_is_on_site: bool = False


@dataclass
class ATLRules:
    """Arbetstidslagens regler — hårda constraints."""
    min_daily_rest_hours: float = 11.0
    min_daily_rest_exception: float = 9.0
    min_weekly_rest_hours: float = 36.0
    max_weekly_hours: float = 48.0
    max_consecutive_work_hours: float = 13.0
    max_work_plus_call_hours: float = 20.0
    max_overtime_per_year: float = 200.0
    max_break_interval_hours: float = 5.0


@dataclass
class Preference:
    """Önskemål från en läkare."""
    doctor_id: str
    type: str
    priority: int
    details: dict = field(default_factory=dict)


@dataclass
class ShiftDefinition:
    """Klinikdefinierad passtyp."""
    id: str
    name: str
    function: Function
    site: str
    start_time: str = "07:00"
    end_time: str = "16:30"
    duration_hours: float = 9.5
    min_staff: int = 1
    max_staff: int = 10
    required_roles: list[Role] = field(default_factory=list)
    required_competencies: list[str] = field(default_factory=list)
    is_on_call: bool = False


@dataclass
class ConstraintRule:
    """Konfigurerbar regel/constraint."""
    id: str
    name: str
    category: str          # "atl", "staffing", "fairness", "preference"
    is_hard: bool = False  # True = aldrig bryts
    weight: int = 5        # 1-10 (bara relevant för mjuka)
    enabled: bool = True
    parameters: dict = field(default_factory=dict)


def default_constraint_rules() -> list[ConstraintRule]:
    """Standardregler för nya kliniker."""
    return [
        ConstraintRule("atl_daily_rest", "11h dygnsvila efter jour", "atl",
                       is_hard=True, weight=10, parameters={"min_hours": 11}),
        ConstraintRule("atl_weekly_rest", "36h sammanhängande veckovila", "atl",
                       is_hard=False, weight=8, parameters={"min_hours": 36}),
        ConstraintRule("atl_max_weekly_hours", "Max 48h arbete per vecka", "atl",
                       is_hard=False, weight=7, parameters={"max_hours": 48}),
        ConstraintRule("call_max_per_week", "Max 1 jour per vecka", "staffing",
                       is_hard=True, weight=10, parameters={"max_calls": 1}),
        ConstraintRule("call_weekend_frequency", "Max var 4:e helg med jour", "fairness",
                       is_hard=False, weight=5, parameters={"min_interval_weeks": 4}),
        ConstraintRule("staffing_senior_presence", "Minst 1 ÖL aktiv per vardag", "staffing",
                       is_hard=True, weight=10, parameters={"min_count": 1}),
        ConstraintRule("training_st_supervisor", "ST med handledare på OP", "preference",
                       is_hard=False, weight=6, parameters={}),
        ConstraintRule("call_fairness", "Rättvis jourfördelning", "fairness",
                       is_hard=False, weight=5, parameters={}),
        ConstraintRule("preference_site", "Respektera site-preferens", "preference",
                       is_hard=False, weight=3, parameters={}),
        ConstraintRule("max_workdays", "Max 5 arbetsdagar per vecka", "atl",
                       is_hard=True, weight=10, parameters={"max_days": 5}),
    ]


@dataclass
class ClinicConfig:
    """Komplett klinikkonfiguration — allt solvern behöver."""
    name: str
    sites: list[str]
    doctors: list[Doctor]
    operating_rooms: list[OperatingRoom]
    staffing_requirements: list[StaffingRequirement]
    call_structure: CallStructure
    atl_rules: ATLRules
    preferences: list[Preference]
    shift_definitions: list[ShiftDefinition] = field(default_factory=list)
    constraint_rules: list[ConstraintRule] = field(default_factory=default_constraint_rules)
    schedule_cycle_weeks: int = 10
    travel_time_between_sites_min: int = 0


def create_kristianstad_example() -> ClinicConfig:
    """
    Exempeldata: Ortopedkliniken Kristianstad-Hässleholm.
    Returnerar en komplett konfiguration med 25 läkare, 7 salar, 2 sites.
    """
    SITE_CSK = "CSK"
    SITE_H = "Hässleholm"

    doctors = [
        # Överläkare (5 st)
        Doctor(id="OL1", name="Dr Andersson", role=Role.ÖVERLÄKARE,
               can_backup_call=True, competencies=["höftprotes", "knäprotes", "revision", "trauma"]),
        Doctor(id="OL2", name="Dr Bergström", role=Role.ÖVERLÄKARE,
               can_backup_call=True, competencies=["höftprotes", "knäprotes", "fotkirurgi"]),
        Doctor(id="OL3", name="Dr Claesson", role=Role.ÖVERLÄKARE,
               can_backup_call=True, exempt_from_call=True,
               competencies=["knäprotes", "artroskopi", "rygg"]),
        Doctor(id="OL4", name="Dr Danielsson", role=Role.ÖVERLÄKARE,
               can_backup_call=True, competencies=["höftprotes", "trauma", "höftfraktur"]),
        Doctor(id="OL5", name="Dr Eriksson", role=Role.ÖVERLÄKARE,
               can_backup_call=True, employment_rate=0.8,
               competencies=["axelkirurgi", "artroskopi", "trauma"]),
        # Specialister (8 st)
        Doctor(id="SP1", name="Dr Fredriksson", role=Role.SPECIALIST,
               can_primary_call=True, can_backup_call=True,
               competencies=["höftprotes", "knäprotes", "höftfraktur"]),
        Doctor(id="SP2", name="Dr Gustafsson", role=Role.SPECIALIST,
               can_primary_call=True, can_backup_call=True,
               competencies=["höftprotes", "knäprotes", "revision"]),
        Doctor(id="SP3", name="Dr Holm", role=Role.SPECIALIST,
               can_primary_call=True, can_backup_call=True,
               competencies=["trauma", "höftfraktur", "fotledsfraktur"]),
        Doctor(id="SP4", name="Dr Isaksson", role=Role.SPECIALIST,
               can_primary_call=True, can_backup_call=True,
               competencies=["artroskopi", "korsband", "axelkirurgi"]),
        Doctor(id="SP5", name="Dr Johansson", role=Role.SPECIALIST,
               can_primary_call=True, can_backup_call=True,
               competencies=["rygg", "höftprotes", "trauma"]),
        Doctor(id="SP6", name="Dr Karlsson", role=Role.SPECIALIST,
               can_primary_call=True, can_backup_call=True,
               competencies=["höftprotes", "knäprotes", "fotkirurgi"]),
        Doctor(id="SP7", name="Dr Lindberg", role=Role.SPECIALIST,
               can_primary_call=True, can_backup_call=True,
               competencies=["trauma", "höftfraktur", "handledsbrott"]),
        Doctor(id="SP8", name="Dr Magnusson", role=Role.SPECIALIST,
               can_primary_call=True, can_backup_call=True,
               site_preference=SITE_H,
               competencies=["höftprotes", "knäprotes", "revision"]),
        # ST-läkare (7 st)
        Doctor(id="ST1", name="Dr Nilsson (ST5)", role=Role.ST_SEN,
               can_primary_call=True, supervisor_id="SP1",
               competencies=["höftprotes", "knäprotes", "höftfraktur"],
               required_procedures={"höftprotes": 20, "knäprotes": 15, "höftfraktur": 25},
               completed_procedures={"höftprotes": 16, "knäprotes": 11, "höftfraktur": 20}),
        Doctor(id="ST2", name="Dr Olsson (ST4)", role=Role.ST_SEN,
               can_primary_call=True, supervisor_id="SP2",
               competencies=["höftprotes", "trauma", "artroskopi"],
               required_procedures={"höftprotes": 20, "knäprotes": 15, "artroskopi": 20},
               completed_procedures={"höftprotes": 10, "knäprotes": 7, "artroskopi": 12}),
        Doctor(id="ST3", name="Dr Persson (ST3)", role=Role.ST_TIDIG,
               can_primary_call=False, supervisor_id="SP3",
               competencies=["höftfraktur", "fotledsfraktur", "handledsbrott"],
               required_procedures={"höftfraktur": 25, "fotledsfraktur": 15, "handledsbrott": 20},
               completed_procedures={"höftfraktur": 8, "fotledsfraktur": 5, "handledsbrott": 10}),
        Doctor(id="ST4", name="Dr Rosén (ST3)", role=Role.ST_TIDIG,
               can_primary_call=False, supervisor_id="SP4",
               competencies=["artroskopi", "trauma"],
               required_procedures={"artroskopi": 20, "korsband": 10, "axelkirurgi": 10},
               completed_procedures={"artroskopi": 6, "korsband": 2, "axelkirurgi": 3}),
        Doctor(id="ST5", name="Dr Svensson (ST2)", role=Role.ST_TIDIG,
               can_primary_call=False, supervisor_id="SP5",
               competencies=["trauma", "handledsbrott"],
               required_procedures={"höftfraktur": 25, "handledsbrott": 20, "fotledsfraktur": 15},
               completed_procedures={"höftfraktur": 3, "handledsbrott": 5, "fotledsfraktur": 2}),
        Doctor(id="ST6", name="Dr Ström (ST4)", role=Role.ST_SEN,
               can_primary_call=True, supervisor_id="SP6",
               competencies=["höftprotes", "knäprotes", "fotkirurgi"],
               required_procedures={"höftprotes": 20, "knäprotes": 15, "fotkirurgi": 10},
               completed_procedures={"höftprotes": 14, "knäprotes": 10, "fotkirurgi": 6}),
        Doctor(id="ST7", name="Dr Torres (ST1)", role=Role.ST_TIDIG,
               can_primary_call=False, supervisor_id="SP7",
               competencies=["trauma"],
               required_procedures={"höftfraktur": 25, "handledsbrott": 20},
               completed_procedures={"höftfraktur": 0, "handledsbrott": 2}),
        # Underläkare (5 st)
        Doctor(id="UL1", name="Dr Wallin", role=Role.UNDERLÄKARE,
               competencies=["grundläggande ortopedi"]),
        Doctor(id="UL2", name="Dr Xiang", role=Role.UNDERLÄKARE,
               competencies=["grundläggande ortopedi"]),
        Doctor(id="UL3", name="Dr Yilmaz", role=Role.UNDERLÄKARE,
               competencies=["grundläggande ortopedi"]),
        Doctor(id="UL4", name="Dr Åberg", role=Role.UNDERLÄKARE,
               competencies=["grundläggande ortopedi"]),
        Doctor(id="UL5", name="Dr Öberg", role=Role.UNDERLÄKARE,
               employment_rate=0.5,
               competencies=["grundläggande ortopedi"]),
    ]

    operating_rooms = [
        OperatingRoom(id="H_OP1", site=SITE_H, name="Sal 1 (höft/knä)"),
        OperatingRoom(id="H_OP2", site=SITE_H, name="Sal 2 (höft/knä)"),
        OperatingRoom(id="H_OP3", site=SITE_H, name="Sal 3 (artroskopi/axel)"),
        OperatingRoom(id="H_OP4", site=SITE_H, name="Sal 4 (rygg/fot)"),
        OperatingRoom(id="H_OP5", site=SITE_H, name="Sal 5 (blandad)", available_days=[0,1,2,3]),
        OperatingRoom(id="C_OP1", site=SITE_CSK, name="Akutsal 1"),
        OperatingRoom(id="C_OP2", site=SITE_CSK, name="Akutsal 2"),
    ]

    staffing = [
        StaffingRequirement(Function.OPERATION, ShiftType.DAG, SITE_H,
                          min_count=2, required_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE, Role.ST_SEN, Role.ST_TIDIG],
                          min_senior=1),
        StaffingRequirement(Function.OPERATION, ShiftType.DAG, SITE_CSK,
                          min_count=2, required_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE, Role.ST_SEN, Role.ST_TIDIG],
                          min_senior=1),
        StaffingRequirement(Function.AVDELNING, ShiftType.DAG, SITE_CSK,
                          min_count=2, required_roles=[Role.SPECIALIST, Role.ST_SEN, Role.ST_TIDIG, Role.UNDERLÄKARE]),
        StaffingRequirement(Function.AVDELNING, ShiftType.DAG, SITE_H,
                          min_count=1, required_roles=[Role.SPECIALIST, Role.ST_SEN, Role.ST_TIDIG, Role.UNDERLÄKARE]),
        StaffingRequirement(Function.MOTTAGNING, ShiftType.DAG, SITE_CSK,
                          min_count=2, required_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE, Role.ST_SEN]),
        StaffingRequirement(Function.MOTTAGNING, ShiftType.DAG, SITE_H,
                          min_count=1, required_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE, Role.ST_SEN]),
        StaffingRequirement(Function.PRIMÄRJOUR, ShiftType.KVÄLLSJOUR, SITE_CSK,
                          min_count=1, required_roles=[Role.ST_SEN, Role.SPECIALIST]),
        StaffingRequirement(Function.PRIMÄRJOUR, ShiftType.NATTJOUR, SITE_CSK,
                          min_count=1, required_roles=[Role.ST_SEN, Role.SPECIALIST]),
        StaffingRequirement(Function.BAKJOUR, ShiftType.KVÄLLSJOUR, SITE_CSK,
                          min_count=1, required_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE]),
        StaffingRequirement(Function.BAKJOUR, ShiftType.NATTJOUR, SITE_CSK,
                          min_count=1, required_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE]),
    ]

    call_structure = CallStructure(
        primary_roles=[Role.ST_SEN, Role.SPECIALIST],
        backup_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE],
        max_calls_per_month=4,
        max_consecutive_nights=1,
        max_weekend_frequency=4,
        rest_after_night=True,
        backup_is_on_site=False,
    )

    preferences = [
        Preference("OL1", "SEMESTER", 1, {"start_week": 28, "end_week": 30}),
        Preference("SP2", "SEMESTER", 1, {"start_week": 29, "end_week": 31}),
        Preference("ST1", "OP_PREF", 2, {"want_procedure": "höftprotes", "want_with": "SP1"}),
        Preference("SP5", "SKIFT_PREF", 3, {"avoid": "NATT"}),
        Preference("OL5", "LEDIG_DAG", 1, {"weekday": 4}),
    ]

    return ClinicConfig(
        name="Ortopedkliniken Kristianstad-Hässleholm",
        sites=[SITE_CSK, SITE_H],
        doctors=doctors,
        operating_rooms=operating_rooms,
        staffing_requirements=staffing,
        call_structure=call_structure,
        atl_rules=ATLRules(),
        preferences=preferences,
        schedule_cycle_weeks=10,
        travel_time_between_sites_min=45,
    )


def create_generic_example() -> ClinicConfig:
    """
    Generisk exempelkonfiguration: 1 sjukhus, 15 läkare, 2 salar.
    Tillräckligt stor för att alla constraints ska vara satisfierbara.
    """
    SITE = "Huvudsjukhuset"

    doctors = [
        # Överläkare (4 st — bakjour + seniornärvaro)
        Doctor(id="OL1", name="Dr Svensson", role=Role.ÖVERLÄKARE,
               can_backup_call=True, competencies=["allmänkirurgi"]),
        Doctor(id="OL2", name="Dr Johansson", role=Role.ÖVERLÄKARE,
               can_backup_call=True, competencies=["allmänkirurgi"]),
        Doctor(id="OL3", name="Dr Wallin", role=Role.ÖVERLÄKARE,
               can_backup_call=True, competencies=["allmänkirurgi"]),
        Doctor(id="OL4", name="Dr Åström", role=Role.ÖVERLÄKARE,
               can_backup_call=True, competencies=["allmänkirurgi"]),
        # Specialister (6 st — bakjour + primärjour)
        Doctor(id="SP1", name="Dr Eriksson", role=Role.SPECIALIST,
               can_primary_call=True, can_backup_call=True, competencies=["allmänkirurgi"]),
        Doctor(id="SP2", name="Dr Lindberg", role=Role.SPECIALIST,
               can_primary_call=True, can_backup_call=True, competencies=["allmänkirurgi"]),
        Doctor(id="SP3", name="Dr Bergström", role=Role.SPECIALIST,
               can_primary_call=True, can_backup_call=True, competencies=["allmänkirurgi"]),
        Doctor(id="SP4", name="Dr Holm", role=Role.SPECIALIST,
               can_primary_call=True, can_backup_call=True, competencies=["allmänkirurgi"]),
        Doctor(id="SP5", name="Dr Lund", role=Role.SPECIALIST,
               can_primary_call=True, can_backup_call=True, competencies=["allmänkirurgi"]),
        Doctor(id="SP6", name="Dr Nordin", role=Role.SPECIALIST,
               can_primary_call=True, can_backup_call=True, competencies=["allmänkirurgi"]),
        # ST-läkare (5 st — primärjour)
        Doctor(id="ST1", name="Dr Nilsson", role=Role.ST_SEN,
               can_primary_call=True, supervisor_id="SP1", competencies=["allmänkirurgi"]),
        Doctor(id="ST2", name="Dr Persson", role=Role.ST_SEN,
               can_primary_call=True, supervisor_id="SP2", competencies=["allmänkirurgi"]),
        Doctor(id="ST3", name="Dr Andersson", role=Role.ST_SEN,
               can_primary_call=True, supervisor_id="SP3", competencies=["allmänkirurgi"]),
        Doctor(id="ST4", name="Dr Rosén", role=Role.ST_SEN,
               can_primary_call=True, supervisor_id="SP4", competencies=["allmänkirurgi"]),
        Doctor(id="ST5", name="Dr Ekström", role=Role.ST_TIDIG,
               supervisor_id="SP5", competencies=["allmänkirurgi"]),
        # Underläkare (3 st)
        Doctor(id="UL1", name="Dr Olsson", role=Role.UNDERLÄKARE,
               competencies=["grundläggande"]),
        Doctor(id="UL2", name="Dr Karlsson", role=Role.UNDERLÄKARE,
               competencies=["grundläggande"]),
        Doctor(id="UL3", name="Dr Åberg", role=Role.UNDERLÄKARE,
               competencies=["grundläggande"]),
    ]

    operating_rooms = [
        OperatingRoom(id="OP1", site=SITE, name="Sal 1"),
        OperatingRoom(id="OP2", site=SITE, name="Sal 2"),
    ]

    staffing = [
        StaffingRequirement(Function.OPERATION, ShiftType.DAG, SITE,
                          min_count=2, required_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE, Role.ST_SEN, Role.ST_TIDIG],
                          min_senior=1),
        StaffingRequirement(Function.AVDELNING, ShiftType.DAG, SITE,
                          min_count=1, required_roles=[Role.SPECIALIST, Role.ST_SEN, Role.ST_TIDIG, Role.UNDERLÄKARE]),
        StaffingRequirement(Function.MOTTAGNING, ShiftType.DAG, SITE,
                          min_count=1, required_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE, Role.ST_SEN]),
        StaffingRequirement(Function.PRIMÄRJOUR, ShiftType.KVÄLLSJOUR, SITE,
                          min_count=1, required_roles=[Role.ST_SEN, Role.SPECIALIST]),
        StaffingRequirement(Function.PRIMÄRJOUR, ShiftType.NATTJOUR, SITE,
                          min_count=1, required_roles=[Role.ST_SEN, Role.SPECIALIST]),
        StaffingRequirement(Function.BAKJOUR, ShiftType.KVÄLLSJOUR, SITE,
                          min_count=1, required_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE]),
        StaffingRequirement(Function.BAKJOUR, ShiftType.NATTJOUR, SITE,
                          min_count=1, required_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE]),
    ]

    call_structure = CallStructure(
        primary_roles=[Role.ST_SEN, Role.SPECIALIST],
        backup_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE],
    )

    return ClinicConfig(
        name="Generisk klinik",
        sites=[SITE],
        doctors=doctors,
        operating_rooms=operating_rooms,
        staffing_requirements=staffing,
        call_structure=call_structure,
        atl_rules=ATLRules(),
        preferences=[],
        schedule_cycle_weeks=4,
    )


# === Serialisering ===

def _enum_val(v):
    return v.value if hasattr(v, 'value') else str(v)

def config_to_dict(config: ClinicConfig) -> dict:
    """Serialisera ClinicConfig till JSON-kompatibel dict."""
    return {
        "name": config.name,
        "sites": config.sites,
        "doctors": [
            {
                "id": d.id, "name": d.name, "role": _enum_val(d.role),
                "site_preference": d.site_preference, "employment_rate": d.employment_rate,
                "can_primary_call": d.can_primary_call, "can_backup_call": d.can_backup_call,
                "exempt_from_call": d.exempt_from_call, "supervisor_id": d.supervisor_id,
                "competencies": d.competencies,
                "required_procedures": d.required_procedures,
                "completed_procedures": d.completed_procedures,
            } for d in config.doctors
        ],
        "operating_rooms": [
            {"id": r.id, "site": r.site, "name": r.name, "available_days": r.available_days,
             "requires_senior": r.requires_senior, "requires_assistant": r.requires_assistant}
            for r in config.operating_rooms
        ],
        "staffing_requirements": [
            {"function": _enum_val(s.function), "shift_type": _enum_val(s.shift_type),
             "site": s.site, "min_count": s.min_count,
             "required_roles": [_enum_val(r) for r in s.required_roles], "min_senior": s.min_senior}
            for s in config.staffing_requirements
        ],
        "call_structure": {
            "primary_roles": [_enum_val(r) for r in config.call_structure.primary_roles],
            "backup_roles": [_enum_val(r) for r in config.call_structure.backup_roles],
            "max_calls_per_month": config.call_structure.max_calls_per_month,
            "max_consecutive_nights": config.call_structure.max_consecutive_nights,
            "max_weekend_frequency": config.call_structure.max_weekend_frequency,
            "rest_after_night": config.call_structure.rest_after_night,
            "backup_is_on_site": config.call_structure.backup_is_on_site,
        },
        "atl_rules": {
            "min_daily_rest_hours": config.atl_rules.min_daily_rest_hours,
            "min_weekly_rest_hours": config.atl_rules.min_weekly_rest_hours,
            "max_weekly_hours": config.atl_rules.max_weekly_hours,
            "max_consecutive_work_hours": config.atl_rules.max_consecutive_work_hours,
        },
        "preferences": [
            {"doctor_id": p.doctor_id, "type": p.type, "priority": p.priority, "details": p.details}
            for p in config.preferences
        ],
        "shift_definitions": [
            {"id": s.id, "name": s.name, "function": _enum_val(s.function), "site": s.site,
             "start_time": s.start_time, "end_time": s.end_time, "duration_hours": s.duration_hours,
             "min_staff": s.min_staff, "max_staff": s.max_staff,
             "required_roles": [_enum_val(r) for r in s.required_roles],
             "required_competencies": s.required_competencies, "is_on_call": s.is_on_call}
            for s in config.shift_definitions
        ],
        "constraint_rules": [
            {"id": c.id, "name": c.name, "category": c.category,
             "is_hard": c.is_hard, "weight": c.weight, "enabled": c.enabled,
             "parameters": c.parameters}
            for c in config.constraint_rules
        ],
        "schedule_cycle_weeks": config.schedule_cycle_weeks,
        "travel_time_between_sites_min": config.travel_time_between_sites_min,
    }


def _role_from_str(s: str) -> Role:
    for r in Role:
        if r.value == s:
            return r
    return Role.UNDERLÄKARE

def _func_from_str(s: str) -> Function:
    for f in Function:
        if f.value == s:
            return f
    return Function.LEDIG

def _shift_from_str(s: str) -> ShiftType:
    for st in ShiftType:
        if st.value == s:
            return st
    return ShiftType.DAG


def dict_to_config(data: dict) -> ClinicConfig:
    """Deserialisera dict till ClinicConfig."""
    doctors = [
        Doctor(
            id=d["id"], name=d["name"], role=_role_from_str(d["role"]),
            site_preference=d.get("site_preference"),
            employment_rate=d.get("employment_rate", 1.0),
            can_primary_call=d.get("can_primary_call", False),
            can_backup_call=d.get("can_backup_call", False),
            exempt_from_call=d.get("exempt_from_call", False),
            supervisor_id=d.get("supervisor_id"),
            competencies=d.get("competencies", []),
            required_procedures=d.get("required_procedures", {}),
            completed_procedures=d.get("completed_procedures", {}),
        ) for d in data.get("doctors", [])
    ]
    rooms = [
        OperatingRoom(
            id=r["id"], site=r["site"], name=r["name"],
            available_days=r.get("available_days", [0,1,2,3,4]),
            requires_senior=r.get("requires_senior", True),
            requires_assistant=r.get("requires_assistant", True),
        ) for r in data.get("operating_rooms", [])
    ]
    staffing = [
        StaffingRequirement(
            function=_func_from_str(s["function"]),
            shift_type=_shift_from_str(s["shift_type"]),
            site=s["site"], min_count=s["min_count"],
            required_roles=[_role_from_str(r) for r in s.get("required_roles", [])],
            min_senior=s.get("min_senior", 0),
        ) for s in data.get("staffing_requirements", [])
    ]
    cs_data = data.get("call_structure", {})
    call_structure = CallStructure(
        primary_roles=[_role_from_str(r) for r in cs_data.get("primary_roles", ["ST_SEN", "SP"])],
        backup_roles=[_role_from_str(r) for r in cs_data.get("backup_roles", ["SP", "ÖL"])],
        max_calls_per_month=cs_data.get("max_calls_per_month", 4),
        max_consecutive_nights=cs_data.get("max_consecutive_nights", 1),
        max_weekend_frequency=cs_data.get("max_weekend_frequency", 4),
        rest_after_night=cs_data.get("rest_after_night", True),
        backup_is_on_site=cs_data.get("backup_is_on_site", False),
    )
    atl_data = data.get("atl_rules", {})
    atl = ATLRules(
        min_daily_rest_hours=atl_data.get("min_daily_rest_hours", 11.0),
        min_weekly_rest_hours=atl_data.get("min_weekly_rest_hours", 36.0),
        max_weekly_hours=atl_data.get("max_weekly_hours", 48.0),
        max_consecutive_work_hours=atl_data.get("max_consecutive_work_hours", 13.0),
    )
    prefs = [
        Preference(p["doctor_id"], p["type"], p.get("priority", 2), p.get("details", {}))
        for p in data.get("preferences", [])
    ]
    shifts = [
        ShiftDefinition(
            id=s["id"], name=s["name"], function=_func_from_str(s["function"]),
            site=s["site"], start_time=s.get("start_time", "07:00"),
            end_time=s.get("end_time", "16:30"), duration_hours=s.get("duration_hours", 9.5),
            min_staff=s.get("min_staff", 1), max_staff=s.get("max_staff", 10),
            required_roles=[_role_from_str(r) for r in s.get("required_roles", [])],
            required_competencies=s.get("required_competencies", []),
            is_on_call=s.get("is_on_call", False),
        ) for s in data.get("shift_definitions", [])
    ]
    rules = [
        ConstraintRule(
            id=c["id"], name=c["name"], category=c.get("category", ""),
            is_hard=c.get("is_hard", False), weight=c.get("weight", 5),
            enabled=c.get("enabled", True), parameters=c.get("parameters", {}),
        ) for c in data.get("constraint_rules", [])
    ] if data.get("constraint_rules") else default_constraint_rules()

    return ClinicConfig(
        name=data["name"],
        sites=data.get("sites", []),
        doctors=doctors,
        operating_rooms=rooms,
        staffing_requirements=staffing,
        call_structure=call_structure,
        atl_rules=atl,
        preferences=prefs,
        shift_definitions=shifts,
        constraint_rules=rules,
        schedule_cycle_weeks=data.get("schedule_cycle_weeks", 10),
        travel_time_between_sites_min=data.get("travel_time_between_sites_min", 0),
    )


if __name__ == "__main__":
    config = create_kristianstad_example()
    print(f"Klinik: {config.name}")
    print(f"Sites: {config.sites}")
    print(f"Antal läkare: {len(config.doctors)}")
    for role in Role:
        count = sum(1 for d in config.doctors if d.role == role)
        if count:
            print(f"  {role.value}: {count}")
    print(f"Antal op-salar: {len(config.operating_rooms)}")
    for site in config.sites:
        count = sum(1 for r in config.operating_rooms if r.site == site)
        print(f"  {site}: {count}")

    print("\n--- Generisk ---")
    g = create_generic_example()
    print(f"Klinik: {g.name}")
    print(f"Sites: {g.sites}")
    print(f"Läkare: {len(g.doctors)}, Salar: {len(g.operating_rooms)}")
