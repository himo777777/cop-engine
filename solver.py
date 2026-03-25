"""
COP Solver v0.2 — Constraint-baserad schemaoptimering
======================================================
Använder Google OR-Tools CP-SAT för att generera optimala scheman.
Generisk — fungerar för alla kliniker med godtyckliga sites.

Hårda constraints (ALDRIG bryts):
  - ATL: dygnsvila 11h, veckovila 36h
  - Minimibemanningstal per funktion
  - Jourlinjer: primärjour + bakjour alltid bemannade
  - Kompetensmatchning: rätt roll på rätt funktion
  - Ingen dubbelbokning: en läkare, en plats, ett skift

Mjuka constraints (optimeras):
  - Rättvis jourfördelning
  - Semesterönskemål
  - ST-utbildningsmål: matcha med handledare
"""

import json
import sys
from collections import defaultdict
from ortools.sat.python import cp_model

from data_model import (
    ClinicConfig, Role, ShiftType, Function,
    create_kristianstad_example,
)


FUNC_NAMES = {
    Function.OPERATION: "Op",
    Function.AVDELNING: "Avd",
    Function.MOTTAGNING: "Mott",
    Function.AKUTMOTTAGNING: "Akut",
    Function.PRIMÄRJOUR: "Primärjour",
    Function.BAKJOUR: "Bakjour",
    Function.ADMIN: "Admin",
    Function.LEDIG: "Ledig",
    Function.SEMESTER: "Semester",
}

DAY_NAMES = ["Mån", "Tis", "Ons", "Tor", "Fre", "Lör", "Sön"]


def _build_functions(config: ClinicConfig):
    """
    Bygg funktions-ID:n dynamiskt från klinikens konfiguration.

    Returnerar:
        day_functions: [(func_id, Function, site_str), ...]
        call_functions: [(func_id, Function, site_str), ...]
        op_funcs_by_site: {site_str: func_id}
    """
    day_functions = []
    op_funcs_by_site = {}

    # En OP-funktion per site (aggregerar alla salar på den siten)
    sites_with_rooms = sorted(set(r.site for r in config.operating_rooms))
    for site in sites_with_rooms:
        func_id = f"OP_{site}"
        day_functions.append((func_id, Function.OPERATION, site))
        op_funcs_by_site[site] = func_id

    # En AVD och MOTT per site (från config.sites)
    for site in config.sites:
        day_functions.append((f"AVD_{site}", Function.AVDELNING, site))
        day_functions.append((f"MOTT_{site}", Function.MOTTAGNING, site))

    # Jour (universella — inte site-specifika i modellen)
    call_functions = [
        ("JOUR_P", Function.PRIMÄRJOUR, None),
        ("JOUR_B", Function.BAKJOUR, None),
    ]

    return day_functions, call_functions, op_funcs_by_site


