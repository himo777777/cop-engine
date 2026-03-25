"""
COP Engine — Tester för Solver
================================
Verifierar alla hårda och mjuka constraints.
Generiska tester — fungerar med vilken klinikkonfiguration som helst.
"""

import pytest
from collections import defaultdict
from data_model import Role, create_kristianstad_example, create_generic_example
from solver import solve_schedule, _build_functions


class TestSolverBasic:
    """Grundläggande solver-tester."""

    def test_solver_returns_schedule(self, full_config):
        schedule = solve_schedule(full_config, num_weeks=1, time_limit_seconds=15)
        assert schedule is not None, "Solver returnerade None"

    def test_all_doctors_in_schedule(self, full_config, solved_schedule):
        for doc in full_config.doctors:
            assert doc.id in solved_schedule, f"{doc.id} saknas i schemat"

    def test_all_days_covered(self, solved_schedule):
        for doc_id, days in solved_schedule.items():
            for d in range(14):
                assert d in days, f"{doc_id} saknar dag {d}"

    def test_schedule_uses_valid_functions(self, full_config, solved_schedule):
        day_funcs, call_funcs, _ = _build_functions(full_config)
        valid = {f[0] for f in day_funcs} | {f[0] for f in call_funcs} | {"LEDIG"}
        for doc_id, days in solved_schedule.items():
            for d, func in days.items():
                assert func in valid, f"{doc_id} dag {d}: ogiltig funktion '{func}'"


class TestConstraint1_ExactlyOneFunction:
    """Constraint 1: Varje läkare har exakt EN funktion per dag."""

    def test_one_function_per_day(self, solved_schedule):
        for doc_id, days in solved_schedule.items():
            assert len(days) == 14
            for d in range(14):
                assert d in days


class TestConstraint2_MinStaffing:
    """Constraint 2: Minimibemanning uppfylls."""

    def test_jour_every_day(self, solved_schedule):
        for day in range(14):
            primary = [d for d, days in solved_schedule.items() if days.get(day) == "JOUR_P"]
            backup = [d for d, days in solved_schedule.items() if days.get(day) == "JOUR_B"]
            assert len(primary) == 1, f"Dag {day}: {len(primary)} primärjourer"
            assert len(backup) == 1, f"Dag {day}: {len(backup)} bakjourer"

    def test_weekday_operations(self, full_config, solved_schedule):
        """Vardagar ska ha läkare på operation per site."""
        _, _, op_funcs = _build_functions(full_config)
        for day in range(14):
            if day % 7 >= 5:
                continue
            for site, op_func_id in op_funcs.items():
                count = sum(1 for d, days in solved_schedule.items() if days.get(day) == op_func_id)
                assert count >= 1, f"Dag {day}: ingen {op_func_id}"

    def test_weekday_avdelning(self, full_config, solved_schedule):
        day_funcs, _, _ = _build_functions(full_config)
        avd_funcs = {f[0] for f in day_funcs if f[1].value == "AVD"}
        for day in range(14):
            if day % 7 >= 5:
                continue
            avd = sum(1 for d, days in solved_schedule.items() if days.get(day) in avd_funcs)
            assert avd >= 1, f"Dag {day}: ingen avdelningsbemanning"


class TestConstraint3_CallRoles:
    """Constraint 3: Rätt roller på jourlinjer."""

    def test_primary_call_roles(self, full_config, solved_schedule):
        doc_by_id = {d.id: d for d in full_config.doctors}
        for day in range(14):
            for doc_id, days in solved_schedule.items():
                if days.get(day) == "JOUR_P":
                    doc = doc_by_id[doc_id]
                    assert doc.can_primary_call, \
                        f"Dag {day}: {doc.name} ({doc.role.value}) på primärjour men ej behörig"

    def test_backup_call_roles(self, full_config, solved_schedule):
        doc_by_id = {d.id: d for d in full_config.doctors}
        for day in range(14):
            for doc_id, days in solved_schedule.items():
                if days.get(day) == "JOUR_B":
                    doc = doc_by_id[doc_id]
                    assert doc.can_backup_call and not doc.exempt_from_call, \
                        f"Dag {day}: {doc.name} ({doc.role.value}) på bakjour"

    def test_exempt_doctors_no_call(self, full_config, solved_schedule):
        for doc in full_config.doctors:
            if doc.exempt_from_call:
                for day in range(14):
                    func = solved_schedule[doc.id].get(day, "LEDIG")
                    assert func not in ("JOUR_P", "JOUR_B"), \
                        f"{doc.name} (undantagen) har jour dag {day}"


