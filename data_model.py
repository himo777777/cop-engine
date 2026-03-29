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
    AT = "AT"                  # AT-läkare (allmäntjänstgöring)
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
    # Granulära jourtyper (används i expanderat schema)
    JOUR_P_KVÄLL = "JOUR_P_KVÄLL"        # Primärjour kväll 16:30-22:00
    JOUR_P_NATT = "JOUR_P_NATT"          # Primärjour natt 22:00-07:00
    JOUR_P_HELGDAG = "JOUR_P_HELGDAG"    # Primärjour helgdag 07:00-22:00
    JOUR_P_HELGNATT = "JOUR_P_HELGNATT"  # Primärjour helgnatt 22:00-07:00
    JOUR_B_HELGDAG = "JOUR_B_HELGDAG"    # Bakjour helgdag
    JOUR_B_HELGNATT = "JOUR_B_HELGNATT"  # Bakjour helgnatt
    ADMIN = "ADMIN"            # MDT, rond, administration
    FORSKNING = "FORSKNING"    # Forskningsdagar
    HANDLEDNING = "HANDLEDNING"  # ST-handledning, AT-handledning
    UTBILDNING = "UTBILDNING"  # Kurser, konferenser, intern utbildning
    AKUT = "AKUT"              # Akutmottagningspass (dagtid)
    LEDIG = "LEDIG"
    SEMESTER = "SEMESTER"
    KOMPLEDIGHET = "KOMPLEDIGHET"  # Kompensationsledighet efter helgjour


# Site is now a plain string, not an enum.
# Examples: "CSK", "Hässleholm", "Karolinska Solna", "Huddinge"


@dataclass
class Doctor:
    """En läkare med alla relevanta attribut."""
    id: str
    name: str
    role: Role
    personal_number: str = ""               # Personnummer (för lönekoppling)
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

    # === AVANCERAD SCHEMALÄGGNING ===

    # Varannan vecka — "biweekly_even" = jämna veckor, "biweekly_odd" = udda, "weekly" = varje vecka
    schedule_pattern: str = "weekly"

    # Fasta veckodagar: {"monday": "OP_CSK", "wednesday": "MOTT_Hässleholm", "friday": "FORSKNING"}
    # Tom dict = inga fasta dagar (solvern väljer fritt)
    fixed_weekdays: dict = field(default_factory=dict)

    # Min passtyp per vecka: {"OP": 1, "MOTT": 1} = minst 1 OP + 1 MOTT per arbetsvecka
    min_shifts_per_week: dict = field(default_factory=dict)

    # Max passtyp per vecka: {"OP": 3} = högst 3 OP-dagar
    max_shifts_per_week: dict = field(default_factory=dict)

    # Halvdagar: {"monday": {"am": "ADMIN", "pm": "MOTT_CSK"}} — sub-dag-granularitet
    # Om en dag har halvdagsschema, överrider den dagtilldelningen
    half_day_schedule: dict = field(default_factory=dict)

    # AT-block: AT-läkare roterar i block, inte dagligen
    # {"block_type": "AVD_CSK", "start_date": "2026-04-01", "end_date": "2026-06-30"}
    current_rotation_block: dict = field(default_factory=dict)

    # AT-rotation: fast veckoschema per veckodag
    # {"monday": "AKUT", "tuesday": "AKUT", "wednesday": "TRAUMA", "thursday": "MOTT", "friday": "AVD"}
    at_weekly_rotation: dict = field(default_factory=dict)

    # AT-rotationsperiod: {"start_date": "2026-04-01", "end_date": "2026-09-30", "supervisor": "Dr X"}
    at_rotation_period: dict = field(default_factory=dict)

    # ST-randning: perioder då ST är på annan klinik
    # [{"klinik": "Handkirurgi SUS", "start_date": "2026-05-01", "end_date": "2026-06-30"}]
    st_randning: list = field(default_factory=list)

    # ST minsta OP-dagar per vecka (None = inget krav)
    st_min_op_days: Optional[int] = None

    # ST krav på OP-typer: ["HOFT_PRIMA", "KNA_PRIMA", "FRAKTUR"]
    st_required_op_types: list = field(default_factory=list)

    # ST procedurmål: {"HOFT_PRIMA": {"goal": 20, "done": 5}, "KNA_PRIMA": {"goal": 15, "done": 3}}
    st_target_procedures: dict = field(default_factory=dict)

    # Fasta återkommande aktiviteter: [{"weekday": "tuesday", "time": "10:00-11:00", "activity": "Infektionsrond"}]
    recurring_activities: list = field(default_factory=list)

    # Bakjourslinje: kan gå bakjour, vilka dagar per vecka
    # {"eligible": True, "max_per_month": 4, "preferred_days": ["monday", "thursday"]}
    backup_call_config: dict = field(default_factory=dict)

    # Konsultschema: dagar/tider då läkaren är tillgänglig för konsultationer
    # [{"weekday": "monday", "type": "telefon"}, {"weekday": "wednesday", "type": "rond"}]
    consultation_schedule: list = field(default_factory=list)

    # Senior/junior OP-par: kräv parning med specifik senioritet på OP
    # {"require_senior_pair": True, "preferred_senior_id": "doc_123", "can_supervise": ["ST_TIDIG", "AT"]}
    op_pairing: dict = field(default_factory=dict)

    # Dagar per vecka (explicit, annars beräknas från employment_rate)
    # T.ex. 3 = exakt 3 arbetsdagar per vecka (solvern respekterar detta)
    work_days_per_week: Optional[int] = None


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
    couple_evening_night: bool = True   # Kväll+natt alltid samma person
    backup_from_home: bool = True       # Bakjour = beredskapsjour hemifrån
    weekend_comp_primary: bool = True   # Helgkomp för primärjour (hård)
    weekend_comp_backup: bool = True    # Helgkomp för bakjour (mjuk)