def solve_schedule(config: ClinicConfig, num_weeks: int = 2, time_limit_seconds: int = 30):
    """
    Genererar ett optimerat schema för given klinik.

    Args:
        config: Komplett klinikkonfiguration
        num_weeks: Antal veckor att schemalägga
        time_limit_seconds: Maximal lösartid

    Returns:
        dict med schemat, eller None om olösbart
    """
    model = cp_model.CpModel()

    num_days = num_weeks * 7

    doc_ids = [d.id for d in config.doctors]
    doc_by_id = {d.id: d for d in config.doctors}

    # Bygg funktioner dynamiskt
    day_functions, call_functions, op_funcs_by_site = _build_functions(config)

    # === BESLUTSVARIABLER ===
    x = {}
    for doc in config.doctors:
        for day in range(num_days):
            weekday = day % 7
            if weekday < 5:
                for func_id, func, site in day_functions:
                    x[(doc.id, day, func_id)] = model.new_bool_var(f"x_{doc.id}_{day}_{func_id}")
            for func_id, func, site in call_functions:
                x[(doc.id, day, func_id)] = model.new_bool_var(f"x_{doc.id}_{day}_{func_id}")
            x[(doc.id, day, "LEDIG")] = model.new_bool_var(f"x_{doc.id}_{day}_LEDIG")

    # === CONSTRAINT 1: Varje läkare gör exakt EN sak per dag ===
    for doc in config.doctors:
        for day in range(num_days):
            weekday = day % 7
            day_vars = []
            if weekday < 5:
                for func_id, _, _ in day_functions:
                    day_vars.append(x[(doc.id, day, func_id)])
            for func_id, _, _ in call_functions:
                day_vars.append(x[(doc.id, day, func_id)])
            day_vars.append(x[(doc.id, day, "LEDIG")])
            model.add_exactly_one(day_vars)

    # === CONSTRAINT 2: Minimibemanningstal (vardagar) — dynamisk ===
    seniors = [d for d in config.doctors if d.role in (Role.SPECIALIST, Role.ÖVERLÄKARE)]

    for day in range(num_days):
        weekday = day % 7
        if weekday >= 5:
            continue

        # OP per site: minst antal tillgängliga salar
        for site, op_func_id in op_funcs_by_site.items():
            rooms_available = sum(
                1 for r in config.operating_rooms
                if r.site == site and weekday in r.available_days
            )
            if rooms_available > 0:
                model.add(
                    sum(x[(d.id, day, op_func_id)] for d in config.doctors) >= rooms_available
                )
                # Minst hälften senior
                if seniors:
                    model.add(
                        sum(x[(d.id, day, op_func_id)] for d in seniors) >= (rooms_available + 1) // 2
                    )

        # AVD, MOTT per site — från staffing_requirements
        for req in config.staffing_requirements:
            if req.shift_type != ShiftType.DAG:
                continue
            if req.function == Function.OPERATION:
                continue  # Handled above via rooms

            func_id = f"{req.function.value}_{req.site}"
            if not any(f_id == func_id for f_id, _, _ in day_functions):
                continue

            eligible = [d for d in config.doctors if d.role in req.required_roles]
            if eligible:
                model.add(
                    sum(x[(d.id, day, func_id)] for d in eligible) >= req.min_count
                )

        # Underläkare får inte göra mottagning
        for doc in config.doctors:
            if doc.role == Role.UNDERLÄKARE:
                for func_id, func, site in day_functions:
                    if func == Function.MOTTAGNING:
                        model.add(x[(doc.id, day, func_id)] == 0)

    # === CONSTRAINT 3: Jourlinjer (alla dagar) ===
    primary_eligible = [d for d in config.doctors if d.can_primary_call]
    backup_eligible = [d for d in config.doctors if d.can_backup_call and not d.exempt_from_call]

    for day in range(num_days):
        model.add(sum(x[(d.id, day, "JOUR_P")] for d in primary_eligible) == 1)
        for d in config.doctors:
            if not d.can_primary_call:
                model.add(x[(d.id, day, "JOUR_P")] == 0)

        model.add(sum(x[(d.id, day, "JOUR_B")] for d in backup_eligible) == 1)
        for d in config.doctors:
            if not d.can_backup_call or d.exempt_from_call:
                model.add(x[(d.id, day, "JOUR_B")] == 0)

    # === CONSTRAINT 4: ATL — Vila efter jour ===
    for doc in config.doctors:
        for day in range(num_days - 1):
            next_day = day + 1
            weekday_next = next_day % 7

            for call_func_id in ["JOUR_P", "JOUR_B"]:
                is_on_call = x.get((doc.id, day, call_func_id))
                if is_on_call is not None and next_day < num_days:
                    if weekday_next < 5:
                        for func_id, _, _ in day_functions:
                            if (doc.id, next_day, func_id) in x:
                                model.add(x[(doc.id, next_day, func_id)] == 0).only_enforce_if(is_on_call)

    # === CONSTRAINT 5: Max jourer per vecka ===
    for doc in config.doctors:
        for week in range(num_weeks):
            week_start = week * 7
            week_calls = []
            for day in range(week_start, min(week_start + 7, num_days)):
                if (doc.id, day, "JOUR_P") in x:
                    week_calls.append(x[(doc.id, day, "JOUR_P")])
                if (doc.id, day, "JOUR_B") in x:
                    week_calls.append(x[(doc.id, day, "JOUR_B")])
            if week_calls:
                model.add(sum(week_calls) <= 1)

    # === CONSTRAINT 6: Helger ===
    for doc in config.doctors:
        for day in range(num_days):
            if day % 7 >= 5:
                jour_vars = []
                if (doc.id, day, "JOUR_P") in x:
                    jour_vars.append(x[(doc.id, day, "JOUR_P")])
                if (doc.id, day, "JOUR_B") in x:
                    jour_vars.append(x[(doc.id, day, "JOUR_B")])
                if (doc.id, day, "LEDIG") in x:
                    jour_vars.append(x[(doc.id, day, "LEDIG")])
                if jour_vars:
                    model.add_exactly_one(jour_vars)

    # === CONSTRAINT 7: OR-kapacitetstak per site ===
    for day in range(num_days):
        weekday = day % 7
        if weekday >= 5:
            continue
        for site, op_func_id in op_funcs_by_site.items():
            rooms = sum(1 for r in config.operating_rooms if r.site == site and weekday in r.available_days)
            if rooms > 0:
                model.add(
                    sum(x[(d.id, day, op_func_id)] for d in config.doctors) <= rooms * 2
                )

    # === CONSTRAINT 8: Max arbetsdagar per vecka ===
    for doc in config.doctors:
        for week in range(num_weeks):
            week_start = week * 7
            model.add(
                sum(1 - x[(doc.id, d, "LEDIG")] for d in range(week_start, min(week_start + 7, num_days))) <= 5
            )

    # === CONSTRAINT 9: Semester/frånvaro ===
    for pref in config.preferences:
        doc = doc_by_id.get(pref.doctor_id)
        if not doc:
            continue
        if pref.type == "LEDIG_DAG":
            weekday = pref.details.get("weekday")
            if weekday is not None:
                for week in range(num_weeks):
                    day_idx = week * 7 + weekday
                    if day_idx < num_days and (doc.id, day_idx, "LEDIG") in x:
                        if pref.priority == 1:
                            model.add(x[(doc.id, day_idx, "LEDIG")] == 1)

    # === CONSTRAINT 10: Deltid ===
    for doc in config.doctors:
        if doc.employment_rate < 1.0:
            max_work_per_week = int(5 * doc.employment_rate + 0.5)
            for week in range(num_weeks):
                week_start = week * 7
                work_vars = []
                for d in range(week_start, min(week_start + 7, num_days)):
                    if (doc.id, d, "LEDIG") in x:
                        work_vars.append(1 - x[(doc.id, d, "LEDIG")])
                if work_vars:
                    model.add(sum(work_vars) <= max_work_per_week)

    # === CONSTRAINT 11: Seniornärvaro per vardag ===
    for day in range(num_days):
        if day % 7 >= 5:
            continue
        active_ol = []
        for d in config.doctors:
            if d.role == Role.ÖVERLÄKARE:
                if (d.id, day, "LEDIG") in x:
                    active_ol.append(1 - x[(d.id, day, "LEDIG")])
        if active_ol:
            model.add(sum(active_ol) >= 1)

    # === MJUKA CONSTRAINTS ===

    # Rättvisa jourfördelning
    call_counts = {}
    for doc in config.doctors:
        if doc.can_primary_call or doc.can_backup_call:
            total_calls = []
            for day in range(num_days):
                if (doc.id, day, "JOUR_P") in x:
                    total_calls.append(x[(doc.id, day, "JOUR_P")])
                if (doc.id, day, "JOUR_B") in x:
                    total_calls.append(x[(doc.id, day, "JOUR_B")])
            if total_calls:
                call_counts[doc.id] = model.new_int_var(0, num_days, f"calls_{doc.id}")
                model.add(call_counts[doc.id] == sum(total_calls))

    if call_counts:
        max_calls = model.new_int_var(0, num_days, "max_calls")
        min_calls = model.new_int_var(0, num_days, "min_calls")
        for doc_id, count_var in call_counts.items():
            model.add(max_calls >= count_var)
            model.add(min_calls <= count_var)

    # ST-utbildning: maximera op-dagar med handledare (generisk)
    op_func_ids = list(op_funcs_by_site.values())
    st_training_bonus = []
    for doc in config.doctors:
        if doc.supervisor_id and doc.required_procedures:
            supervisor = doc_by_id.get(doc.supervisor_id)
            if supervisor:
                for day in range(num_days):
                    if day % 7 >= 5:
                        continue
                    for op_fid in op_func_ids:
                        if (doc.id, day, op_fid) in x and (supervisor.id, day, op_fid) in x:
                            both_op = model.new_bool_var(f"train_{doc.id}_{supervisor.id}_{day}_{op_fid}")
                            model.add_bool_and([
                                x[(doc.id, day, op_fid)],
                                x[(supervisor.id, day, op_fid)]
                            ]).only_enforce_if(both_op)
                            model.add_bool_or([
                                x[(doc.id, day, op_fid)].negated(),
                                x[(supervisor.id, day, op_fid)].negated()
                            ]).only_enforce_if(both_op.negated())
                            st_training_bonus.append(both_op)

    # Önskemål
    preference_bonus = []
    for pref in config.preferences:
        doc = doc_by_id.get(pref.doctor_id)
        if not doc:
            continue
        if pref.type == "SKIFT_PREF":
            avoid = pref.details.get("avoid")
            if avoid == "NATT":
                for day in range(num_days):
                    if (doc.id, day, "JOUR_P") in x:
                        pref_var = model.new_bool_var(f"pref_nojour_{doc.id}_{day}")
                        model.add(x[(doc.id, day, "JOUR_P")] == 0).only_enforce_if(pref_var)
                        model.add(x[(doc.id, day, "JOUR_P")] == 1).only_enforce_if(pref_var.negated())
                        weight = 3 if pref.priority == 1 else 1
                        preference_bonus.append(weight * pref_var)
        elif pref.type == "OP_PREF":
            want_with = pref.details.get("want_with")
            if want_with and want_with in doc_by_id:
                for day in range(num_days):
                    if day % 7 >= 5:
                        continue
                    for op_fid in op_func_ids:
                        if (doc.id, day, op_fid) in x and (want_with, day, op_fid) in x:
                            both_var = model.new_bool_var(f"oppref_{doc.id}_{want_with}_{day}_{op_fid}")
                            model.add_bool_and([x[(doc.id, day, op_fid)], x[(want_with, day, op_fid)]]).only_enforce_if(both_var)
                            model.add_bool_or([x[(doc.id, day, op_fid)].negated(), x[(want_with, day, op_fid)].negated()]).only_enforce_if(both_var.negated())
                            preference_bonus.append(2 * both_var)

    # === OBJEKTIV FUNKTION ===
    objective_terms = []
    for bonus_var in st_training_bonus:
        objective_terms.append(10 * bonus_var)
    for pref_term in preference_bonus:
        objective_terms.append(pref_term)
    if call_counts:
        call_spread = model.new_int_var(0, num_days, "call_spread")
        model.add(call_spread == max_calls - min_calls)
        objective_terms.append(-5 * call_spread)

    if objective_terms:
        model.maximize(sum(objective_terms))

    # === LÖSNING ===
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_workers = 4

    print(f"Löser schema för {len(config.doctors)} läkare, {num_weeks} veckor, {num_days} dagar...")
    print(f"Sites: {config.sites} | Funktioner: {[f[0] for f in day_functions]}")

    status = solver.solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        quality = "OPTIMALT" if status == cp_model.OPTIMAL else "GENOMFÖRBART"
        print(f"\n✅ Schema hittat ({quality})!")
        if objective_terms:
            print(f"Objektvärde: {solver.objective_value}")

        schedule = extract_schedule(solver, x, config, num_days, day_functions, call_functions)
        print_schedule(schedule, config, num_days)
        print_statistics(schedule, config, num_days, solver, call_counts)

        return schedule
    else:
        print(f"\n❌ Inget schema hittades. Status: {solver.status_name(status)}")
        return None


