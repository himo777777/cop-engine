"""
COP Engine — Tester för nya per-doktor constraints
=====================================================
Testar: bakjourslinje, konsultschema, senior/junior OP-par,
AT-rotation, ST-randning, ST OP-krav, och serialisering.
"""

import pytest
import copy
from data_model import (
    Role, Doctor, OperatingRoom, StaffingRequirement, CallStructure,
    ATLRules, ClinicConfig, Function, ShiftType, Preference,
    config_to_dict, dict_to_config, create_kristianstad_example,
)
from solver import solve_schedule, _build_functions


@pytest.fixture(scope="module")
def base_config():
    """Kristianstad-config som bas — bevisad lösbar."""
    config = create_kristianstad_example()
    config.schedule_start_date = "2026-04-06"
    return config


@pytest.fixture(scope="module")
def base_schedule(base_config):
    """Löst schema att referera till."""
    return solve_schedule(base_config, num_weeks=2, time_limit_seconds=30)


class TestBakjourslinje:
    """Testar bakjourslinje — per-läkare constraint (solver knowledge)."""

    def test_bakjour_max_per_month(self, base_config):
        """Seniora med backup_call_config.max_per_month ska inte överskrida maxgränsen."""
        config = copy.deepcopy(base_config)
        # Hitta en ÖL och sätt max 1 bakjour
        target_ol = None
        for d in config.doctors:
            if d.role == Role.ÖVERLÄKARE and d.can_backup_call:
                d.backup_call_config = {"eligible": True, "max_per_month": 1}
                target_ol = d.id
                break

        assert target_ol is not None, "Ingen ÖL med bakjour hittad"
        schedule = solve_schedule(config, num_weeks=2, time_limit_seconds=20)
        assert schedule is not None, "Solver hittade ingen lösning"

        # Räkna bakjourer
        bak_count = sum(1 for d, func in schedule[target_ol].items() if func == "JOUR_B")
        assert bak_count <= 1, f"{target_ol} har {bak_count} bakjourer, max 1"


class TestKonsultschema:
    """Testar konsultschema — mjukt constraint för konsultdagar."""

    def test_konsult_dag_preferens(self, base_config):
        """Läkare med konsultschema bör oftare vara på MOTT på konsultdagar (mjukt)."""
        config = copy.deepcopy(base_config)
        target = None
        for d in config.doctors:
            if d.role in (Role.ÖVERLÄKARE, Role.SPECIALIST):
                d.consultation_schedule = [
                    {"weekday": "monday", "type": "telefon"},
                ]
                target = d.id
                break

        assert target is not None
        schedule = solve_schedule(config, num_weeks=2, time_limit_seconds=20)
        assert schedule is not None, "Solver hittade ingen lösning"
        # Mjukt constraint — vi verifierar bara att solvern löser sig


class TestSeniorJuniorOPPar:
    """Testar senior/junior OP-parning — hård constraint."""

    def test_junior_on_op_requires_senior(self, base_config):
        """Junior med require_senior_pair=True ska ALDRIG vara ensam på OP."""
        config = copy.deepcopy(base_config)
        target_junior = None
        for d in config.doctors:
            if d.role == Role.ST_TIDIG:
                d.op_pairing = {"require_senior_pair": True}
                target_junior = d.id
                break
        for d in config.doctors:
            if d.role in (Role.SPECIALIST, Role.ÖVERLÄKARE):
                d.op_pairing = {"can_supervise": ["ST_TIDIG", "AT"]}

        if target_junior is None:
            pytest.skip("Ingen ST_TIDIG i konfigurationen")

        schedule = solve_schedule(config, num_weeks=2, time_limit_seconds=20)
        assert schedule is not None, "Solver hittade ingen lösning"

        # Kontrollera: varje dag junior är på OP, finns senior på samma OP
        for day in range(14):
            if day % 7 >= 5:
                continue
            func = schedule.get(target_junior, {}).get(day)
            if func and func.startswith("OP_"):
                seniors_same_op = [
                    doc_id for doc_id, days in schedule.items()
                    if doc_id != target_junior
                    and days.get(day) == func
                    and any(d2.id == doc_id and d2.role in (Role.SPECIALIST, Role.ÖVERLÄKARE)
                            for d2 in config.doctors)
                ]
                assert len(seniors_same_op) >= 1, (
                    f"Dag {day}: {target_junior} på {func} utan senior"
                )


class TestATRotation:
    """Testar AT-rotation — fasta veckodagar per funktion."""

    def test_at_weekly_rotation_enforced(self, base_config):
        """AT-läkare med at_weekly_rotation ska respektera veckodagsschema."""
        config = copy.deepcopy(base_config)
        target_at = None
        # Hitta en AT-läkare och tilldela en rotation
        for d in config.doctors:
            if d.role == Role.AT:
                site = config.sites[0]
                d.at_weekly_rotation = {
                    "monday": f"AVD_{site}",
                    "tuesday": f"AVD_{site}",
                }
                d.at_rotation_period = {
                    "start_date": "2026-04-06",
                    "end_date": "2026-06-30",
                }
                target_at = d.id
                break

        if target_at is None:
            pytest.skip("Ingen AT-läkare i konfigurationen")

        schedule = solve_schedule(config, num_weeks=2, time_limit_seconds=20)
        assert schedule is not None, "Solver hittade ingen lösning"

        # Kolla att AT faktiskt har AVD på måndagar/tisdagar
        site = config.sites[0]
        expected_func = f"AVD_{site}"
        for day in range(14):
            if day % 7 in (0, 1):  # Måndag, tisdag
                func = schedule.get(target_at, {}).get(day)
                if func and func != "LEDIG":
                    assert func == expected_func, (
                        f"{target_at} dag {day}: förväntade {expected_func}, fick {func}"
                    )


