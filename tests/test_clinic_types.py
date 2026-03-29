"""
Tests for multi-clinic-type support.
Verifies that the solver handles different healthcare settings:
- Vårdcentral (primary care, no OP, no jour)
- Internmedicin (internal medicine, no OP, has jour)
- Psykiatri (psychiatry, no OP, may have jour)
- Serialization roundtrip for new fields
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from data_model import (
    Doctor, Role, ClinicConfig, CallStructure, ATLRules,
    StaffingRequirement, Function, ShiftType,
    config_to_dict, dict_to_config, default_constraint_rules,
)
from solver import _build_functions, solve_schedule


def create_vardcentral_config() -> ClinicConfig:
    """Vårdcentral: distriktsläkare + SSK, ingen OP, ingen jour."""
    doctors = [
        Doctor(id="DL1", name="Dr Alm", role=Role.DISTRIKTSLÄKARE),
        Doctor(id="DL2", name="Dr Björk", role=Role.DISTRIKTSLÄKARE),
        Doctor(id="DL3", name="Dr Ceder", role=Role.DISTRIKTSLÄKARE),
        Doctor(id="SSK1", name="Ssk Dahlin", role=Role.SJUKSKÖTERSKA),
        Doctor(id="SSK2", name="Ssk Ek", role=Role.SJUKSKÖTERSKA),
        Doctor(id="FT1", name="Ft Falk", role=Role.FYSIOTERAPEUT),
        Doctor(id="PSY1", name="Psy Gran", role=Role.PSYKOLOG),
    ]
    return ClinicConfig(
        name="VC Söder",
        sites=["VC Söder"],
        doctors=doctors,
        operating_rooms=[],       # Ingen OP
        staffing_requirements=[],
        call_structure=CallStructure(
            primary_roles=[], backup_roles=[],
            max_calls_per_month=0,
        ),
        atl_rules=ATLRules(),
        preferences=[],
        constraint_rules=default_constraint_rules(),
        schedule_cycle_weeks=4,
        clinic_type="vardcentral",
        has_on_call=False,
        has_operations=False,
    )


def create_internmedicin_config() -> ClinicConfig:
    """Internmedicin: har jour men ingen OP.
    Needs enough doctors to cover jour 24/7 + rest days.
    """
    doctors = [
        Doctor(id="OL1", name="Dr Ström", role=Role.ÖVERLÄKARE, can_backup_call=True),
        Doctor(id="OL2", name="Dr Lind", role=Role.ÖVERLÄKARE, can_backup_call=True),
        Doctor(id="OL3", name="Dr Eld", role=Role.ÖVERLÄKARE, can_backup_call=True),
        Doctor(id="SP1", name="Dr Lund", role=Role.SPECIALIST, can_primary_call=True, can_backup_call=True),
        Doctor(id="SP2", name="Dr Berg", role=Role.SPECIALIST, can_primary_call=True, can_backup_call=True),
        Doctor(id="SP3", name="Dr Vik", role=Role.SPECIALIST, can_primary_call=True, can_backup_call=True),
        Doctor(id="SP4", name="Dr Hav", role=Role.SPECIALIST, can_primary_call=True, can_backup_call=True),
        Doctor(id="SP5", name="Dr Skog", role=Role.SPECIALIST, can_primary_call=True, can_backup_call=True),
        Doctor(id="SP6", name="Dr Mark", role=Role.SPECIALIST, can_primary_call=True, can_backup_call=True),
        Doctor(id="ST1", name="Dr Sjö (ST)", role=Role.ST_SEN, can_primary_call=True),
        Doctor(id="ST2", name="Dr Äng (ST)", role=Role.ST_SEN, can_primary_call=True),
        Doctor(id="ST3", name="Dr Bäck (ST)", role=Role.ST_TIDIG),
        Doctor(id="UL1", name="Dr Dal (UL)", role=Role.UNDERLÄKARE),
    ]
    # Adjust rules for internmedicin
    rules = default_constraint_rules()
    for r in rules:
        if r.id == "akut_staffing":
            r.enabled = False  # No AKUT function on internmedicin
        if r.id == "call_max_per_week":
            r.parameters["max_calls"] = 2  # Allow 2 jours/week (realistic for internmed)
    return ClinicConfig(
        name="Internmedicin CSK",
        sites=["CSK"],
        doctors=doctors,
        operating_rooms=[],
        staffing_requirements=[],
        call_structure=CallStructure(
            primary_roles=[Role.ST_SEN, Role.SPECIALIST],
            backup_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE],
            max_calls_per_month=6,
        ),
        atl_rules=ATLRules(),
        preferences=[],
        constraint_rules=rules,
        schedule_cycle_weeks=4,
        clinic_type="internmedicin",
        has_on_call=True,
        has_operations=False,
    )


def create_psykiatri_config() -> ClinicConfig:
    """Psykiatri: samtal, gruppterapi, akutpsyk, har jour."""
    doctors = [
        Doctor(id="OL1", name="Dr Ask", role=Role.ÖVERLÄKARE, can_backup_call=True),
        Doctor(id="SP1", name="Dr Björk", role=Role.SPECIALIST, can_primary_call=True, can_backup_call=True),
        Doctor(id="SP2", name="Dr Ceder", role=Role.SPECIALIST, can_primary_call=True),
        Doctor(id="ST1", name="Dr Ek (ST)", role=Role.ST_SEN, can_primary_call=True),
        Doctor(id="PSY1", name="Psy Furu", role=Role.PSYKOLOG),
        Doctor(id="KUR1", name="Kur Gran", role=Role.KURATOR),
    ]
    return ClinicConfig(
        name="Psykiatri Lund",
        sites=["Lund"],
        doctors=doctors,
        operating_rooms=[],
        staffing_requirements=[],
        call_structure=CallStructure(
            primary_roles=[Role.ST_SEN, Role.SPECIALIST],
            backup_roles=[Role.SPECIALIST, Role.ÖVERLÄKARE],
            max_calls_per_month=4,
        ),
        atl_rules=ATLRules(),
        preferences=[],
        constraint_rules=default_constraint_rules(),
        schedule_cycle_weeks=4,
        clinic_type="psykiatri",
        has_on_call=True,
        has_operations=False,
    )


# === _build_functions tests ===

class TestBuildFunctionsVardcentral:
    def test_no_op_functions(self):
        config = create_vardcentral_config()
        day_funcs, call_funcs, op_funcs, akut_sites = _build_functions(config)
        func_ids = [f[0] for f in day_funcs]
        assert not any(fid.startswith("OP_") for fid in func_ids), "Vårdcentral should have no OP functions"
        assert len(op_funcs) == 0

    def test_no_call_functions(self):
        config = create_vardcentral_config()
        day_funcs, call_funcs, op_funcs, akut_sites = _build_functions(config)
        assert len(call_funcs) == 0, "Vårdcentral should have no call functions"

    def test_has_vardcentral_functions(self):
        config = create_vardcentral_config()
        day_funcs, call_funcs, op_funcs, akut_sites = _build_functions(config)
        func_ids = [f[0] for f in day_funcs]
        assert "BVC" in func_ids, "Vårdcentral should have BVC"
        assert "MVC" in func_ids, "Vårdcentral should have MVC"
        assert "TELEFON" in func_ids, "Vårdcentral should have TELEFON"
        assert "HEMBESÖK" in func_ids, "Vårdcentral should have HEMBESÖK"
        assert "VIDEO" in func_ids, "Vårdcentral should have VIDEO"

    def test_has_mottagning(self):
        config = create_vardcentral_config()
        day_funcs, call_funcs, op_funcs, akut_sites = _build_functions(config)
        func_ids = [f[0] for f in day_funcs]
        assert any(fid.startswith("MOTT_") for fid in func_ids)

    def test_no_avd(self):
        config = create_vardcentral_config()
        day_funcs, call_funcs, op_funcs, akut_sites = _build_functions(config)
        func_ids = [f[0] for f in day_funcs]
        assert not any(fid.startswith("AVD_") for fid in func_ids), "Vårdcentral should have no AVD"


class TestBuildFunctionsInternmedicin:
    def test_no_op(self):
        config = create_internmedicin_config()
        day_funcs, call_funcs, op_funcs, akut_sites = _build_functions(config)
        func_ids = [f[0] for f in day_funcs]
        assert not any(fid.startswith("OP_") for fid in func_ids)

    def test_has_call(self):
        config = create_internmedicin_config()
        day_funcs, call_funcs, op_funcs, akut_sites = _build_functions(config)
        assert len(call_funcs) == 2  # JOUR_P, JOUR_B

    def test_has_internmedicin_functions(self):
        config = create_internmedicin_config()
        day_funcs, call_funcs, op_funcs, akut_sites = _build_functions(config)
        func_ids = [f[0] for f in day_funcs]
        assert "ROND" in func_ids
        assert "DIALYS" in func_ids
        assert "DAGVÅRD" in func_ids

    def test_has_avd(self):
        config = create_internmedicin_config()
        day_funcs, call_funcs, op_funcs, akut_sites = _build_functions(config)
        func_ids = [f[0] for f in day_funcs]
        assert any(fid.startswith("AVD_") for fid in func_ids)


class TestBuildFunctionsPsykiatri:
    def test_has_psyk_functions(self):
        config = create_psykiatri_config()
        day_funcs, call_funcs, op_funcs, akut_sites = _build_functions(config)
        func_ids = [f[0] for f in day_funcs]
        assert "SAMTAL" in func_ids
        assert "GRUPP" in func_ids
        assert "AKUTPSYK" in func_ids


# === Solver tests ===

class TestSolverVardcentral:
    def test_solver_finds_solution(self):
        """Solver should find a solution for a vårdcentral with no OP/no jour."""
        config = create_vardcentral_config()
        result = solve_schedule(config, num_weeks=2, time_limit_seconds=15)
        assert result is not None, "Solver should find a solution for vårdcentral"
        # Every doctor should have assignments
        for doc in config.doctors:
            assert doc.id in result, f"Doctor {doc.id} should be in schedule"

    def test_no_jour_in_schedule(self):
        """Jour functions should not appear in the schedule for a vårdcentral."""
        config = create_vardcentral_config()
        result = solve_schedule(config, num_weeks=2, time_limit_seconds=15)
        assert result is not None
        for doc_id, days in result.items():
            if doc_id.startswith("_"):
                continue
            for day, func in days.items():
                assert "JOUR" not in func, f"Jour should not appear in vårdcentral schedule: {doc_id} day {day} = {func}"


class TestSolverInternmedicin:
    def test_solver_finds_solution(self):
        """Solver should find a solution for internmedicin (jour but no OP)."""
        config = create_internmedicin_config()
        result = solve_schedule(config, num_weeks=2, time_limit_seconds=15)
        assert result is not None, "Solver should find a solution for internmedicin"

    def test_no_op_in_schedule(self):
        """OP functions should not appear in the schedule."""
        config = create_internmedicin_config()
        result = solve_schedule(config, num_weeks=2, time_limit_seconds=15)
        assert result is not None
        for doc_id, days in result.items():
            if doc_id.startswith("_"):
                continue
            for day, func in days.items():
                assert not func.startswith("OP_"), f"OP should not appear in internmedicin schedule"


# === Serialization tests ===

class TestSerializationNewFields:
    def test_roundtrip_vardcentral(self):
        """Config with new fields should survive serialization roundtrip."""
        config = create_vardcentral_config()
        d = config_to_dict(config)
        assert d["clinic_type"] == "vardcentral"
        assert d["has_on_call"] == False
        assert d["has_operations"] == False
        assert d["custom_roles"] == []
        assert d["custom_functions"] == []

        config2 = dict_to_config(d)
        assert config2.clinic_type == "vardcentral"
        assert config2.has_on_call == False
        assert config2.has_operations == False

    def test_roundtrip_with_custom_roles(self):
        """Custom roles/functions should survive roundtrip."""
        config = create_vardcentral_config()
        config.custom_roles = [{"id": "DISTRIKT_SSK", "label": "Distriktssköterska", "seniority": 2}]
        config.custom_functions = [{"id": "HEMSJUKVÅRD", "label": "Hemsjukvård", "category": "vård"}]

        d = config_to_dict(config)
        assert len(d["custom_roles"]) == 1
        assert d["custom_roles"][0]["id"] == "DISTRIKT_SSK"
        assert len(d["custom_functions"]) == 1

        config2 = dict_to_config(d)
        assert len(config2.custom_roles) == 1
        assert config2.custom_roles[0]["id"] == "DISTRIKT_SSK"
        assert len(config2.custom_functions) == 1

    def test_default_values_for_old_configs(self):
        """Old configs without new fields should get safe defaults."""
        minimal = {
            "name": "Old Clinic",
            "sites": ["A"],
            "doctors": [{"id": "d1", "name": "Doc", "role": "SP"}],
        }
        config = dict_to_config(minimal)
        assert config.clinic_type == "kirurgi"  # default
        assert config.has_on_call == True        # default
        assert config.has_operations == True     # default
        assert config.custom_roles == []
        assert config.custom_functions == []

    def test_schedule_start_date_roundtrip(self):
        config = create_vardcentral_config()
        config.schedule_start_date = "2026-05-01"
        d = config_to_dict(config)
        assert d["schedule_start_date"] == "2026-05-01"
        config2 = dict_to_config(d)
        assert config2.schedule_start_date == "2026-05-01"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