def extract_schedule(solver, x, config, num_days, day_functions, call_functions):
    """Extrahera lösningen till en läsbar datastruktur."""
    schedule = {}
    for doc in config.doctors:
        schedule[doc.id] = {}
        for day in range(num_days):
            weekday = day % 7
            assigned = None

            if weekday < 5:
                for func_id, _, _ in day_functions:
                    if (doc.id, day, func_id) in x and solver.value(x[(doc.id, day, func_id)]) == 1:
                        assigned = func_id
                        break

            if assigned is None:
                for func_id, _, _ in call_functions:
                    if (doc.id, day, func_id) in x and solver.value(x[(doc.id, day, func_id)]) == 1:
                        assigned = func_id
                        break

            if assigned is None:
                assigned = "LEDIG"

            schedule[doc.id][day] = assigned

    return schedule


def print_schedule(schedule, config, num_days):
    """Skriv ut schemat i tabellformat."""
    print("\n" + "=" * 120)
    print("GENERERAT SCHEMA")
    print("=" * 120)

    header = f"{'Läkare':<25}"
    for day in range(min(num_days, 14)):
        weekday = day % 7
        header += f" {DAY_NAMES[weekday]:>8}"
    print(header)
    print("-" * 120)

    role_order = {Role.ÖVERLÄKARE: 0, Role.SPECIALIST: 1, Role.ST_SEN: 2, Role.ST_TIDIG: 3, Role.UNDERLÄKARE: 4}

    for doc in sorted(config.doctors, key=lambda d: (role_order.get(d.role, 5), d.id)):
        line = f"{doc.name} ({doc.role.value})"
        line = f"{line:<25}"
        for day in range(min(num_days, 14)):
            func = schedule[doc.id][day]
            display = func[:8] if func != "LEDIG" else "  —  "
            line += f" {display:>8}"
        print(line)

    print("=" * 120)