class TestConstraint4_ATLRestAfterCall:
    """Constraint 4: 11h dygnsvila — ledig dagen efter jour."""

    def test_rest_after_call(self, solved_schedule):
        for doc_id, days in solved_schedule.items():
            for d in range(13):
                func = days.get(d, "LEDIG")
                func_next = days.get(d + 1, "LEDIG")
                if func in ("JOUR_P", "JOUR_B"):
                    assert func_next in ("LEDIG", "JOUR_P", "JOUR_B"), \
                        f"{doc_id}: jour dag {d} → {func_next} dag {d+1}"


class TestConstraint5_MaxOneCallPerWeek:
    """Constraint 5: Max 1 jour per vecka."""

    def test_max_one_call_per_week(self, solved_schedule):
        for doc_id, days in solved_schedule.items():
            for week in range(2):
                ws = week * 7
                calls = sum(1 for d in range(ws, ws + 7)
                           if days.get(d) in ("JOUR_P", "JOUR_B"))
                assert calls <= 1, f"{doc_id}: {calls} jourer vecka {week+1}"


class TestConstraint6_WeekendFunctions:
    """Constraint 6: Helger — bara jour eller ledig."""

    def test_weekend_only_call_or_free(self, solved_schedule):
        for doc_id, days in solved_schedule.items():
            for d in range(14):
                if d % 7 >= 5:
                    func = days.get(d, "LEDIG")
                    assert func in ("JOUR_P", "JOUR_B", "LEDIG"), \
                        f"{doc_id} dag {d} (helg): {func}"


class TestConstraint7_ORCapacity:
    """Constraint 7: Max läkare per site = salar × 2."""

    def test_or_capacity_per_site(self, full_config, solved_schedule):
        _, _, op_funcs = _build_functions(full_config)
        for site, op_func_id in op_funcs.items():
            max_rooms = sum(1 for r in full_config.operating_rooms if r.site == site)
            max_docs = max_rooms * 2
            for day in range(14):
                if day % 7 >= 5:
                    continue
                count = sum(1 for d, days in solved_schedule.items()
                           if days.get(day) == op_func_id)
                assert count <= max_docs, \
                    f"Dag {day}: {count} på {op_func_id} (max {max_docs})"


class TestConstraint8_MaxWorkdays:
    """Constraint 8: Max 5 arbetsdagar per vecka."""

    def test_max_five_workdays(self, solved_schedule):
        for doc_id, days in solved_schedule.items():
            for week in range(2):
                ws = week * 7
                work = sum(1 for d in range(ws, ws + 7) if days.get(d, "LEDIG") != "LEDIG")
                assert work <= 5, f"{doc_id}: {work} arbetsdagar vecka {week+1}"


class TestSoftConstraints:
    """Mjuka constraints (optimering)."""

    def test_call_fairness(self, full_config, solved_schedule):
        call_count = {}
        for doc in full_config.doctors:
            calls = sum(1 for d in range(14)
                       if solved_schedule[doc.id].get(d) in ("JOUR_P", "JOUR_B"))
            if calls > 0:
                call_count[doc.id] = calls
        if call_count:
            assert max(call_count.values()) - min(call_count.values()) <= 2

    def test_st_supervisor_matching(self, full_config, solved_schedule):
        matches = 0
        total = 0
        for doc in full_config.doctors:
            if doc.supervisor_id:
                for d in range(14):
                    func = solved_schedule[doc.id].get(d, "")
                    if func.startswith("OP_"):
                        total += 1
                        sup_func = solved_schedule.get(doc.supervisor_id, {}).get(d, "")
                        if sup_func == func:
                            matches += 1
        if total > 0:
            assert matches / total >= 0.5, f"ST-matchning: {matches}/{total}"


class TestSolverPerformance:
    """Prestandatester."""

    def test_solver_produces_result(self, solved_schedule):
        assert solved_schedule is not None
        assert len(solved_schedule) == 25


class TestGenericSolver:
    """Testa att solvern fungerar med generisk konfiguration."""

    def test_generic_config_solves(self, generic_config):
        schedule = solve_schedule(generic_config, num_weeks=1, time_limit_seconds=30)
        assert schedule is not None, "Generisk konfiguration olösbar"
        assert len(schedule) == 18

    def test_generic_functions_are_dynamic(self, generic_config):
        day_funcs, call_funcs, op_funcs = _build_functions(generic_config)
        assert "OP_Huvudsjukhuset" in op_funcs.values()
        assert any("AVD_" in f[0] for f in day_funcs)
        assert any("MOTT_" in f[0] for f in day_funcs)

    def test_generic_schedule_valid(self, generic_config):
        schedule = solve_schedule(generic_config, num_weeks=1, time_limit_seconds=15)
        assert schedule is not None
        day_funcs, call_funcs, _ = _build_functions(generic_config)
        valid = {f[0] for f in day_funcs} | {f[0] for f in call_funcs} | {"LEDIG"}
        for doc_id, days in schedule.items():
            for d, func in days.items():
                assert func in valid, f"{doc_id} dag {d}: '{func}' ej i {valid}"
