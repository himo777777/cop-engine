"""
COP Engine — Tester för Solver
================================
Verifierar alla hårda och mjuka constraints.
Generiska tester — fungerar med vilken klinikkonfiguration som helst.
"""

import pytest
import copy
from collections import defaultdict
from data_model import (
    Role, create_kristianstad_example, create_generic_example, is_jour,
    Preference, ClinicConfig, OBRates,
)
from solver import solve_schedule, _build_functions, expand_to_granular, solve_rolling, _ob_cost


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
        day_funcs, call_funcs, _, _ = _build_functions(full_config)
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
        _, _, op_funcs, _ = _build_functions(full_config)
        for day in range(14):
            if day % 7 >= 5:
                continue
            for site, op_func_id in op_funcs.items():
                count = sum(1 for d, days in solved_schedule.items() if days.get(day) == op_func_id)
                assert count >= 1, f"Dag {day}: ingen {op_func_id}"

    def test_weekday_avdelning(self, full_config, solved_schedule):
        day_funcs, _, _, _ = _build_functions(full_config)
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
        _, _, op_funcs, _ = _build_functions(full_config)
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
        day_funcs, call_funcs, op_funcs, _ = _build_functions(generic_config)
        assert "OP_Huvudsjukhuset" in op_funcs.values()
        assert any("AVD_" in f[0] for f in day_funcs)
        assert any("MOTT_" in f[0] for f in day_funcs)

    def test_generic_schedule_valid(self, generic_config):
        schedule = solve_schedule(generic_config, num_weeks=1, time_limit_seconds=15)
        assert schedule is not None
        day_funcs, call_funcs, _, _ = _build_functions(generic_config)
        valid = {f[0] for f in day_funcs} | {f[0] for f in call_funcs} | {"LEDIG"}
        for doc_id, days in schedule.items():
            for d, func in days.items():
                assert func in valid, f"{doc_id} dag {d}: '{func}' ej i {valid}"


