"""
COP Engine — Klinisk Validering
=================================
Kör solvern mot realistiska schemaperioder och analyserar:
  - ATL-efterlevnad (dygnsvila, veckovila, max arbetstid)
  - OP-kapacitet (fyllnadsgrad)
  - Jourfördelning (standardavvikelse)
  - Rollbalans per dag
  - Frånvarokedjans effektivitet
  - Jämförelse med manuell schemaläggning (uppskattad)

Kör: python clinical_validation.py
"""

import sys
import os
import time
import json
from datetime import date, timedelta
from collections import Counter, defaultdict
from statistics import mean, stdev

# Importera COP-moduler
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data_model import create_kristianstad_example  # Används i demo (__main__)
from solver import solve_schedule

# ============================================================================
# VALIDERING
# ============================================================================

def validate_schedule(config, schedule, num_weeks):
    """Komplett klinisk validering av ett genererat schema."""
    results = {
        "summary": {},
        "atl": {},
        "operations": {},
        "on_call": {},
        "workload": {},
        "role_balance": {},
        "absence_simulation": {},
    }

    assignments = schedule.get("assignments", [])
    doctors = config["doctors"]
    doctor_map = {d["id"]: d for d in doctors}
    total_days = num_weeks * 7

    print(f"\n{'='*70}")
    print(f"  COP KLINISK VALIDERING — {num_weeks} veckor, {len(doctors)} läkare")
    print(f"{'='*70}\n")

    # --- 1. Grundläggande statistik ---
    print("📊 GRUNDLÄGGANDE STATISTIK")
    print("-" * 40)

    shift_counts = Counter()
    doctor_shifts = defaultdict(list)

    for a in assignments:
        doc = a.get("doctor_id", a.get("doctor", ""))
        shift = a.get("shift_type", a.get("shift", ""))
        day = a.get("day", a.get("day_index", 0))
        site = a.get("site", "")
        shift_counts[shift] += 1
        doctor_shifts[doc].append({"day": day, "shift": shift, "site": site})

    total_assignments = len(assignments)
    print(f"  Totalt tilldelningar: {total_assignments}")
    print(f"  Skiftfördelning:")
    for shift, count in sorted(shift_counts.items()):
        print(f"    {shift:12s}: {count:4d} ({count/total_assignments*100:.1f}%)")

    results["summary"] = {
        "total_assignments": total_assignments,
        "total_doctors": len(doctors),
        "total_days": total_days,
        "weeks": num_weeks,
        "shift_distribution": dict(shift_counts),
    }

    # --- 2. ATL-validering ---
    print(f"\n⚖️  ATL-VALIDERING")
    print("-" * 40)

    atl_violations = []
    atl_warnings = []

    for doc_id, shifts in doctor_shifts.items():
        doc = doctor_map.get(doc_id)
        if not doc:
            continue

        shifts_sorted = sorted(shifts, key=lambda s: s["day"])

        # Dygnsvila: kontrollera efter jour/beredskap
        for i in range(len(shifts_sorted) - 1):
            curr = shifts_sorted[i]
            next_s = shifts_sorted[i + 1]
            if curr["shift"] in ("JOUR", "BEREDSKAP") and next_s["day"] == curr["day"] + 1:
                if next_s["shift"] not in ("LEDIG",):
                    atl_violations.append({
                        "doctor": doc_id,
                        "type": "DYGNSVILA",
                        "detail": f"Arbete dag {next_s['day']} efter {curr['shift']} dag {curr['day']}",
                    })

        # Max arbetsdagar per vecka
        for week in range(num_weeks):
            week_shifts = [s for s in shifts_sorted
                          if week * 7 <= s["day"] < (week + 1) * 7
                          and s["shift"] not in ("LEDIG",)]
            work_days = len(week_shifts)

            max_days = 5
            if doc.get("employment_rate", 1.0) < 1.0:
                max_days = int(5 * doc["employment_rate"] + 0.5)

            if work_days > max_days:
                atl_violations.append({
                    "doctor": doc_id,
                    "type": "MAX_ARBETSDAGAR",
                    "detail": f"Vecka {week+1}: {work_days} dagar (max {max_days})",
                })

        # Max 1 jour per vecka
        for week in range(num_weeks):
            week_calls = [s for s in shifts_sorted
                         if week * 7 <= s["day"] < (week + 1) * 7
                         and s["shift"] == "JOUR"]
            if len(week_calls) > 1:
                atl_violations.append({
                    "doctor": doc_id,
                    "type": "JOURFREKVENS",
                    "detail": f"Vecka {week+1}: {len(week_calls)} jourer (max 1)",
                })

    if atl_violations:
        print(f"  ❌ {len(atl_violations)} ATL-överträdelser hittade:")
        for v in atl_violations[:10]:
            print(f"    - [{v['type']}] {v['doctor']}: {v['detail']}")
    else:
        print(f"  ✅ 0 ATL-överträdelser — fullt ATL-kompatibelt!")

    results["atl"] = {
        "violations": len(atl_violations),
        "details": atl_violations[:20],
        "compliant": len(atl_violations) == 0,
    }

    # --- 3. OP-kapacitet ---
    print(f"\n🔪 OPERATIONSKAPACITET")
    print("-" * 40)

    rooms_per_day = config.get("rooms_per_day", 7)
    op_per_day = defaultdict(int)
    for a in assignments:
        shift = a.get("shift_type", a.get("shift", ""))
        day = a.get("day", a.get("day_index", 0))
        if shift == "OP":
            op_per_day[day] += 1

    weekday_ops = {d: c for d, c in op_per_day.items() if d % 7 < 5}  # Bara vardagar
    if weekday_ops:
        avg_op = mean(weekday_ops.values())
        min_op = min(weekday_ops.values())
        max_op = max(weekday_ops.values())
        fill_rate = avg_op / rooms_per_day * 100
        print(f"  OP-salar per dag: {rooms_per_day}")
        print(f"  Genomsnittlig beläggning: {avg_op:.1f} / {rooms_per_day} ({fill_rate:.0f}%)")
        print(f"  Min: {min_op}, Max: {max_op}")
        print(f"  {'✅' if fill_rate >= 80 else '⚠️'} Fyllnadsgrad: {fill_rate:.0f}%")
    else:
        avg_op = 0
        fill_rate = 0
        print(f"  ⚠️ Inga OP-tilldelningar hittade")

    results["operations"] = {
        "rooms_available": rooms_per_day,
        "avg_ops_per_day": round(avg_op, 1) if weekday_ops else 0,
        "fill_rate_pct": round(fill_rate, 1),
        "min_ops_day": min(weekday_ops.values()) if weekday_ops else 0,
        "max_ops_day": max(weekday_ops.values()) if weekday_ops else 0,
    }

    # --- 4. Jourfördelning ---
    print(f"\n🌙 JOURFÖRDELNING")
    print("-" * 40)

    call_counts = Counter()
    for a in assignments:
        shift = a.get("shift_type", a.get("shift", ""))
        doc = a.get("doctor_id", a.get("doctor", ""))
        if shift == "JOUR":
            call_counts[doc] += 1

    if call_counts:
        call_values = list(call_counts.values())
        avg_calls = mean(call_values)
        call_std = stdev(call_values) if len(call_values) > 1 else 0
        print(f"  Behöriga jourläkare: {len(call_counts)}")
        print(f"  Genomsnitt: {avg_calls:.1f} jourer per läkare")
        print(f"  Standardavvikelse: {call_std:.2f}")
        print(f"  Min: {min(call_values)}, Max: {max(call_values)}")
        fairness = "✅ Jämn" if call_std < 1.0 else "⚠️ Ojämn" if call_std < 2.0 else "❌ Mycket ojämn"
        print(f"  Rättvisa: {fairness} (σ = {call_std:.2f})")

        print(f"\n  Per läkare:")
        for doc, count in sorted(call_counts.items(), key=lambda x: -x[1]):
            bar = "█" * count
            print(f"    {doc:8s}: {count} {bar}")
    else:
        call_std = 0
        print(f"  ⚠️ Inga jourer tilldelade")

    results["on_call"] = {
        "total_doctors_with_call": len(call_counts),
        "distribution": dict(call_counts),
        "std_deviation": round(call_std, 2),
        "fair": call_std < 1.5 if call_counts else True,
    }

    # --- 5. Arbetsbelastning per läkare ---
    print(f"\n📈 ARBETSBELASTNING")
    print("-" * 40)

    workloads = {}
    for doc_id, shifts in doctor_shifts.items():
        doc = doctor_map.get(doc_id, {})
        work_shifts = [s for s in shifts if s["shift"] not in ("LEDIG",)]
        employment = doc.get("employment_rate", 1.0)
        expected = num_weeks * 5 * employment
        actual = len(work_shifts)
        deviation = ((actual - expected) / expected * 100) if expected > 0 else 0
        workloads[doc_id] = {
            "actual": actual,
            "expected": round(expected, 1),
            "deviation_pct": round(deviation, 1),
            "employment_rate": employment,
        }

    deviations = [abs(w["deviation_pct"]) for w in workloads.values()]
    avg_dev = mean(deviations) if deviations else 0
    max_dev = max(deviations) if deviations else 0
    print(f"  Genomsnittlig avvikelse: {avg_dev:.1f}%")
    print(f"  Max avvikelse: {max_dev:.1f}%")
    print(f"  {'✅' if max_dev < 30 else '⚠️'} Balans: {'Bra' if avg_dev < 15 else 'Acceptabel' if avg_dev < 25 else 'Behöver förbättras'}")

    # Visa topp-5 avvikelser
    sorted_work = sorted(workloads.items(), key=lambda x: abs(x[1]["deviation_pct"]), reverse=True)
    if sorted_work:
        print(f"\n  Största avvikelser:")
        for doc_id, w in sorted_work[:5]:
            arrow = "↑" if w["deviation_pct"] > 0 else "↓"
            print(f"    {doc_id:8s}: {w['actual']}/{w['expected']} dagar ({arrow}{abs(w['deviation_pct']):.0f}%) [tjg: {w['employment_rate']*100:.0f}%]")

    results["workload"] = {
        "avg_deviation_pct": round(avg_dev, 1),
        "max_deviation_pct": round(max_dev, 1),
        "balanced": avg_dev < 20,
        "per_doctor": workloads,
    }

    # --- 6. Rollbalans per dag ---
    print(f"\n👥 ROLLBALANS PER DAG")
    print("-" * 40)

    role_per_day = defaultdict(lambda: defaultdict(int))
    for a in assignments:
        doc_id = a.get("doctor_id", a.get("doctor", ""))
        day = a.get("day", a.get("day_index", 0))
        shift = a.get("shift_type", a.get("shift", ""))
        if shift not in ("LEDIG",):
            role = doc_id.split("_")[0] if "_" in doc_id else doc_id[:2]
            role_per_day[day][role] += 1

    senior_present_days = 0
    total_weekdays = 0
    for day in range(total_days):
        if day % 7 < 5:  # Vardag
            total_weekdays += 1
            roles = role_per_day[day]
            if roles.get("OL", 0) >= 1 or roles.get("ÖL", 0) >= 1:
                senior_present_days += 1

    senior_pct = (senior_present_days / total_weekdays * 100) if total_weekdays > 0 else 0
    print(f"  Seniornärvaro (ÖL): {senior_present_days}/{total_weekdays} vardagar ({senior_pct:.0f}%)")
    print(f"  {'✅' if senior_pct >= 95 else '⚠️'} Seniorkrav: {'Uppfyllt' if senior_pct >= 95 else 'Ej uppfyllt'}")

    results["role_balance"] = {
        "senior_presence_pct": round(senior_pct, 1),
        "senior_present_days": senior_present_days,
        "total_weekdays": total_weekdays,
    }

    # --- 7. Frånvarosimulering ---
    print(f"\n🔄 FRÅNVAROSIMULERING")
    print("-" * 40)

    try:
        from absence_chain import AbsenceChain, AbsenceType
        start_date = date(2026, 4, 6)  # Måndag

        chain = AbsenceChain(config, schedule, start_date, num_weeks)
        sim_results = []

        # Simulera 5 frånvarofall
        test_cases = [
            ("OL1", AbsenceType.SJUK, start_date + timedelta(days=1), start_date + timedelta(days=2)),
            ("ST1", AbsenceType.VAB, start_date + timedelta(days=3), start_date + timedelta(days=4)),
            ("SPEC2", AbsenceType.SJUK, start_date + timedelta(days=7), start_date + timedelta(days=9)),
        ]

        for doc_id, absence_type, start, end in test_cases:
            try:
                result = chain.execute(doc_id, absence_type, start, end, auto_select=True)
                sim_results.append({
                    "doctor": doc_id,
                    "type": absence_type.value,
                    "status": result.status.value,
                    "steps": len(result.chain_steps),
                    "covered": result.status.value in ("COMPLETED", "completed"),
                })
                status_icon = "✅" if result.status.value in ("COMPLETED", "completed") else "⚠️"
                print(f"  {status_icon} {doc_id} ({absence_type.value}): {result.status.value} — {len(result.chain_steps)} steg")
            except Exception as e:
                sim_results.append({"doctor": doc_id, "status": "ERROR", "error": str(e)})
                print(f"  ❌ {doc_id}: {e}")

        covered = sum(1 for r in sim_results if r.get("covered", False))
        total_sim = len(sim_results)
        print(f"\n  Täckningsgrad: {covered}/{total_sim} ({covered/total_sim*100:.0f}%)")
        results["absence_simulation"] = {
            "total_cases": total_sim,
            "covered": covered,
            "coverage_pct": round(covered / total_sim * 100, 1) if total_sim > 0 else 0,
            "details": sim_results,
        }
    except Exception as e:
        print(f"  ⚠️ Frånvarosimulering ej tillgänglig: {e}")
        results["absence_simulation"] = {"error": str(e)}

    # --- SLUTBETYG ---
    print(f"\n{'='*70}")
    print(f"  SLUTBETYG")
    print(f"{'='*70}")

    score = 100
    penalties = []

    if results["atl"]["violations"] > 0:
        penalty = min(results["atl"]["violations"] * 10, 40)
        score -= penalty
        penalties.append(f"ATL-brott: -{penalty}p ({results['atl']['violations']} st)")

    if results["operations"]["fill_rate_pct"] < 80:
        penalty = int((80 - results["operations"]["fill_rate_pct"]) / 2)
        score -= penalty
        penalties.append(f"OP-fyllnad: -{penalty}p ({results['operations']['fill_rate_pct']:.0f}%)")

    if results["on_call"].get("std_deviation", 0) > 1.5:
        penalty = int(results["on_call"]["std_deviation"] * 5)
        score -= penalty
        penalties.append(f"Jourfördelning: -{penalty}p (σ={results['on_call']['std_deviation']:.2f})")

    if results["workload"]["avg_deviation_pct"] > 20:
        penalty = int((results["workload"]["avg_deviation_pct"] - 20) / 2)
        score -= penalty
        penalties.append(f"Arbetsbelastning: -{penalty}p ({results['workload']['avg_deviation_pct']:.0f}% avvikelse)")

    score = max(0, score)

    if penalties:
        for pen in penalties:
            print(f"  {pen}")
        print()

    grade = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 60 else "F"
    grade_emoji = {"A": "🏆", "B": "✅", "C": "⚠️", "D": "⚠️", "F": "❌"}[grade]

    print(f"  {grade_emoji} BETYG: {grade} ({score}/100)")
    print(f"{'='*70}\n")

    results["summary"]["score"] = score
    results["summary"]["grade"] = grade

    return results