# Alla func_id:n som representerar jour
JOUR_FUNC_IDS = frozenset({
    "JOUR_P", "JOUR_B",
    "JOUR_P_KVÄLL", "JOUR_P_NATT",
    "JOUR_P_HELGDAG", "JOUR_P_HELGNATT",
    "JOUR_B_HELGDAG", "JOUR_B_HELGNATT",
})


def is_jour(func_id: str) -> bool:
    """Returnerar True om func_id är en jourtyp."""
    return func_id in JOUR_FUNC_IDS


# Icke-kliniska funktioner (behöver inte bemanningskrav)
NON_CLINICAL_FUNC_IDS = frozenset({
    "ADMIN", "FORSKNING", "HANDLEDNING", "UTBILDNING",
    "LEDIG", "SEMESTER", "KOMPLEDIGHET",
})


def is_non_clinical(func_id: str) -> bool:
    """Returnerar True om func_id är icke-klinisk (admin/forskning/ledig etc)."""
    return func_id in NON_CLINICAL_FUNC_IDS


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
class OBRates:
    """OB-tillägg per tidsperiod (multiplikator)."""
    weekday_evening: float = 1.0    # Vardag 18-22
    weekday_night: float = 1.5      # Vardag 22-06
    saturday_day: float = 1.2       # Lördag 07-22
    sunday_day: float = 1.5         # Söndag/helgdag 07-22
    weekend_night: float = 2.0      # Helg 22-07


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
    is_half_day: bool = False          # True = halvdagspass (AM eller PM)
    half_day_period: str = ""          # "am" eller "pm" — bara relevant om is_half_day=True
    allows_recurring_activity: bool = True  # Kan ha inbäddade aktiviteter (ronder etc)


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
        ConstraintRule("weekend_compensation", "Ledig vardag efter helgjour", "fairness",
                       is_hard=False, weight=7, parameters={}),
        ConstraintRule("ob_cost_fairness", "Rättvis OB-fördelning", "fairness",
                       is_hard=False, weight=4, parameters={}),
        # === NYA CONSTRAINTS (Fas 1 expansion) ===
        ConstraintRule("biweekly_pattern", "Varannan-vecka-schema", "staffing",
                       is_hard=True, weight=10, parameters={}),
        ConstraintRule("min_shift_types", "Min passtyp per läkare/vecka", "staffing",
                       is_hard=False, weight=7, parameters={}),
        ConstraintRule("fixed_weekdays", "Fasta veckodagar per läkare", "staffing",
                       is_hard=True, weight=10, parameters={}),
        ConstraintRule("half_day_support", "Halvdagsschema AM/PM", "staffing",
                       is_hard=True, weight=10, parameters={}),
        ConstraintRule("st_supervisor_pairing", "ST med handledare på OP", "staffing",
                       is_hard=False, weight=8, parameters={}),
        ConstraintRule("at_block_rotation", "AT-block rotation (ej daglig)", "staffing",
                       is_hard=True, weight=10, parameters={}),
        ConstraintRule("akut_staffing", "AKUT-bemanning dagtid", "staffing",
                       is_hard=True, weight=10, parameters={"min_count": 3}),
        ConstraintRule("recurring_activities", "Fasta ronder/konferenser", "preference",
                       is_hard=False, weight=6, parameters={}),
        ConstraintRule("weekend_comp_auto", "Auto-kompledighet efter helgjour", "fairness",
                       is_hard=False, weight=7, parameters={"comp_day_offset": 1}),
        ConstraintRule("min_senior_coverage", "Minimum seniorbeläggning per vecka", "staffing",
                       is_hard=True, weight=10,
                       description="Minst 50% av seniora läkare (ÖL + SP) måste vara schemalagda varje vecka"),
        ConstraintRule("continuity_of_care", "Kontinuitetskrav (COC)", "quality",
                       is_hard=False, weight=3,
                       description="Gruppera mottagningsdagar (MOTT) konsekutivt per läkare per vecka — undviker glapp"),
        ConstraintRule("at_weekly_rotation", "AT-rotation veckoschema", "staffing",
                       is_hard=True, weight=10,
                       description="AT-läkare placeras enligt fast veckoschema: t.ex. 1 dag trauma, 2 dagar akut, 1 dag mott"),
        ConstraintRule("st_op_requirement", "ST minsta OP-dagar/vecka", "fairness",
                       is_hard=False, weight=5,
                       description="ST-läkare garanteras konfigurerat minimiantal OP-dagar per vecka för utbildningsmål"),
    ]
    # OBS: Bakjourslinje, konsultschema och senior/junior OP-parning är INTE
    # togglebara klinik-regler. De är inbyggd solver-kunskap som alltid är aktiv
    # och styrs per läkare via Doctor-fälten backup_call_config, consultation_schedule, op_pairing.


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
    max_concurrent_vacation: int = 0   # 0 = auto: ceil(len(doctors) * 0.2)
    travel_time_between_sites_min: int = 0
    ob_rates: OBRates = field(default_factory=OBRates)
    optimize_ob_cost: bool = True
    detailed_rules: list = field(default_factory=list)  # list[DetailedRule] från rule_engine
    comp_time_rates: "CompTimeRates" = None
    auto_plan_comp_days: bool = True
    max_comp_balance_days: float = 20.0