class TestExpandToGranular:
    """Tester för expand_to_granular() — mappar JOUR_P/JOUR_B till granulära jourtyper."""

    def test_weekday_jour_p_becomes_kvall(self, solved_schedule):
        """Vardag JOUR_P ska expanderas till JOUR_P_KVÄLL."""
        num_days = max(max(days.keys()) for days in solved_schedule.values()) + 1
        granular = expand_to_granular(solved_schedule, num_days)
        for doc_id, days in granular.items():
            for day, func in days.items():
                if day % 7 < 5 and solved_schedule[doc_id][day] == "JOUR_P":
                    assert func == "JOUR_P_KVÄLL", \
                        f"{doc_id} dag {day}: vardag JOUR_P ska bli JOUR_P_KVÄLL, fick {func}"

    def test_weekend_jour_p_becomes_helg(self, solved_schedule):
        """Helg JOUR_P ska expanderas till JOUR_P_HELGDAG (lör) / JOUR_P_HELGNATT (sön)."""
        num_days = max(max(days.keys()) for days in solved_schedule.values()) + 1
        granular = expand_to_granular(solved_schedule, num_days)
        for doc_id, days in granular.items():
            for day, func in days.items():
                if solved_schedule[doc_id][day] == "JOUR_P":
                    if day % 7 == 5:  # Lördag
                        assert func == "JOUR_P_HELGDAG", \
                            f"{doc_id} dag {day}: lör JOUR_P → JOUR_P_HELGDAG, fick {func}"
                    elif day % 7 == 6:  # Söndag
                        assert func == "JOUR_P_HELGNATT", \
                            f"{doc_id} dag {day}: sön JOUR_P → JOUR_P_HELGNATT, fick {func}"

    def test_weekend_jour_b_becomes_helg(self, solved_schedule):
        """Helg JOUR_B ska expanderas till JOUR_B_HELGDAG (lör) / JOUR_B_HELGNATT (sön)."""
        num_days = max(max(days.keys()) for days in solved_schedule.values()) + 1
        granular = expand_to_granular(solved_schedule, num_days)
        for doc_id, days in granular.items():
            for day, func in days.items():
                if solved_schedule[doc_id][day] == "JOUR_B":
                    if day % 7 == 5:
                        assert func == "JOUR_B_HELGDAG", \
                            f"{doc_id} dag {day}: lör JOUR_B → JOUR_B_HELGDAG, fick {func}"
                    elif day % 7 == 6:
                        assert func == "JOUR_B_HELGNATT", \
                            f"{doc_id} dag {day}: sön JOUR_B → JOUR_B_HELGNATT, fick {func}"

    def test_weekday_jour_b_unchanged(self, solved_schedule):
        """Vardag JOUR_B ska vara oförändrad (bakjour hemifrån täcker kväll+natt)."""
        num_days = max(max(days.keys()) for days in solved_schedule.values()) + 1
        granular = expand_to_granular(solved_schedule, num_days)
        for doc_id, days in granular.items():
            for day, func in days.items():
                if day % 7 < 5 and solved_schedule[doc_id][day] == "JOUR_B":
                    assert func == "JOUR_B", \
                        f"{doc_id} dag {day}: vardag JOUR_B ska vara oförändrad, fick {func}"

    def test_non_jour_unchanged(self, solved_schedule):
        """Icke-jour funktioner ska vara oförändrade."""
        num_days = max(max(days.keys()) for days in solved_schedule.values()) + 1
        granular = expand_to_granular(solved_schedule, num_days)
        for doc_id, days in granular.items():
            for day, func in days.items():
                raw_func = solved_schedule[doc_id][day]
                if raw_func not in ("JOUR_P", "JOUR_B"):
                    assert func == raw_func, \
                        f"{doc_id} dag {day}: {raw_func} ska vara oförändrad, fick {func}"

    def test_granular_types_are_jour(self):
        """Alla granulära jourtyper ska identifieras av is_jour()."""
        granular_types = [
            "JOUR_P_KVÄLL", "JOUR_P_NATT",
            "JOUR_P_HELGDAG", "JOUR_P_HELGNATT",
            "JOUR_B_HELGDAG", "JOUR_B_HELGNATT",
            "JOUR_P", "JOUR_B",
        ]
        for jt in granular_types:
            assert is_jour(jt), f"is_jour('{jt}') ska vara True"

        non_jour = ["LEDIG", "OP_H", "AVD_CSK", "MOTT_H"]
        for nj in non_jour:
            assert not is_jour(nj), f"is_jour('{nj}') ska vara False"


class TestWeekendCompensation:
    """Tester för helgkompensation (CONSTRAINT 12)."""

    def test_weekend_comp_primary(self, solved_schedule):
        """Läkare med primärjour lör/sön → minst 1 LEDIG vardag veckan efter."""
        num_days = max(max(days.keys()) for days in solved_schedule.values()) + 1
        num_weeks = num_days // 7

        for doc_id, days in solved_schedule.items():
            for week in range(num_weeks - 1):
                sat = week * 7 + 5
                sun = week * 7 + 6
                weekend_primary = sum(
                    1 for d in [sat, sun]
                    if d < num_days and days.get(d) == "JOUR_P"
                )
                if weekend_primary > 0:
                    next_week_start = (week + 1) * 7
                    next_week_ledig = sum(
                        1 for d in range(next_week_start, min(next_week_start + 5, num_days))
                        if days.get(d) == "LEDIG"
                    )
                    assert next_week_ledig >= weekend_primary, \
                        f"{doc_id} vecka {week}: {weekend_primary} helg-primärjour men bara " \
                        f"{next_week_ledig} LEDIG vardagar veckan efter (kräver >= {weekend_primary})"

    def test_weekend_comp_both_days(self, solved_schedule):
        """Läkare med primärjour BÅDE lör+sön → minst 2 LEDIG vardagar veckan efter."""
        num_days = max(max(days.keys()) for days in solved_schedule.values()) + 1
        num_weeks = num_days // 7

        for doc_id, days in solved_schedule.items():
            for week in range(num_weeks - 1):
                sat = week * 7 + 5
                sun = week * 7 + 6
                has_sat = sat < num_days and days.get(sat) == "JOUR_P"
                has_sun = sun < num_days and days.get(sun) == "JOUR_P"
                if has_sat and has_sun:
                    next_week_start = (week + 1) * 7
                    next_week_ledig = sum(
                        1 for d in range(next_week_start, min(next_week_start + 5, num_days))
                        if days.get(d) == "LEDIG"
                    )
                    assert next_week_ledig >= 2, \
                        f"{doc_id} vecka {week}: jour lör+sön men bara {next_week_ledig} LEDIG vardagar"

    def test_weekend_comp_rule_exists(self, full_config):
        """Constraint-regel 'weekend_compensation' ska finnas i konfigurationen."""
        rule_ids = [r.id for r in full_config.constraint_rules]
        assert "weekend_compensation" in rule_ids, \
            "Regeln 'weekend_compensation' saknas i constraint_rules"


