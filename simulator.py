"""
COP "Vad händer om?"-simulator
================================
Simulerar konsekvenser av hypotetiska förändringar.
Kör solve_schedule() med modifierad config och jämför resultat.
"""

import copy
from dataclasses import dataclass, field
from typing import Optional

from data_model import ClinicConfig, Doctor, Role, OperatingRoom
from solver import solve_schedule, _ob_cost


@dataclass
class Scenario:
    """Ett hypotetiskt scenario."""
    type: str       # "doctor_change", "doctor_leaves", "add_doctor", "room_change", "rule_change"
    description: str = ""
    changes: dict = field(default_factory=dict)


@dataclass
class SimulationResult:
    """Resultat av en simulation."""
    feasible: bool
    baseline_score: float = 0
    simulated_score: float = 0
    impact_summary_sv: str = ""
    staffing_impact: dict = field(default_factory=dict)
    call_impact: dict = field(default_factory=dict)
    ob_impact: dict = field(default_factory=dict)
    constraint_violations: list = field(default_factory=list)
    recommendations_sv: list = field(default_factory=list)
    risk_level: str = "low"


class WhatIfSimulator:
    """Simulerar hypotetiska förändringar i klinikkonfigurationen."""

    def __init__(self, config: ClinicConfig, baseline_schedule: dict = None):
        self.config = config
        self.baseline = baseline_schedule

    def simulate(self, scenarios: list, time_limit: int = 15) -> list:
        """Kör simuleringar för en lista scenarier."""
        results = []
        for scenario in scenarios:
            result = self._run_scenario(scenario, time_limit)
            results.append(result)
        return results

    def _run_scenario(self, scenario: Scenario, time_limit: int) -> SimulationResult:
        """Kör ett enskilt scenario."""
        modified = copy.deepcopy(self.config)

        try:
            self._apply_changes(modified, scenario)
        except Exception as e:
            return SimulationResult(
                feasible=False,
                impact_summary_sv=f"Kunde inte tillämpa scenario: {e}",
                risk_level="critical",
            )

        # Kör solver med modifierad config
        result_schedule = solve_schedule(modified, num_weeks=2, time_limit_seconds=time_limit)

        if result_schedule is None:
            recs = self._generate_recommendations(scenario, modified)
            return SimulationResult(
                feasible=False,
                impact_summary_sv=f"Schemat blir olösbart med denna ändring ({scenario.description})",
                recommendations_sv=recs,
                risk_level="critical",
            )

        # Jämför med baseline
        staffing = self._compare_staffing(result_schedule, modified)
        calls = self._compare_calls(result_schedule, modified)
        ob = self._compare_ob(result_schedule, modified)
        summary = self._build_summary(scenario, staffing, calls, ob)
        risk = self._assess_risk(staffing, calls)

        return SimulationResult(
            feasible=True,
            staffing_impact=staffing,
            call_impact=calls,
            ob_impact=ob,
            impact_summary_sv=summary,
            recommendations_sv=self._generate_recommendations(scenario, modified),
            risk_level=risk,
        )

    def _apply_changes(self, config: ClinicConfig, scenario: Scenario):
        """Tillämpa scenario-ändringar på config."""
        changes = scenario.changes
        st = scenario.type

        if st == "doctor_change":
            doc_id = changes.get("doctor_id")
            for doc in config.doctors:
                if doc.id == doc_id:
                    if "employment_rate" in changes:
                        doc.employment_rate = changes["employment_rate"]
                    if "can_primary_call" in changes:
                        doc.can_primary_call = changes["can_primary_call"]
                    if "can_backup_call" in changes:
                        doc.can_backup_call = changes["can_backup_call"]
                    break

        elif st == "doctor_leaves":
            doc_id = changes.get("doctor_id")
            config.doctors = [d for d in config.doctors if d.id != doc_id]

        elif st == "add_doctor":
            new = Doctor(
                id=changes.get("id", "NEW_DOC"),
                name=changes.get("name", "Ny läkare"),
                role=Role(changes.get("role", "SP")),
                can_primary_call=changes.get("can_primary_call", False),
                can_backup_call=changes.get("can_backup_call", False),
                employment_rate=changes.get("employment_rate", 1.0),
            )
            config.doctors.append(new)

        elif st == "room_change":
            site = changes.get("site")
            delta = changes.get("rooms_delta", 0)
            if delta < 0:
                # Ta bort salar
                rooms_at_site = [r for r in config.operating_rooms if r.site == site]
                to_remove = abs(delta)
                for r in rooms_at_site[-to_remove:]:
                    config.operating_rooms.remove(r)
            elif delta > 0:
                for i in range(delta):
                    config.operating_rooms.append(OperatingRoom(
                        id=f"NEW_OP_{site}_{i}", site=site, name=f"Ny sal {site} {i+1}",
                    ))

        elif st == "rule_change":
            rule_id = changes.get("rule_id")
            for rule in config.constraint_rules:
                if rule.id == rule_id:
                    if "enabled" in changes:
                        rule.enabled = changes["enabled"]
                    if "weight" in changes:
                        rule.weight = changes["weight"]
                    break

    def _compare_staffing(self, schedule: dict, config: ClinicConfig) -> dict:
        """Jämför bemanningsnivåer."""
        func_counts = {}
        for days in schedule.values():
            for func in days.values():
                func_counts[func] = func_counts.get(func, 0) + 1

        baseline_counts = {}
        if self.baseline:
            for days in self.baseline.values():
                for func in days.values():
                    baseline_counts[func] = baseline_counts.get(func, 0) + 1

        impact = {}
        for func in set(list(func_counts.keys()) + list(baseline_counts.keys())):
            new = func_counts.get(func, 0)
            old = baseline_counts.get(func, new)
            if old > 0:
                impact[func] = {"before": old, "after": new, "change_pct": round((new - old) / old * 100, 1)}

        return impact

    def _compare_calls(self, schedule: dict, config: ClinicConfig) -> dict:
        """Jämför jourfördelning."""
        call_per_doc = {}
        for doc_id, days in schedule.items():
            calls = sum(1 for f in days.values() if f in ("JOUR_P", "JOUR_B"))
            if calls > 0:
                call_per_doc[doc_id] = calls
        values = list(call_per_doc.values())
        return {
            "per_doctor": call_per_doc,
            "max": max(values) if values else 0,
            "min": min(values) if values else 0,
            "spread": (max(values) - min(values)) if values else 0,
        }

    def _compare_ob(self, schedule: dict, config: ClinicConfig) -> dict:
        """Jämför OB-kostnader."""
        total = 0
        for doc_id, days in schedule.items():
            for day, func in days.items():
                total += _ob_cost(int(day) % 7, func, config.ob_rates)
        return {"total_ob": round(total, 1)}

    def _build_summary(self, scenario, staffing, calls, ob) -> str:
        """Bygg sammanfattning på svenska."""
        parts = []
        changes = scenario.changes

        if scenario.type == "doctor_change":
            doc_id = changes.get("doctor_id", "?")
            if "employment_rate" in changes:
                parts.append(f"{doc_id} till {int(changes['employment_rate']*100)}% tjänstgöring")
        elif scenario.type == "doctor_leaves":
            parts.append(f"{changes.get('doctor_id', '?')} slutar")
        elif scenario.type == "room_change":
            parts.append(f"OP-sal ändring på {changes.get('site')}: {changes.get('rooms_delta', 0):+d}")

        spread = calls.get("spread", 0)
        if spread > 3:
            parts.append(f"jourspridning {spread} (ojämn)")

        return " | ".join(parts) if parts else "Simulation genomförd"

    def _assess_risk(self, staffing, calls) -> str:
        """Bedöm risknivå."""
        spread = calls.get("spread", 0)
        if spread > 5:
            return "high"
        if spread > 3:
            return "medium"
        return "low"

    def _generate_recommendations(self, scenario, config) -> list:
        """Generera rekommendationer."""
        recs = []
        if scenario.type == "doctor_leaves":
            role = None
            for d in self.config.doctors:
                if d.id == scenario.changes.get("doctor_id"):
                    role = d.role.value
                    break
            if role:
                recs.append(f"Rekrytera en {role} för att kompensera")
            recs.append("Överväg tillfällig ökning av jourbelastning")
        elif scenario.type == "doctor_change":
            if scenario.changes.get("employment_rate", 1) < 0.8:
                recs.append("Kontrollera att jourbemanningen inte påverkas")
        elif scenario.type == "room_change":
            if scenario.changes.get("rooms_delta", 0) < 0:
                recs.append("Omplanera OP-program till andra salar/sites")
        return recs