# === GRUNDSCHEMA ===

@dataclass
class BaseScheduleSlot:
    """En position i grundschemat."""
    doctor_id: str
    cycle_week: int
    weekday: int
    function: str
    site: str = None

@dataclass
class BaseSchedule:
    """Komplett grundschema — den roterande cykeln."""
    id: str
    name: str
    clinic_id: str
    cycle_length_weeks: int = 10
    slots: list = field(default_factory=list)
    effective_from: str = None
    effective_to: str = None
    created_at: str = None
    version: int = 1

@dataclass
class ScheduleDeviation:
    """En avvikelse från grundschemat."""
    id: str
    base_schedule_id: str
    date: str
    doctor_id: str
    original_function: str
    new_function: str
    reason: str
    approved_by: str = None
    created_at: str = None


# === KOMPENSATIONSTID ===

@dataclass
class CompTimeEntry:
    """En post i kompensationstidkontot."""
    id: str
    doctor_id: str
    date: str
    call_type: str
    hours_earned: float
    hours_used: float = 0.0
    status: str = "pending"
    planned_date: str = None

@dataclass
class CompTimeAccount:
    """Kompensationstidkonto per läkare."""
    doctor_id: str
    entries: list = field(default_factory=list)

    @property
    def balance_hours(self) -> float:
        return sum(e.hours_earned - e.hours_used for e in self.entries if e.status != "expired")

    @property
    def balance_days(self) -> float:
        return self.balance_hours / 8.0

