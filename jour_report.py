"""
Detaljerad jourrapport — fördelning, rättvisa, OB, kalender.
"""

from datetime import date, timedelta
from data_model import is_jour
from swedish_calendar import get_swedish_holidays


class JourReporter:

    def generate_report(self, schedule: dict, period_start: str, period_end: str,
                        doctors: list) -> dict:
        """Komplett jourrapport."""
        start = date.fromisoformat(period_start)
        end = date.fromisoformat(period_end)
        doc_map = {d.id: d for d in doctors}
        holidays = get_swedish_holidays(start.year)

        by_doctor = {}
        total_by_type = {}

        for doc in doctors:
            by_doctor[doc.id] = {
                "name": doc.name, "role": doc.role.value,
                "total_jours": 0, "by_type": {},
                "weekday_jours": 0, "weekend_jours": 0, "holiday_jours": 0,
                "ob_hours": 0.0,
            }

        current = start
        while current <= end:
            ds = current.isoformat()
            wd = current.weekday()
            is_holiday = ds in holidays
            is_weekend = wd >= 5

            for doc_id, days in schedule.items():
                func = days.get(ds) or days.get(str((current - start).days))
                if not func or not is_jour(func):
                    continue

                if doc_id not in by_doctor:
                    continue

                bd = by_doctor[doc_id]
                bd["total_jours"] += 1
                bd["by_type"][func] = bd["by_type"].get(func, 0) + 1
                total_by_type[func] = total_by_type.get(func, 0) + 1

                if is_holiday:
                    bd["holiday_jours"] += 1
                    bd["ob_hours"] += 12
                elif is_weekend:
                    bd["weekend_jours"] += 1
                    bd["ob_hours"] += 8
                else:
                    bd["weekday_jours"] += 1
                    bd["ob_hours"] += 4

            current += timedelta(days=1)

        # Rättvisa-analys
        counts = [d["total_jours"] for d in by_doctor.values() if d["total_jours"] > 0]
        mean = sum(counts) / len(counts) if counts else 0
        max_c = max(counts) if counts else 0
        min_c = min(counts) if counts else 0
        std = (sum((c - mean)**2 for c in counts) / len(counts))**0.5 if counts else 0

        # Gini-koefficient
        gini = 0
        if counts and sum(counts) > 0:
            sorted_c = sorted(counts)
            n = len(sorted_c)
            total = sum(sorted_c)
            cum = 0
            for i, c in enumerate(sorted_c):
                cum += c
                gini += (2 * (i + 1) - n - 1) * c
            gini = gini / (n * total) if total > 0 else 0

        most = max(by_doctor.values(), key=lambda d: d["total_jours"]) if by_doctor else {}
        least_active = [d for d in by_doctor.values() if d["total_jours"] > 0]
        least = min(least_active, key=lambda d: d["total_jours"]) if least_active else {}

        recs = []
        if max_c > mean * 1.2:
            recs.append(f"{most.get('name','?')} har {max_c} jourer vs medel {mean:.0f} — minska nästa period")
        if min_c < mean * 0.8 and least:
            recs.append(f"{least.get('name','?')} har bara {min_c} jourer — ge fler nästa period")

        return {
            "period": f"{period_start} — {period_end}",
            "summary": {
                "total_jour_shifts": sum(counts),
                "by_type": total_by_type,
            },
            "by_doctor": by_doctor,
            "fairness_analysis": {
                "gini_coefficient": round(abs(gini), 3),
                "mean": round(mean, 1),
                "std_deviation": round(std, 1),
                "max": max_c, "min": min_c,
                "most_jours": {"doctor": most.get("name", "?"), "count": max_c},
                "least_jours": {"doctor": least.get("name", "?"), "count": min_c},
                "max_deviation_pct": round((max_c - mean) / mean * 100, 1) if mean > 0 else 0,
                "recommendations": recs,
            },
        }

    def compare_periods(self, report1: dict, report2: dict) -> dict:
        """Jämför två jourperioder."""
        docs1 = report1.get("by_doctor", {})
        docs2 = report2.get("by_doctor", {})
        changes = {}
        for doc_id in set(list(docs1.keys()) + list(docs2.keys())):
            c1 = docs1.get(doc_id, {}).get("total_jours", 0)
            c2 = docs2.get(doc_id, {}).get("total_jours", 0)
            if c1 != c2:
                name = docs1.get(doc_id, docs2.get(doc_id, {})).get("name", doc_id)
                changes[doc_id] = {"name": name, "period1": c1, "period2": c2, "diff": c2 - c1}
        return {"changes": changes, "total_changes": len(changes)}


jour_reporter = JourReporter()
