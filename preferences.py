"""
Schemaönskemål-hantering.
Samlar in, validerar och konverterar önskemål till solver-input.
"""

from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, date
from typing import Optional
import math


class WishType(Enum):
    LEDIG_DAG = "ledig_dag"
    LEDIG_VECKA = "ledig_vecka"
    UNDVIK_JOUR = "undvik_jour"
    FÖREDRA_FUNKTION = "föredra_funktion"
    UNDVIK_FUNKTION = "undvik_funktion"
    FÖREDRA_SITE = "föredra_site"
    JOUR_PREF = "jour_pref"
    ANNAN = "annan"


class WishPriority(Enum):
    MÅSTE = "must"
    STARK = "strong"
    NORMAL = "normal"
    SVAG = "weak"


PRIORITY_WEIGHTS = {
    WishPriority.MÅSTE: 10,
    WishPriority.STARK: 8,
    WishPriority.NORMAL: 5,
    WishPriority.SVAG: 2,
}


@dataclass
class ScheduleWish:
    id: str
    doctor_id: str
    doctor_name: str = ""
    period_id: str = ""
    wish_type: str = "ledig_dag"
    priority: str = "normal"
    dates: list = field(default_factory=list)
    week_numbers: list = field(default_factory=list)
    weekdays: list = field(default_factory=list)
    function: str = None
    site: str = None
    note: str = ""
    status: str = "pending"
    admin_note: str = ""
    created_at: str = None
    updated_at: str = None


@dataclass
class WishPeriod:
    id: str
    clinic_id: str
    name: str
    schedule_start: str
    schedule_end: str
    wish_deadline: str
    status: str = "open"
    created_by: str = None
    created_at: str = None


class PreferenceManager:
    """Hanterar önskemålsinsamling och konvertering till solver-input."""

    def __init__(self):
        self._periods = {}
        self._wishes = []

    def create_period(self, clinic_id, name, schedule_start, schedule_end, wish_deadline) -> WishPeriod:
        period = WishPeriod(
            id=f"wp_{len(self._periods)+1}",
            clinic_id=clinic_id, name=name,
            schedule_start=schedule_start, schedule_end=schedule_end,
            wish_deadline=wish_deadline,
            created_at=datetime.now().isoformat(),
        )
        self._periods[period.id] = period
        return period

    def submit_wish(self, wish: ScheduleWish) -> ScheduleWish:
        wish.created_at = datetime.now().isoformat()
        self._wishes.append(wish)
        return wish

    def get_wishes_for_period(self, period_id, doctor_id=None) -> list:
        wishes = [w for w in self._wishes if w.period_id == period_id]
        if doctor_id:
            wishes = [w for w in wishes if w.doctor_id == doctor_id]
        return wishes

    def close_period(self, period_id) -> Optional[WishPeriod]:
        period = self._periods.get(period_id)
        if period:
            period.status = "closed"
        return period

    def convert_to_solver_input(self, wishes, config):
        """Konvertera önskemål till Preference-objekt."""
        from data_model import Preference
        preferences = []
        for w in wishes:
            if w.status == "rejected":
                continue
            prio_enum = WishPriority(w.priority) if w.priority in [p.value for p in WishPriority] else WishPriority.NORMAL
            weight = PRIORITY_WEIGHTS.get(prio_enum, 5)
            solver_prio = 1 if prio_enum == WishPriority.MÅSTE else 2

            if w.wish_type in ("ledig_dag", WishType.LEDIG_DAG.value):
                for d in (w.dates or []):
                    preferences.append(Preference(
                        doctor_id=w.doctor_id, type="LEDIG_DAG",
                        priority=solver_prio, details={"date": d, "weight": weight},
                    ))
            elif w.wish_type in ("ledig_vecka", WishType.LEDIG_VECKA.value):
                for wk in (w.week_numbers or []):
                    preferences.append(Preference(
                        doctor_id=w.doctor_id, type="SEMESTER_BLOCK",
                        priority=solver_prio, details={"start_week": wk, "end_week": wk},
                    ))
            elif w.wish_type in ("undvik_jour", WishType.UNDVIK_JOUR.value):
                preferences.append(Preference(
                    doctor_id=w.doctor_id, type="SKIFT_PREF",
                    priority=3, details={"avoid": "JOUR", "dates": w.dates, "weight": weight},
                ))
        return preferences

    def get_collision_report(self, period_id, max_concurrent=5) -> dict:
        """Dagar där för många önskar ledigt."""
        wishes = self.get_wishes_for_period(period_id)
        date_counts = {}
        for w in wishes:
            for d in (w.dates or []):
                if d not in date_counts:
                    date_counts[d] = {"count": 0, "doctors": []}
                date_counts[d]["count"] += 1
                date_counts[d]["doctors"].append(w.doctor_id)

        collisions = {d: {**info, "max_allowed": max_concurrent}
                      for d, info in date_counts.items() if info["count"] > max_concurrent}
        return collisions

    def get_fulfillment_report(self, period_id, schedule) -> dict:
        """Hur många önskemål uppfylldes?"""
        wishes = self.get_wishes_for_period(period_id)
        total = len(wishes)
        fulfilled = 0
        for w in wishes:
            if w.wish_type in ("ledig_dag", WishType.LEDIG_DAG.value):
                for d in (w.dates or []):
                    doc_sched = schedule.get(w.doctor_id, {})
                    for day_idx, func in doc_sched.items():
                        if func == "LEDIG":
                            fulfilled += 1
                            break
        rate = fulfilled / total if total > 0 else 1.0
        return {"total": total, "fulfilled": fulfilled, "rate": round(rate, 2)}


# Global instance
preference_manager = PreferenceManager()
