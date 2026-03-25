"""
COP Engine — Pytest Fixtures
=============================
Delade fixtures för alla tester.
"""

import sys
import os
from datetime import date, timedelta

import pytest

# Lägg till cop-engine i sökvägen
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data_model import (
    ClinicConfig, Role, Doctor, OperatingRoom,
    StaffingRequirement, CallStructure, ATLRules, Preference,
    Function, ShiftType, create_kristianstad_example, create_generic_example,
)
from solver import solve_schedule


@pytest.fixture
def full_config():
    """Komplett Kristianstad-Hässleholm konfiguration (25 läkare, 7 salar)."""
    return create_kristianstad_example()


@pytest.fixture
def generic_config():
    """Generisk 1-sjukhus konfiguration (10 läkare, 3 salar)."""
    return create_generic_example()


@pytest.fixture
def small_config():
    """Minimal konfiguration för snabba tester (6 läkare, 2 salar)."""
    SITE_A = "Sjukhus_A"
    SITE_B = "Sjukhus_B"

    doctors = [
        Doctor(id="OL1", name="Dr A", role=Role.ÖVERLÄKARE,
               can_backup_call=True, competencies=["höftprotes", "trauma"]),
        Doctor(id="SP1", name="Dr B", role=Role.SPECIALIST,
               can_primary_call=True, can_backup_call=True,
               competencies=["höftprotes", "knäprotes"]),
        Doctor(id="SP2", name="Dr C", role=Role.SPECIALIST,
               can_primary_call=True, can_backup_call=True,
               competencies=["trauma", "höftfraktur"]),
        Doctor(id="ST1", name="Dr D", role=Role.ST_SEN,
               can_primary_call=True, supervisor_id="SP1",
               competencies=["höftprotes"],
               required_procedures={"höftprotes": 20},
               completed_procedures={"höftprotes": 10}),
        Doctor(id="ST2", name="Dr E", role=Role.ST_TIDIG,
               supervisor_id="SP2", competencies=["trauma"],
               required_procedures={"höftfraktur": 25},
               completed_procedures={"höftfraktur": 5}),
        Doctor(id="UL1", name="Dr F", role=Role.UNDERLÄKARE,
               competencies=["grundläggande ortopedi"]),
    ]

    rooms = [
        OperatingRoom(id="OP1", site=SITE_B, name="Sal 1"),
        OperatingRoom(id="OP2", site=SITE_A, name="Akutsal 1"),
    ]

    staffing = [
        StaffingRequirement(Function.OPERATION, ShiftType.DAG, SITE_B,
                            min_count=1, required_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE, Role.ST_SEN]),
        StaffingRequirement(Function.OPERATION, ShiftType.DAG, SITE_A,
                            min_count=1, required_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE, Role.ST_SEN]),
        StaffingRequirement(Function.AVDELNING, ShiftType.DAG, SITE_A,
                            min_count=1, required_roles=[Role.ST_TIDIG, Role.UNDERLÄKARE]),
        StaffingRequirement(Function.PRIMÄRJOUR, ShiftType.KVÄLLSJOUR, SITE_A,
                            min_count=1, required_roles=[Role.ST_SEN, Role.SPECIALIST]),
        StaffingRequirement(Function.BAKJOUR, ShiftType.KVÄLLSJOUR, SITE_A,
                            min_count=1, required_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE]),
    ]

    call_structure = CallStructure(
        primary_roles=[Role.ST_SEN, Role.SPECIALIST],
        backup_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE],
    )

    return ClinicConfig(
        name="Test-klinik",
        sites=[SITE_A, SITE_B],
        doctors=doctors,
        operating_rooms=rooms,
        staffing_requirements=staffing,
        call_structure=call_structure,
        atl_rules=ATLRules(),
        preferences=[],
    )


@pytest.fixture(scope="session")
def _session_config():
    """Session-wide config (skapas bara en gång)."""
    return create_kristianstad_example()


@pytest.fixture(scope="session")
def _session_schedule(_session_config):
    """Session-wide schema (löser bara en gång för alla tester)."""
    schedule = solve_schedule(_session_config, num_weeks=2, time_limit_seconds=30)
    assert schedule is not None, "Solver hittade ingen lösning"
    return schedule


@pytest.fixture
def solved_schedule(_session_schedule):
    """Genererat 2-veckorsschema — delar session-wide lösning."""
    import copy
    return copy.deepcopy(_session_schedule)


@pytest.fixture
def schedule_start_date():
    """Startdatum för testscheman."""
    return date(2026, 4, 6)  # Måndag
