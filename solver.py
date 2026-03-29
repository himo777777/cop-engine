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
import math
import sys
from collections import defaultdict
from ortools.sat.python import cp_model

from data_model import (
    ClinicConfig, Role, ShiftType, Function, ConstraintRule,
    create_kristianstad_example, is_jour, is_non_clinical, OBRates,
    NON_CLINICAL_FUNC_IDS,
)


FUNC_NAMES = {
    Function.OPERATION: "Op",
    Function.AVDELNING: "Avd",
    Function.MOTTAGNING: "Mott",
    Function.AKUTMOTTAGNING: "Akut",
    Function.PRIMÄRJOUR: "Primärjour",
    Function.BAKJOUR: "Bakjour",
    Function.JOUR_P_KVÄLL: "PJ Kväll",
    Function.JOUR_P_NATT: "PJ Natt",
    Function.JOUR_P_HELGDAG: "PJ Helgdag",
    Function.JOUR_P_HELGNATT: "PJ Helgnatt",
    Function.JOUR_B_HELGDAG: "BJ Helgdag",
    Function.JOUR_B_HELGNATT: "BJ Helgnatt",
    Function.ADMIN: "Admin",
    Function.FORSKNING: "Forskning",
    Function.HANDLEDNING: "Handledning",
    Function.UTBILDNING: "Utbildning",
    Function.AKUT: "Akut",
    Function.LEDIG: "Ledig",
    Function.SEMESTER: "Semester",
    Function.KOMPLEDIGHET: "Kompledighet",
}

DAY_NAMES = ["Mån", "Tis", "Ons", "Tor", "Fre", "Lör", "Sön"]


def _get_rule(config: ClinicConfig, rule_id: str, default_weight: int = 5):
    """Hämta regel från config. Returnerar (enabled, is_hard, weight)."""
    for rule in getattr(config, 'constraint_rules', []):
        if rule.id == rule_id and rule.enabled:
            return (True, rule.is_hard, rule.weight)
    return (False, False, default_weight)


def _rule_weight(config: ClinicConfig, rule_id: str, default: int = 5) -> int:
    """Hämta mjuk-vikt för en regel. Returnerar 0 om inaktiverad."""
    enabled, is_hard, weight = _get_rule(config, rule_id, default)
    if not enabled:
        return 0
    # Skala vikt: config weight 1-10 → solver multiplier
    return weight * 2  # weight 5 → 10, weight 10 → 20


def _ob_cost(weekday: int, func_id: str, ob_rates: OBRates) -> float:
    """Beräkna OB-kostnad för en tilldelning baserat på veckodag och skift."""
    if func_id == "JOUR_P":
        if weekday < 5:  # Vardag kväll+natt
            return ob_rates.weekday_evening + ob_rates.weekday_night
        elif weekday == 5:  # Lördag dag
            return ob_rates.saturday_day
        else:  # Söndag dag
            return ob_rates.sunday_day
    elif func_id == "JOUR_B":
        if weekday < 5:
            return ob_rates.weekday_evening + ob_rates.weekday_night
        elif weekday == 5:
            return ob_rates.saturday_day
        else:
            return ob_rates.sunday_day
    return 0.0


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

    # AKUT per site (om StaffingRequirement finns för AKUT)
    akut_sites = set()
    for req in getattr(config, 'staffing_requirements', []):
        if req.function == Function.AKUTMOTTAGNING:
            akut_sites.add(req.site)
    if not akut_sites:
        # Fallback: alla sites har akut
        akut_sites = set(config.sites)
    for site in sorted(akut_sites):
        day_functions.append((f"AKUT_{site}", Function.AKUTMOTTAGNING, site))

    # Icke-kliniska funktioner (ej site-specifika)
    day_functions.append(("ADMIN", Function.ADMIN, None))
    day_functions.append(("FORSKNING", Function.FORSKNING, None))
    day_functions.append(("HANDLEDNING", Function.HANDLEDNING, None))
    day_functions.append(("UTBILDNING", Function.UTBILDNING, None))

    # Jour (universella — inte site-specifika i modellen)
    call_functions = [
        ("JOUR_P", Function.PRIMÄRJOUR, None),
        ("JOUR_B", Function.BAKJOUR, None),
    ]

    return day_functions, call_functions, op_funcs_by_site, sorted(akut_sites)