class TestSemesterBlock:
    """Tester för semesterblock (CONSTRAINT 13+14)."""

    def test_semester_block_hard(self, full_config):
        """Läkare med SEMESTER_BLOCK prioritet 1 ska vara LEDIG alla dagar i blocket."""
        schedule = solve_schedule(full_config, num_weeks=2, time_limit_seconds=30)
        assert schedule is not None

        for pref in full_config.preferences:
            if pref.type == "SEMESTER_BLOCK" and pref.priority == 1:
                sw = pref.details.get("start_week", 0)
                ew = pref.details.get("end_week", sw)
                for week in range(sw, ew + 1):
                    for d in range(week * 7, min((week + 1) * 7, 14)):
                        func = schedule[pref.doctor_id].get(d)
                        assert func == "LEDIG", \
                            f"{pref.doctor_id} dag {d} (semesterblock): {func} (ska vara LEDIG)"

    def test_semester_block_no_call(self, full_config):
        """Semesterläkare (prio 1) ska inte ha jour under semester."""
        schedule = solve_schedule(full_config, num_weeks=2, time_limit_seconds=30)
        assert schedule is not None

        for pref in full_config.preferences:
            if pref.type == "SEMESTER_BLOCK" and pref.priority == 1:
                sw = pref.details.get("start_week", 0)
                ew = pref.details.get("end_week", sw)
                for week in range(sw, ew + 1):
                    for d in range(week * 7, min((week + 1) * 7, 14)):
                        func = schedule[pref.doctor_id].get(d)
                        assert not is_jour(func), \
                            f"{pref.doctor_id} dag {d}: har jour {func} under semester"

    def test_max_concurrent_vacation(self, full_config):
        """Max 20% av läkarna ska kunna vara på semester samtidigt."""
        import math
        max_vac = math.ceil(len(full_config.doctors) * 0.2)

        # Skapa config med fler semestrar för att testa gränsen
        config = copy.deepcopy(full_config)
        # Lägg till semesterblock vecka 0 för 7 läkare (> 20% av 25 = 5)
        extra_semesters = []
        eligible = [d for d in config.doctors if d.id not in ("OL1",)][:7]
        for doc in eligible:
            extra_semesters.append(
                Preference(doc.id, "SEMESTER_BLOCK", 2, {"start_week": 0, "end_week": 0})
            )
        config.preferences = [p for p in config.preferences if p.type != "SEMESTER_BLOCK"]
        config.preferences.extend(extra_semesters)

        schedule = solve_schedule(config, num_weeks=2, time_limit_seconds=30)
        assert schedule is not None

        # Kontrollera att max max_vac är lediga per dag (av de som har semester-pref)
        semester_doc_ids = {p.doctor_id for p in extra_semesters}
        for d in range(7):  # Vecka 0
            ledig_count = sum(
                1 for did in semester_doc_ids
                if schedule[did].get(d) == "LEDIG"
            )
            assert ledig_count <= max_vac, \
                f"Dag {d}: {ledig_count} semestrar (max {max_vac})"