@dataclass
class CompTimeRates:
    """Kompensationstid per jourtyp."""
    weekday_evening: float = 4.0
    weekday_night: float = 8.0
    weekend_day: float = 8.0
    weekend_night: float = 10.0
    holiday_day: float = 12.0
    holiday_night: float = 14.0


# === PEER-TO-PEER BYTE ===

class SwapRequestStatus(Enum):
    PENDING_PEER = "pending_peer"
    PEER_ACCEPTED = "peer_accepted"
    PEER_REJECTED = "peer_rejected"
    ATL_VALIDATED = "atl_validated"
    ATL_WARNING = "atl_warning"
    PENDING_ADMIN = "pending_admin"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

@dataclass
class SwapRequest:
    """Bytesförfrågan mellan två läkare."""
    id: str
    clinic_id: str
    requester_id: str
    requester_date: str
    requester_function: str
    target_id: str
    target_date: str
    target_function: str
    status: str = "pending_peer"
    message: str = ""
    atl_warnings: list = field(default_factory=list)
    peer_response_at: str = None
    admin_response_at: str = None
    admin_id: str = None
    created_at: str = None
    expires_at: str = None
    completed_at: str = None


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
        couple_evening_night=True,
        backup_from_home=True,
        weekend_comp_primary=True,
        weekend_comp_backup=True,
    )

    preferences = [
        Preference("OL1", "SEMESTER_BLOCK", 1, {"start_week": 0, "end_week": 0}),
        Preference("SP2", "SEMESTER_BLOCK", 2, {"start_week": 1, "end_week": 1}),
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
                # Avancerad schemaläggning
                "schedule_pattern": d.schedule_pattern,
                "fixed_weekdays": d.fixed_weekdays,
                "min_shifts_per_week": d.min_shifts_per_week,
                "max_shifts_per_week": d.max_shifts_per_week,
                "half_day_schedule": d.half_day_schedule,
                "current_rotation_block": d.current_rotation_block,
                "recurring_activities": d.recurring_activities,
                "work_days_per_week": d.work_days_per_week,
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
    if not s:
        return Function.LEDIG
    for f in Function:
        if f.value == s:
            return f
    # Stöd för alternativa namngivningar
    mapping = {
        "regular": Function.LEDIG,
        "jour": Function.PRIMÄRJOUR,
        "operation": Function.OPERATION,
        "mottagning": Function.MOTTAGNING,
        "avdelning": Function.AVDELNING,
        "admin": Function.ADMIN,
        "trauma": Function.AKUTMOTTAGNING,
    }
    return mapping.get(s.lower(), Function.LEDIG)

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
            # Avancerad schemaläggning
            schedule_pattern=d.get("schedule_pattern", "weekly"),
            fixed_weekdays=d.get("fixed_weekdays", {}),
            min_shifts_per_week=d.get("min_shifts_per_week", {}),
            max_shifts_per_week=d.get("max_shifts_per_week", {}),
            half_day_schedule=d.get("half_day_schedule", {}),
            current_rotation_block=d.get("current_rotation_block", {}),
            recurring_activities=d.get("recurring_activities", []),
            work_days_per_week=d.get("work_days_per_week"),
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
            id=s["id"], name=s["name"],
            function=_func_from_str(s.get("function") or s.get("type") or s.get("shift_type", "")),
            site=s.get("site", ""),
            start_time=s.get("start_time") or s.get("start", "07:00"),
            end_time=s.get("end_time") or s.get("end", "16:30"),
            duration_hours=s.get("duration_hours", 9.5),
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