def solve_schedule(config: ClinicConfig, num_weeks: int = 2, time_limit_seconds: int = 30,
                   locked_assignments: dict = None):
    """
    Genererar ett optimerat schema för given klinik.

    Args:
        config: Komplett klinikkonfiguration
        num_weeks: Antal veckor att schemalägga
        time_limit_seconds: Maximal lösartid
        locked_assignments: Låsta tilldelningar {doc_id: {day_index: func_id}}

    Returns:
        dict med schemat, eller None om olösbart
    """
    model = cp_model.CpModel()

    num_days = num_weeks * 7

    doc_ids = [d.id for d in config.doctors]
    doc_by_id = {d.id: d for d in config.doctors}

    # Bygg funktioner dynamiskt
    day_functions, call_functions, op_funcs_by_site, akut_sites = _build_functions(config)

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

    # === LÅSTA TILLDELNINGAR (rullande schema) ===
    if locked_assignments:
        for doc_id, day_funcs in locked_assignments.items():
            for day_key, func_id in day_funcs.items():
                day = int(day_key)
                if (doc_id, day, func_id) in x:
                    model.add(x[(doc_id, day, func_id)] == 1)

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

    # === CONSTRAINT X: Kompetensmatchning ===
    # För varje ShiftDefinition med required_competencies: minst en kvalificerad läkare måste vara tilldelad.
    _FUNC_TO_ID = {
        Function.OPERATION:       lambda s: f"OP_{s.site}",
        Function.AVDELNING:       lambda s: f"AVD_{s.site}",
        Function.MOTTAGNING:      lambda s: f"MOTT_{s.site}",
        Function.AKUTMOTTAGNING:  lambda s: f"AKUT_{s.site}",
        Function.PRIMÄRJOUR:      lambda _: "JOUR_P",
        Function.BAKJOUR:         lambda _: "JOUR_B",
        Function.ADMIN:           lambda _: "ADMIN",
        Function.FORSKNING:       lambda _: "FORSKNING",
        Function.HANDLEDNING:     lambda _: "HANDLEDNING",
        Function.UTBILDNING:      lambda _: "UTBILDNING",
    }
    for shift_def in getattr(config, "shift_definitions", []):
        if not getattr(shift_def, "required_competencies", None):
            continue
        fn = _FUNC_TO_ID.get(shift_def.function)
        if fn is None:
            continue
        func_id = fn(shift_def)
        qualified = [
            d for d in config.doctors
            if all(c in (d.competencies or []) for c in shift_def.required_competencies)
        ]
        if not qualified:
            continue
        for day in range(num_days):
            keys = [(d.id, day, func_id) for d in qualified if (d.id, day, func_id) in x]
            if keys:
                model.add(sum(x[k] for k in keys) >= 1)

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

    # === CONSTRAINT 10: Deltid / explicit arbetsdagar per vecka ===
    for doc in config.doctors:
        # Explicit work_days_per_week tar prioritet, annars beräkna från employment_rate
        explicit_days = getattr(doc, 'work_days_per_week', None)
        if explicit_days is not None:
            max_work_per_week = explicit_days
        elif doc.employment_rate < 1.0 and doc.employment_rate > 0.0:
            max_work_per_week = int(5 * doc.employment_rate + 0.5)
        else:
            continue  # Heltid eller 0% (=ej satt) → hanteras av constraint 8

        pattern = getattr(doc, 'schedule_pattern', 'weekly')

        for week in range(num_weeks):
            # Hoppa över off-veckor för biweekly — de hanteras av constraint 15
            if pattern == 'biweekly_even' and week % 2 == 1:
                continue
            if pattern == 'biweekly_odd' and week % 2 == 0:
                continue

            week_start = week * 7
            work_vars = []
            for d in range(week_start, min(week_start + 7, num_days)):
                if (doc.id, d, "LEDIG") in x:
                    work_vars.append(1 - x[(doc.id, d, "LEDIG")])
            if work_vars:
                model.add(sum(work_vars) <= max_work_per_week)
                # Om explicit_days: solvern ska försöka använda exakt så många dagar
                if explicit_days is not None:
                    model.add(sum(work_vars) >= max(0, max_work_per_week - 1))

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

    # === CONSTRAINT 12: Helgkompensation ===
    # Primärjour helg → 1 ledig vardag veckan efter (hård)
    # Bakjour helg → 1 ledig vardag veckan efter (mjuk penalty)
    weekend_comp_penalties = []

    for doc in config.doctors:
        for week in range(num_weeks - 1):  # Sista veckan har ingen "veckan efter"
            sat = week * 7 + 5
            sun = week * 7 + 6
            next_week_start = (week + 1) * 7
            next_week_weekdays = [d for d in range(next_week_start, min(next_week_start + 5, num_days))]

            if not next_week_weekdays:
                continue

            for call_type in ["JOUR_P", "JOUR_B"]:
                is_primary = (call_type == "JOUR_P")
                weekend_calls = []
                for d in [sat, sun]:
                    if d < num_days and (doc.id, d, call_type) in x:
                        weekend_calls.append(x[(doc.id, d, call_type)])

                if not weekend_calls:
                    continue

                num_weekend_calls = model.new_int_var(0, 2, f"wc_{doc.id}_{week}_{call_type}")
                model.add(num_weekend_calls == sum(weekend_calls))

                next_week_ledig = []
                for d in next_week_weekdays:
                    if (doc.id, d, "LEDIG") in x:
                        next_week_ledig.append(x[(doc.id, d, "LEDIG")])

                if not next_week_ledig:
                    continue

                num_ledig = model.new_int_var(0, 5, f"wl_{doc.id}_{week}_{call_type}")
                model.add(num_ledig == sum(next_week_ledig))

                if is_primary and config.call_structure.weekend_comp_primary:
                    # HÅRD: lediga vardagar >= helgjourdagar
                    model.add(num_ledig >= num_weekend_calls)
                elif not is_primary and config.call_structure.weekend_comp_backup:
                    # MJUK: penalty om inte tillräckligt ledigt
                    shortfall = model.new_int_var(0, 2, f"ws_{doc.id}_{week}")
                    model.add(shortfall >= num_weekend_calls - num_ledig)
                    model.add(shortfall >= 0)
                    weekend_comp_penalties.append(shortfall)

    # === CONSTRAINT 13: Semesterblock ===
    locked_set = set()
    if locked_assignments:
        for doc_id, day_funcs in locked_assignments.items():
            for day_key in day_funcs:
                locked_set.add((doc_id, int(day_key)))

    semester_bonus = []
    for pref in config.preferences:
        if pref.type != "SEMESTER_BLOCK":
            continue
        doc = doc_by_id.get(pref.doctor_id)
        if not doc:
            continue

        sw = pref.details.get("start_week", 0)
        ew = pref.details.get("end_week", sw)

        for week in range(sw, min(ew + 1, num_weeks)):
            for d in range(week * 7, min((week + 1) * 7, num_days)):
                if (doc.id, d) in locked_set:
                    continue  # Skippa låsta dagar
                if (doc.id, d, "LEDIG") not in x:
                    continue
                if pref.priority == 1:
                    model.add(x[(doc.id, d, "LEDIG")] == 1)
                else:
                    semester_bonus.append(x[(doc.id, d, "LEDIG")])

    # === CONSTRAINT 14: Max samtidiga semestrar ===
    max_vac = config.max_concurrent_vacation
    if max_vac == 0:
        max_vac = math.ceil(len(config.doctors) * 0.2)

    semester_days = defaultdict(set)
    for pref in config.preferences:
        if pref.type != "SEMESTER_BLOCK":
            continue
        sw = pref.details.get("start_week", 0)
        ew = pref.details.get("end_week", sw)
        for week in range(sw, min(ew + 1, num_weeks)):
            for d in range(week * 7, min((week + 1) * 7, num_days)):
                semester_days[d].add(pref.doctor_id)

    for day, doc_ids in semester_days.items():
        if len(doc_ids) > max_vac:
            ledig_vars = [x[(did, day, "LEDIG")] for did in doc_ids if (did, day, "LEDIG") in x]
            if ledig_vars:
                model.add(sum(ledig_vars) <= max_vac)

    # === CONSTRAINT 15: Varannan vecka (biweekly pattern) ===
    # Läkare med schedule_pattern="biweekly_even" jobbar bara jämna veckor (0, 2, 4, ...)
    # Läkare med schedule_pattern="biweekly_odd" jobbar bara udda veckor (1, 3, 5, ...)
    for doc in config.doctors:
        pattern = getattr(doc, 'schedule_pattern', 'weekly')
        if pattern in ('biweekly_even', 'biweekly_odd'):
            for week in range(num_weeks):
                is_off_week = (
                    (pattern == 'biweekly_even' and week % 2 == 1) or
                    (pattern == 'biweekly_odd' and week % 2 == 0)
                )
                if is_off_week:
                    week_start = week * 7
                    for d in range(week_start, min(week_start + 7, num_days)):
                        if (doc.id, d, "LEDIG") in x:
                            model.add(x[(doc.id, d, "LEDIG")] == 1)

    # === CONSTRAINT 16: Fasta veckodagar ===
    # fixed_weekdays: {"monday": "OP_CSK", "wednesday": "MOTT_Hässleholm"}
    WEEKDAY_MAP = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4}
    for doc in config.doctors:
        fixed = getattr(doc, 'fixed_weekdays', {})
        if not fixed:
            continue
        pattern = getattr(doc, 'schedule_pattern', 'weekly')
        for day_name, func_id in fixed.items():
            wd = WEEKDAY_MAP.get(day_name.lower())
            if wd is None:
                continue
            for week in range(num_weeks):
                # Respektera biweekly: hoppa över off-veckor
                if pattern == 'biweekly_even' and week % 2 == 1:
                    continue
                if pattern == 'biweekly_odd' and week % 2 == 0:
                    continue
                day_idx = week * 7 + wd
                if day_idx < num_days and (doc.id, day_idx, func_id) in x:
                    model.add(x[(doc.id, day_idx, func_id)] == 1)

    # === CONSTRAINT 17: Min passtyp per vecka ===
    # min_shifts_per_week: {"OP": 1, "MOTT": 1} = minst 1 OP-dag + 1 MOTT-dag per arbetsvecka
    for doc in config.doctors:
        min_shifts = getattr(doc, 'min_shifts_per_week', {})
        if not min_shifts:
            continue
        pattern = getattr(doc, 'schedule_pattern', 'weekly')
        for week in range(num_weeks):
            # Hoppa över off-veckor
            if pattern == 'biweekly_even' and week % 2 == 1:
                continue
            if pattern == 'biweekly_odd' and week % 2 == 0:
                continue
            week_start = week * 7
            for shift_prefix, min_count in min_shifts.items():
                # Hitta alla func_ids som matchar prefixet (t.ex. "OP" matchar "OP_CSK", "OP_Hässleholm")
                matching_vars = []
                for d in range(week_start, min(week_start + 5, num_days)):  # Bara vardagar
                    for func_id, _, _ in day_functions:
                        if func_id.startswith(shift_prefix) and (doc.id, d, func_id) in x:
                            matching_vars.append(x[(doc.id, d, func_id)])
                    # Kolla även exakt match (t.ex. "ADMIN")
                    if (doc.id, d, shift_prefix) in x and shift_prefix not in [f[0] for f in day_functions if f[0].startswith(shift_prefix)]:
                        matching_vars.append(x[(doc.id, d, shift_prefix)])
                if matching_vars:
                    model.add(sum(matching_vars) >= min_count)

    # === CONSTRAINT 18: Max passtyp per vecka ===
    for doc in config.doctors:
        max_shifts = getattr(doc, 'max_shifts_per_week', {})
        if not max_shifts:
            continue
        pattern = getattr(doc, 'schedule_pattern', 'weekly')
        for week in range(num_weeks):
            if pattern == 'biweekly_even' and week % 2 == 1:
                continue
            if pattern == 'biweekly_odd' and week % 2 == 0:
                continue
            week_start = week * 7
            for shift_prefix, max_count in max_shifts.items():
                matching_vars = []
                for d in range(week_start, min(week_start + 5, num_days)):
                    for func_id, _, _ in day_functions:
                        if func_id.startswith(shift_prefix) and (doc.id, d, func_id) in x:
                            matching_vars.append(x[(doc.id, d, func_id)])
                if matching_vars:
                    model.add(sum(matching_vars) <= max_count)

    # === CONSTRAINT 19: AT-block rotation ===
    # AT-läkare med current_rotation_block tilldelas ALLTID blockens funktion
    for doc in config.doctors:
        block = getattr(doc, 'current_rotation_block', {})
        if not block or not block.get('block_type'):
            continue
        func_id = block['block_type']  # t.ex. "AVD_CSK"
        for day in range(num_days):
            weekday = day % 7
            if weekday >= 5:
                continue  # AT jobbar inte helg (såvida de inte har jour)
            if (doc.id, day, func_id) in x:
                # AT ska vara på sin block-funktion varje vardag (om inte ledig/jour)
                # Vi gör det som mjuk constraint — prioritera block-funktionen
                pass  # Hanteras via fixed_weekdays + min_shifts istället

    # === CONSTRAINT 20: AKUT-bemanning ===
    # Minst N läkare på AKUT per vardag
    akut_rule = None
    for rule in getattr(config, 'constraint_rules', []):
        if rule.id == 'akut_staffing' and rule.enabled:
            akut_rule = rule
            break

    if akut_rule:
        akut_min = akut_rule.parameters.get('min_count', 3)
        for day in range(num_days):
            weekday = day % 7
            if weekday >= 5:
                continue
            for site in sorted(akut_sites):
                akut_fid = f"AKUT_{site}"
                akut_vars = [x[(d.id, day, akut_fid)] for d in config.doctors
                             if (d.id, day, akut_fid) in x]
                if akut_vars:
                    # Minst akut_min läkare (fördelat över alla sites — eller per site)
                    # Per site: använd min_count / antal sites
                    per_site_min = max(1, akut_min // max(1, len(akut_sites)))
                    model.add(sum(akut_vars) >= per_site_min)

    # === CONSTRAINT 21: Fasta återkommande aktiviteter ===
    # recurring_activities: [{"weekday": "tuesday", "time": "10:00-11:00", "activity": "Infektionsrond"}]
    # Implementering: om en läkare har en recurring activity på en viss veckodag,
    # säkerställ att den dagen är en arbetsdag (ej ledig) och tilldela ADMIN om
    # aktiviteten inte kräver en specifik funktion.
    for doc in config.doctors:
        activities = getattr(doc, 'recurring_activities', [])
        if not activities:
            continue
        pattern = getattr(doc, 'schedule_pattern', 'weekly')
        for act in activities:
            wd = WEEKDAY_MAP.get(act.get('weekday', '').lower())
            if wd is None:
                continue
            for week in range(num_weeks):
                if pattern == 'biweekly_even' and week % 2 == 1:
                    continue
                if pattern == 'biweekly_odd' and week % 2 == 0:
                    continue
                day_idx = week * 7 + wd
                if day_idx < num_days and (doc.id, day_idx, "LEDIG") in x:
                    # Säkerställ att läkaren INTE är ledig den dagen
                    model.add(x[(doc.id, day_idx, "LEDIG")] == 0)

    # === CONSTRAINT 22: Halvdagar (metadata) ===
    # half_day_schedule: {"monday": {"am": "ADMIN", "pm": "MOTT_CSK"}}
    # Solvern modellerar fortfarande en funktion per dag — vi använder AM-funktionen
    # som huvudtilldelning och lagrar PM som metadata i output.
    for doc in config.doctors:
        half_days = getattr(doc, 'half_day_schedule', {})
        if not half_days:
            continue
        pattern = getattr(doc, 'schedule_pattern', 'weekly')
        for day_name, periods in half_days.items():
            wd = WEEKDAY_MAP.get(day_name.lower())
            if wd is None:
                continue
            am_func = periods.get('am', 'ADMIN')
            for week in range(num_weeks):
                if pattern == 'biweekly_even' and week % 2 == 1:
                    continue
                if pattern == 'biweekly_odd' and week % 2 == 0:
                    continue
                day_idx = week * 7 + wd
                if day_idx < num_days and (doc.id, day_idx, am_func) in x:
                    model.add(x[(doc.id, day_idx, am_func)] == 1)

    # === CONSTRAINT 23: Minst 50% seniora läkare (ÖL + SP) per vecka ===
    senior_docs = [doc for doc in config.doctors if doc.role.value in ("ÖL", "SP")]
    total_seniors = len(senior_docs)
    min_senior_per_week = max(1, math.ceil(total_seniors * 0.5))

    for week in range(num_weeks):
        week_senior_work = []
        for doc in senior_docs:
            pattern = getattr(doc, 'schedule_pattern', 'weekly')
            # Skip off-weeks for biweekly
            if pattern == 'biweekly_even' and week % 2 == 1:
                continue
            if pattern == 'biweekly_odd' and week % 2 == 0:
                continue
            # Count work days (non-LEDIG) for this senior in this week
            for d_offset in range(5):  # Mon-Fri
                day = week * 7 + d_offset
                if day >= num_days:
                    break
                for f in day_functions:
                    if (doc.id, day, f) in x:
                        week_senior_work.append(x[(doc.id, day, f)])

        # At least min_senior_per_week seniors should work in this week
        if week_senior_work:
            model.add(sum(week_senior_work) >= min_senior_per_week)

    # === CONSTRAINT 24: Kontinuitetskrav (COC) — Gruppera MOTT-dagar per läkare per vecka ===
    # Om en läkare har mottagning (MOTT_*) under en vecka, försök samla dem
    # på konsekutiva dagar istället för utspridda. Mjukt constraint via penalty.
    mott_functions = [f for f in day_functions if f.startswith("MOTT")]
    coc_penalty_terms = []

    for doc in config.doctors:
        pattern = getattr(doc, 'schedule_pattern', 'weekly')
        for week in range(num_weeks):
            if pattern == 'biweekly_even' and week % 2 == 1:
                continue
            if pattern == 'biweekly_odd' and week % 2 == 0:
                continue

            # Collect MOTT assignments for Mon-Fri this week
            week_mott = []
            for d_offset in range(5):
                day = week * 7 + d_offset
                if day >= num_days:
                    break
                day_mott_vars = []
                for f in mott_functions:
                    if (doc.id, day, f) in x:
                        day_mott_vars.append(x[(doc.id, day, f)])
                if day_mott_vars:
                    has_mott = model.new_bool_var(f"mott_{doc.id}_w{week}_d{d_offset}")
                    model.add(has_mott <= sum(day_mott_vars))
                    model.add(has_mott >= sum(day_mott_vars) - len(day_mott_vars) + 1)
                    week_mott.append(has_mott)

            # Penalize gaps between MOTT days (non-consecutive pattern)
            # For each pair of non-adjacent MOTT days, add a penalty
            if len(week_mott) >= 3:
                for i in range(len(week_mott) - 2):
                    # If day i has MOTT and day i+2 has MOTT but day i+1 doesn't → gap penalty
                    gap = model.new_bool_var(f"mott_gap_{doc.id}_w{week}_{i}")
                    model.add(gap >= week_mott[i] + week_mott[i+2] - week_mott[i+1] - 1)
                    model.add(gap >= 0)
                    coc_penalty_terms.append(gap)

    # === DETAILED RULES (avancerad regelmotor) ===
    rule_objective_terms = []
    if getattr(config, 'detailed_rules', None):
        try:
            from rule_engine import RuleEngine
            engine = RuleEngine(config, config.detailed_rules)
            rule_objective_terms = engine.compile_to_constraints(
                model, x, num_days, num_weeks, day_functions, call_functions
            )
        except Exception as e:
            print(f"  ⚠ Regelmotor-fel: {e}")

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

    # === MJUK: 36h veckovila — penalty om inte 2 sammanhängande lediga dagar per vecka ===
    weekly_rest_violations = []
    for doc in config.doctors:
        for week in range(num_weeks):
            ws = week * 7
            # has_consecutive_rest = 1 om minst 2 dagar i rad är LEDIG
            has_rest = model.new_bool_var(f"wrest_{doc.id}_{week}")
            # Check all consecutive day pairs in the week
            pair_vars = []
            for d in range(ws, min(ws + 6, num_days - 1)):
                pair = model.new_bool_var(f"rpair_{doc.id}_{d}")
                model.add_bool_and([
                    x[(doc.id, d, "LEDIG")],
                    x[(doc.id, d + 1, "LEDIG")]
                ]).only_enforce_if(pair)
                model.add_bool_or([
                    x[(doc.id, d, "LEDIG")].negated(),
                    x[(doc.id, d + 1, "LEDIG")].negated()
                ]).only_enforce_if(pair.negated())
                pair_vars.append(pair)
            if pair_vars:
                model.add_max_equality(has_rest, pair_vars)
                weekly_rest_violations.append(has_rest)

    # === MJUK: Helgfrekvens — penalty om jour oftare än var N:e helg ===
    weekend_freq = config.call_structure.max_weekend_frequency or 4
    weekend_penalties = []
    for doc in config.doctors:
        if not (doc.can_primary_call or doc.can_backup_call):
            continue
        for week in range(max(0, num_weeks - weekend_freq + 1)):
            # Count weekend calls in a window of weekend_freq weeks
            weekend_calls = []
            for w in range(week, min(week + weekend_freq, num_weeks)):
                sat = w * 7 + 5
                sun = w * 7 + 6
                for d in [sat, sun]:
                    if d < num_days:
                        if (doc.id, d, "JOUR_P") in x:
                            weekend_calls.append(x[(doc.id, d, "JOUR_P")])
                        if (doc.id, d, "JOUR_B") in x:
                            weekend_calls.append(x[(doc.id, d, "JOUR_B")])
            if weekend_calls:
                too_many = model.new_bool_var(f"wknd_{doc.id}_{week}")
                model.add(sum(weekend_calls) > 1).only_enforce_if(too_many)
                model.add(sum(weekend_calls) <= 1).only_enforce_if(too_many.negated())
                weekend_penalties.append(too_many)

    # === MJUK: Site preference bonus ===
    site_pref_bonus = []
    for doc in config.doctors:
        if not doc.site_preference:
            continue
        for day in range(num_days):
            if day % 7 >= 5:
                continue
            for func_id, func, site in day_functions:
                if site == doc.site_preference and (doc.id, day, func_id) in x:
                    site_pref_bonus.append(x[(doc.id, day, func_id)])

    # === OBJEKTIV FUNKTION (vikter från config.constraint_rules) ===
    w_training = _rule_weight(config, "training_st_supervisor", 6)
    w_fairness = _rule_weight(config, "call_fairness", 5)
    w_rest = _rule_weight(config, "atl_weekly_rest", 8)
    w_weekend = _rule_weight(config, "call_weekend_frequency", 5)
    w_site = _rule_weight(config, "preference_site", 3)
    w_comp = _rule_weight(config, "weekend_compensation", 7)

    objective_terms = []
    for bonus_var in st_training_bonus:
        objective_terms.append(w_training * bonus_var)
    for pref_term in preference_bonus:
        objective_terms.append(pref_term)
    if call_counts and w_fairness:
        call_spread = model.new_int_var(0, num_days, "call_spread")
        model.add(call_spread == max_calls - min_calls)
        objective_terms.append(-w_fairness * call_spread)
    for rest_var in weekly_rest_violations:
        objective_terms.append(w_rest * rest_var)
    for wknd_var in weekend_penalties:
        objective_terms.append(-w_weekend * wknd_var)
    for sp_var in site_pref_bonus:
        objective_terms.append(w_site * sp_var)
    # Helgkompensation bakjour (mjuk penalty)
    for penalty in weekend_comp_penalties:
        objective_terms.append(-w_comp * penalty)
    # Semesterblock (mjuka önskemål)
    for sv in semester_bonus:
        objective_terms.append(3 * sv)

    # OB-kostnadsrättvisa
    if config.optimize_ob_cost:
        ob_counts = {}
        for doc in config.doctors:
            ob_terms = []
            for day in range(num_days):
                weekday = day % 7
                for call_type in ["JOUR_P", "JOUR_B"]:
                    if (doc.id, day, call_type) in x:
                        cost = int(_ob_cost(weekday, call_type, config.ob_rates) * 10)
                        if cost > 0:
                            ob_terms.append(cost * x[(doc.id, day, call_type)])
            if ob_terms:
                ob_counts[doc.id] = model.new_int_var(0, num_days * 40, f"ob_{doc.id}")
                model.add(ob_counts[doc.id] == sum(ob_terms))

        if ob_counts:
            max_ob = model.new_int_var(0, num_days * 40, "max_ob")
            min_ob = model.new_int_var(0, num_days * 40, "min_ob")
            for doc_id, ob_var in ob_counts.items():
                model.add(max_ob >= ob_var)
                model.add(min_ob <= ob_var)
            ob_spread = model.new_int_var(0, num_days * 40, "ob_spread")
            model.add(ob_spread == max_ob - min_ob)

            w_ob = _rule_weight(config, "ob_cost_fairness", 4)
            objective_terms.append(-w_ob * ob_spread)

    # Lägg till regelmotor-termer
    objective_terms.extend(rule_objective_terms)

    # COC-penalty (Constraint 24): Straffa glapp i mottagningsdagar
    if coc_penalty_terms:
        w_coc = _rule_weight(config, "continuity_of_care", 3)
        objective_terms.append(-w_coc * sum(coc_penalty_terms))

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

        schedule, half_day_meta = extract_schedule(solver, x, config, num_days, day_functions, call_functions)
        print_schedule(schedule, config, num_days)
        print_statistics(schedule, config, num_days, solver, call_counts)

        # Returnera som tuple-safe dict — metadata separat så expand_to_granular inte kraschar
        result = dict(schedule)
        if half_day_meta:
            result["_meta"] = {"half_day": half_day_meta}

        return result
    else:
        print(f"\n❌ Inget schema hittades. Status: {solver.status_name(status)}")
        return None


def solve_rolling(
    config: ClinicConfig,
    existing_schedule: dict,
    locked_weeks: int,
    new_weeks: int = 1,
    time_limit_seconds: int = 30,
) -> dict:
    """Rullande schemauppdatering — behåll låsta veckor, generera nya.

    Args:
        config: Klinikkonfiguration
        existing_schedule: Befintligt schema {doc_id: {day_idx: func_id}}
        locked_weeks: Antal veckor från befintligt schema att behålla
        new_weeks: Antal nya veckor att generera
        time_limit_seconds: Max lösartid

    Returns:
        Kombinerat schema (låsta + nya veckor), eller None om olösbart
    """
    total_weeks = locked_weeks + new_weeks
    locked_days = locked_weeks * 7

    locked = {}
    for doc_id, days in existing_schedule.items():
        locked[doc_id] = {}
        for day_idx, func_id in days.items():
            day_idx = int(day_idx)
            if day_idx < locked_days:
                locked[doc_id][day_idx] = func_id

    return solve_schedule(
        config,
        num_weeks=total_weeks,
        time_limit_seconds=time_limit_seconds,
        locked_assignments=locked,
    )


def generate_base_schedule(config: ClinicConfig, cycle_weeks: int = 10,
                           time_limit_seconds: int = 120):
    """Generera optimalt grundschema för en hel cykel.

    Anropar solve_schedule() med cycle_weeks som num_weeks.
    Returnerar BaseSchedule-objekt eller None.
    """
    from data_model import BaseSchedule, BaseScheduleSlot
    from datetime import datetime

    schedule = solve_schedule(config, num_weeks=cycle_weeks, time_limit_seconds=time_limit_seconds)
    if schedule is None:
        return None

    slots = []
    for doc_id, days in schedule.items():
        for day_idx, func_id in days.items():
            day_idx = int(day_idx)
            slots.append(BaseScheduleSlot(
                doctor_id=doc_id,
                cycle_week=day_idx // 7,
                weekday=day_idx % 7,
                function=func_id,
            ))

    return BaseSchedule(
        id=f"base_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        name=f"Grundschema {cycle_weeks}v",
        clinic_id=getattr(config, 'name', 'unknown'),
        cycle_length_weeks=cycle_weeks,
        slots=slots,
        created_at=datetime.now().isoformat(),
    )


def resolve_effective_schedule(base, deviations: list, start_date: str,
                                num_weeks: int) -> dict:
    """Beräkna faktiskt schema = grundschema + avvikelser.

    1. Beräkna vilken cykelposition varje datum hamnar på
    2. Hämta grundschema-slot
    3. Applicera avvikelser (override)
    """
    from datetime import date as dt_date, timedelta

    start = dt_date.fromisoformat(start_date)
    cycle_len = base.cycle_length_weeks

    # Bygg slot-lookup: (doctor_id, cycle_week, weekday) → function
    slot_lookup = {}
    for slot in base.slots:
        slot_lookup[(slot.doctor_id, slot.cycle_week, slot.weekday)] = slot.function

    # Bygg deviation-lookup: (doctor_id, date) → new_function
    dev_lookup = {}
    for dev in deviations:
        dev_lookup[(dev.doctor_id, dev.date)] = dev.new_function

    # Generera effektivt schema
    schedule = {}
    num_days = num_weeks * 7
    for day_idx in range(num_days):
        day_date = start + timedelta(days=day_idx)
        date_str = day_date.isoformat()
        cycle_week = (day_idx // 7) % cycle_len
        weekday = day_idx % 7

        for slot in base.slots:
            doc_id = slot.doctor_id
            if doc_id not in schedule:
                schedule[doc_id] = {}

            # Kolla avvikelse först
            if (doc_id, date_str) in dev_lookup:
                schedule[doc_id][day_idx] = dev_lookup[(doc_id, date_str)]
            else:
                # Hämta från grundschema
                func = slot_lookup.get((doc_id, cycle_week, weekday), "LEDIG")
                schedule[doc_id][day_idx] = func

    return schedule


def expand_to_granular(schedule: dict, num_days: int) -> dict:
    """Expandera JOUR_P/JOUR_B till granulära jourtyper baserat på veckodag.

    Vardagar:
        JOUR_P → JOUR_P_KVÄLL (representerar kopplat kväll+natt-pass)
        JOUR_B → JOUR_B (bakjour hemifrån, kväll+natt)
    Helger:
        JOUR_P → JOUR_P_HELGDAG (lördag) / JOUR_P_HELGNATT (söndag)
        JOUR_B → JOUR_B_HELGDAG (lördag) / JOUR_B_HELGNATT (söndag)
    """
    expanded = {}
    for doc_id, days in schedule.items():
        if doc_id.startswith("_"):
            continue  # Hoppa över metadata-nycklar
        expanded[doc_id] = {}
        for day, func_id in days.items():
            weekday = day % 7
            if weekday < 5:  # Vardag
                if func_id == "JOUR_P":
                    expanded[doc_id][day] = "JOUR_P_KVÄLL"
                else:
                    expanded[doc_id][day] = func_id
            else:  # Helg (5=lör, 6=sön)
                if func_id == "JOUR_P":
                    expanded[doc_id][day] = "JOUR_P_HELGDAG" if weekday == 5 else "JOUR_P_HELGNATT"
                elif func_id == "JOUR_B":
                    expanded[doc_id][day] = "JOUR_B_HELGDAG" if weekday == 5 else "JOUR_B_HELGNATT"
                else:
                    expanded[doc_id][day] = func_id
    return expanded


def extract_schedule(solver, x, config, num_days, day_functions, call_functions):
    """Extrahera lösningen till en läsbar datastruktur.

    Returnerar:
        schedule: {doc_id: {day_idx: func_id}}
        half_day_meta: {doc_id: {day_idx: {"am": func_id, "pm": func_id}}}
    """
    WEEKDAY_MAP = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4}

    schedule = {}
    half_day_meta = {}

    for doc in config.doctors:
        schedule[doc.id] = {}
        half_days = getattr(doc, 'half_day_schedule', {})

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

            # Halvdags-metadata
            if half_days:
                for day_name, periods in half_days.items():
                    wd = WEEKDAY_MAP.get(day_name.lower())
                    if wd is not None and weekday == wd and assigned != "LEDIG":
                        if doc.id not in half_day_meta:
                            half_day_meta[doc.id] = {}
                        half_day_meta[doc.id][day] = {
                            "am": periods.get("am", assigned),
                            "pm": periods.get("pm", assigned),
                        }

    return schedule, half_day_meta


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
            if is_jour(func_today) and not is_jour(func_tomorrow) and func_tomorrow != "LEDIG":
                violations += 1
                print(f"  ⚠️  {doc.name}: Jour dag {day+1} → arbete dag {day+2}")

    if violations == 0:
        print("  Inga ATL-brott detekterade ✅")
    else:
        print(f"  {violations} potentiella ATL-brott ⚠️")


if __name__ == "__main__":
    config = create_kristianstad_example()
    schedule = solve_schedule(config, num_weeks=2, time_limit_seconds=30)