class TestRollingSchedule:
    """Tester för rullande schemauppdatering (solve_rolling)."""

    def test_rolling_basic(self, full_config):
        """Generera 4 veckor, rulla framåt 1 — vecka 2-4 ska bevaras."""
        original = solve_schedule(full_config, num_weeks=4, time_limit_seconds=60)
        assert original is not None

        rolled = solve_rolling(
            full_config,
            existing_schedule=original,
            locked_weeks=3,
            new_weeks=1,
            time_limit_seconds=60,
        )
        assert rolled is not None

        # Vecka 1-3 av original (dag 0-20) ska vara identiska med dag 0-20 i rolled
        for doc_id in original:
            for day in range(21):  # 3 veckor = 21 dagar
                assert rolled[doc_id][day] == original[doc_id][day], \
                    f"{doc_id} dag {day}: original={original[doc_id][day]}, rolled={rolled[doc_id][day]}"

    def test_rolling_locked_respected(self, full_config):
        """Låsta tilldelningar ska bevaras exakt."""
        original = solve_schedule(full_config, num_weeks=2, time_limit_seconds=30)
        assert original is not None

        locked = {}
        for doc_id, days in original.items():
            locked[doc_id] = {d: f for d, f in days.items() if d < 7}  # Lås vecka 1

        result = solve_schedule(
            full_config,
            num_weeks=2,
            time_limit_seconds=30,
            locked_assignments=locked,
        )
        assert result is not None

        for doc_id, day_funcs in locked.items():
            for day, func in day_funcs.items():
                assert result[doc_id][day] == func, \
                    f"{doc_id} dag {day}: låst={func}, fick={result[doc_id][day]}"

    def test_rolling_atl_across_boundary(self, full_config):
        """ATL-regler ska respekteras över gräns låst/ny vecka — inga dagfunktioner efter jour."""
        original = solve_schedule(full_config, num_weeks=3, time_limit_seconds=30)
        assert original is not None

        rolled = solve_rolling(
            full_config,
            existing_schedule=original,
            locked_weeks=2,
            new_weeks=1,
            time_limit_seconds=30,
        )
        assert rolled is not None

        # Kolla att jour dag 13 (sista låsta dag) → ingen dagfunktion dag 14
        # (CONSTRAINT 4 blockerar dagfunktioner, inte jour/LEDIG)
        day_func_ids = {f[0] for f in _build_functions(full_config)[0]}
        for doc_id, days in rolled.items():
            if is_jour(days.get(13, "LEDIG")):
                if 14 % 7 < 5:  # Om dag 14 är vardag
                    func_14 = days.get(14, "LEDIG")
                    assert func_14 not in day_func_ids, \
                        f"{doc_id}: jour dag 13 men dagfunktion {func_14} dag 14"


class TestOBCost:
    """Tester för OB-kostnadsoptimering."""

    def test_ob_cost_calculation(self):
        """_ob_cost() ska returnera rätt värden per dag och skifttyp."""
        rates = OBRates()

        # Vardag JOUR_P: kväll (1.0) + natt (1.5) = 2.5
        assert _ob_cost(0, "JOUR_P", rates) == 2.5  # Måndag
        assert _ob_cost(3, "JOUR_B", rates) == 2.5   # Torsdag

        # Lördag (dag 5): 1.2
        assert _ob_cost(5, "JOUR_P", rates) == 1.2
        assert _ob_cost(5, "JOUR_B", rates) == 1.2

        # Söndag (dag 6): 1.5
        assert _ob_cost(6, "JOUR_P", rates) == 1.5
        assert _ob_cost(6, "JOUR_B", rates) == 1.5

        # Dagfunktioner: 0
        assert _ob_cost(0, "OP_H", rates) == 0.0
        assert _ob_cost(0, "LEDIG", rates) == 0.0

    def test_ob_fairness(self, full_config):
        """OB-kostnader ska vara rimligt jämnt fördelade."""
        schedule = solve_schedule(full_config, num_weeks=2, time_limit_seconds=30)
        assert schedule is not None

        ob_per_doc = {}
        for doc in full_config.doctors:
            total = 0.0
            for day in range(14):
                func = schedule[doc.id].get(day, "LEDIG")
                total += _ob_cost(day % 7, func, full_config.ob_rates)
            if total > 0:
                ob_per_doc[doc.id] = total

        if ob_per_doc:
            max_ob = max(ob_per_doc.values())
            min_ob = min(ob_per_doc.values())
            # Spridningen ska vara rimlig (inte extremt ojämn)
            assert max_ob - min_ob <= 15, \
                f"OB-spridning för stor: max={max_ob}, min={min_ob}, diff={max_ob-min_ob}"