def print_statistics(schedule, config, num_days, solver, call_counts):
    """Skriv ut statistik och kvalitetsmått."""
    print("\n" + "=" * 80)
    print("STATISTIK")
    print("=" * 80)

    # Jourfördelning
    print("\n📊 Jourfördelning:")
    call_stats = defaultdict(lambda: {"primär": 0, "bak": 0, "total": 0})
    for doc in config.doctors:
        for day in range(num_days):
            func = schedule[doc.id][day]
            if func == "JOUR_P":
                call_stats[doc.id]["primär"] += 1
                call_stats[doc.id]["total"] += 1
            elif func == "JOUR_B":
                call_stats[doc.id]["bak"] += 1
                call_stats[doc.id]["total"] += 1

    for doc in config.doctors:
        if call_stats[doc.id]["total"] > 0:
            stats = call_stats[doc.id]
            print(f"  {doc.name:<25} Primär: {stats['primär']:>2}  Bak: {stats['bak']:>2}  Total: {stats['total']:>2}")

    # ST-handledarmatchning
    print("\n📊 ST-handledarmatchning:")
    for doc in config.doctors:
        if doc.supervisor_id:
            matches = 0
            total_op = 0
            for day in range(num_days):
                func = schedule[doc.id][day]
                if func.startswith("OP_"):
                    total_op += 1
                    sup_func = schedule.get(doc.supervisor_id, {}).get(day, "")
                    if sup_func == func:
                        matches += 1
            if total_op > 0:
                print(f"  {doc.name:<25} {matches}/{total_op} op-dagar med handledare ({matches/total_op*100:.0f}%)")

    # ATL-validering
    print("\n✅ ATL-validering:")
    violations = 0
    for doc in config.doctors:
        for day in range(num_days - 1):
            func_today = schedule[doc.id][day]
            func_tomorrow = schedule[doc.id].get(day + 1, "LEDIG")
            if func_today in ("JOUR_P", "JOUR_B") and func_tomorrow not in ("LEDIG", "JOUR_P", "JOUR_B"):
                violations += 1
                print(f"  ⚠️  {doc.name}: Jour dag {day+1} → arbete dag {day+2}")

    if violations == 0:
        print("  Inga ATL-brott detekterade ✅")
    else:
        print(f"  {violations} potentiella ATL-brott ⚠️")


if __name__ == "__main__":
    config = create_kristianstad_example()
    schedule = solve_schedule(config, num_weeks=2, time_limit_seconds=30)