class TestSTOPKrav:
    """Testar ST OP-krav — mjukt constraint."""

    def test_st_min_op_days(self, base_config):
        """ST med st_min_op_days ska sträva efter att ha OP-dagar (mjukt)."""
        config = copy.deepcopy(base_config)
        target_st = None
        for d in config.doctors:
            if d.role in (Role.ST_TIDIG, Role.ST_SEN):
                d.st_min_op_days = 1
                target_st = d.id
                break

        if target_st is None:
            pytest.skip("Ingen ST i konfigurationen")

        schedule = solve_schedule(config, num_weeks=2, time_limit_seconds=20)
        assert schedule is not None, "Solver hittade ingen lösning"

        # Mjukt — verifiera att lösning finns, logga OP-dagar
        for week in range(2):
            ws = week * 7
            op_count = sum(1 for d in range(ws, ws + 5)
                          if schedule.get(target_st, {}).get(d, "").startswith("OP_"))
            print(f"  {target_st} vecka {week}: {op_count} OP-dagar (mål: 1)")


class TestSerialization:
    """Testar att nya Doctor-fält serialiseras/deserialiseras korrekt."""

    def test_roundtrip_new_fields(self):
        """config_to_dict -> dict_to_config ska bevara alla nya fält."""
        config = create_kristianstad_example()
        config.schedule_start_date = "2026-04-06"

        # Sätt nya fält på den första läkaren (garanterat att finns)
        d0 = config.doctors[0]
        d0.at_weekly_rotation = {"monday": f"AVD_{config.sites[0]}", "friday": "ADMIN"}
        d0.at_rotation_period = {"start_date": "2026-04-01", "end_date": "2026-09-30"}
        d0.st_randning = [{"klinik": "Handkirurgi SUS", "start_date": "2026-05-01", "end_date": "2026-06-30"}]
        d0.st_min_op_days = 2
        d0.st_required_op_types = ["HOFT_PRIMA", "KNA_PRIMA"]
        d0.st_target_procedures = {"HOFT_PRIMA": {"goal": 20, "done": 5}}
        d0.backup_call_config = {"eligible": True, "max_per_month": 4}
        d0.consultation_schedule = [{"weekday": "monday", "type": "telefon"}]
        d0.op_pairing = {"require_senior_pair": True}

        data = config_to_dict(config)
        config2 = dict_to_config(data)

        # Verifiera på d0
        d0_new = next(d for d in config2.doctors if d.id == d0.id)
        assert d0_new.at_weekly_rotation == d0.at_weekly_rotation
        assert d0_new.at_rotation_period["start_date"] == "2026-04-01"
        assert len(d0_new.st_randning) == 1
        assert d0_new.st_randning[0]["klinik"] == "Handkirurgi SUS"
        assert d0_new.st_min_op_days == 2
        assert d0_new.st_required_op_types == ["HOFT_PRIMA", "KNA_PRIMA"]
        assert d0_new.st_target_procedures["HOFT_PRIMA"]["goal"] == 20
        assert d0_new.backup_call_config["eligible"] is True
        assert d0_new.backup_call_config["max_per_month"] == 4
        assert d0_new.consultation_schedule[0]["type"] == "telefon"
        assert d0_new.op_pairing["require_senior_pair"] is True

    def test_schedule_start_date_roundtrip(self):
        """schedule_start_date ska bevaras."""
        config = create_kristianstad_example()
        d = config_to_dict(config)
        d["schedule_start_date"] = "2026-04-06"
        config2 = dict_to_config(d)
        assert config2.schedule_start_date == "2026-04-06"

    def test_dict_to_config_empty_new_fields(self):
        """Nya fält ska ha tomma standardvärden om de saknas i dict."""
        minimal_dict = {
            "name": "Minimal",
            "sites": ["A"],
            "doctors": [{"id": "d1", "name": "Dr X", "role": "SP"}],
            "operating_rooms": [],
            "staffing_requirements": [],
            "call_structure": {},
            "atl_rules": {},
            "preferences": [],
        }
        config = dict_to_config(minimal_dict)
        doc = config.doctors[0]
        assert doc.at_weekly_rotation == {}
        assert doc.st_randning == []
        assert doc.st_min_op_days is None
        assert doc.backup_call_config == {}
        assert doc.consultation_schedule == []
        assert doc.op_pairing == {}


class TestUIConfigEndpoint:
    """Testar att _build_functions returnerar korrekta data."""

    def test_build_functions_correct(self):
        """_build_functions returnerar korrekta tuples."""
        config = create_kristianstad_example()
        day_funcs, call_funcs, op_by_site, akut_sites = _build_functions(config)

        for item in day_funcs:
            assert isinstance(item, tuple), f"Icke-tuple: {item}"
            assert len(item) == 3
            func_id, func_enum, site = item
            assert isinstance(func_id, str)

        # Alla sites bör ha OP-funktioner
        for site in config.sites:
            rooms = [r for r in config.operating_rooms if r.site == site]
            if rooms:
                assert site in op_by_site, f"Site {site} saknas i op_by_site"

        call_ids = [f[0] for f in call_funcs]
        assert "JOUR_P" in call_ids
        assert "JOUR_B" in call_ids