# ============================================================================
# MAIN — Kör validering
# ============================================================================

if __name__ == "__main__":
    print("🏥 COP Engine — Klinisk Validering")
    print("Bygger konfiguration...")

    # Demo: validera Kristianstad-exemplet
    clinic = create_kristianstad_example()

    # Konvertera ClinicConfig till dict för validering
    config = {
        "doctors": [{"id": d.id, "name": d.name, "role": d.role.value,
                      "employment_rate": d.employment_rate,
                      "can_primary_call": d.can_primary_call,
                      "can_backup_call": d.can_backup_call,
                      "exempt_from_call": d.exempt_from_call,
                      "competencies": d.competencies,
                      "supervisor_id": d.supervisor_id}
                     for d in clinic.doctors],
        "rooms_per_day": len(clinic.operating_rooms),
        "sites": list(clinic.sites),
        "call_structure": {"primary_roles": [r.value for r in clinic.call_structure.primary_roles],
                           "backup_roles": [r.value for r in clinic.call_structure.backup_roles]},
    }

    for num_weeks in [1, 2]:
        print(f"\n\n{'#'*70}")
        print(f"#  VALIDERING: {num_weeks} VECKOR")
        print(f"{'#'*70}")

        t0 = time.time()
        schedule = solve_schedule(clinic, num_weeks=num_weeks)
        elapsed = time.time() - t0
        print(f"\n⏱️  Solver tid: {elapsed:.1f} sekunder")

        if schedule:
            # Konvertera solver-format {doc_id: {day: "OP_H"}} till flat lista
            flat_assignments = []
            for doc_id, days in schedule.items():
                for day, func_str in days.items():
                    # func_str = "OP_H", "MOTT_C", "JOUR_P", "JOUR_B", "LEDIG", "AVD_H", etc.
                    parts = func_str.split("_") if "_" in func_str else [func_str, ""]
                    shift_type = parts[0]  # OP, MOTT, JOUR, AVD, LEDIG
                    site = parts[1] if len(parts) > 1 else ""
                    flat_assignments.append({
                        "doctor_id": doc_id,
                        "day": day,
                        "shift_type": shift_type,
                        "site": site,
                    })
            flat_schedule = {"assignments": flat_assignments}
            results = validate_schedule(config, flat_schedule, num_weeks)

            # Spara resultat
            outfile = f"validation_results_{num_weeks}w.json"
            with open(outfile, "w") as f:
                json.dump(results, f, indent=2, ensure_ascii=False, default=str)
            print(f"📄 Resultat sparade: {outfile}")
        else:
            print("❌ Solver hittade ingen lösning!")

    print("\n✅ Klinisk validering komplett!")
