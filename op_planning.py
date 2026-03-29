"""
OP-lista-koppling — matcha operationer med schemalagda kirurger.
"""

from dataclasses import dataclass, field


@dataclass
class Operation:
    id: str
    patient_id: str = ""
    procedure: str = ""
    required_competence: str = ""
    estimated_duration_min: int = 120
    priority: str = "elektiv"
    site: str = ""
    op_room: str = None
    scheduled_date: str = None
    assigned_surgeon: str = None
    assistant_needed: bool = True


class OPPlanner:
    """Kopplar OP-lista med schemalagda läkare."""

    def match_competence(self, operations: list, schedule: dict, doctor_competences: dict,
                         date_str: str = None) -> dict:
        """Matcha operationer med kirurger baserat på kompetens."""
        matched = []
        unmatched = []

        # Hitta vilka läkare som är på OP denna dag
        op_doctors = set()
        for doc_id, days in schedule.items():
            func = days.get(date_str, "")
            if func and func.startswith("OP"):
                op_doctors.add(doc_id)

        available = {d: list(c) for d, c in doctor_competences.items() if d in op_doctors}

        for op in operations:
            found = False
            for doc_id, comps in available.items():
                if op.required_competence in comps or not op.required_competence:
                    matched.append({"operation": op.id, "procedure": op.procedure,
                                   "surgeon": doc_id, "competence_match": True})
                    found = True
                    break
            if not found:
                unmatched.append({"operation": op.id, "procedure": op.procedure,
                                 "required": op.required_competence,
                                 "reason": f"Ingen med {op.required_competence}-kompetens schemalagd"})

        return {"matched": matched, "unmatched": unmatched,
                "warnings": [f"{len(unmatched)} operationer saknar rätt kompetens"] if unmatched else []}

    def suggest_changes(self, unmatched: list, full_schedule: dict, doctor_competences: dict,
                        date_str: str = None) -> list:
        """Föreslå schemaändringar för omatchade operationer."""
        suggestions = []
        for um in unmatched:
            needed = um.get("required", "")
            for doc_id, comps in doctor_competences.items():
                if needed in comps:
                    func = full_schedule.get(doc_id, {}).get(date_str, "LEDIG")
                    if func and not func.startswith("OP") and func != "LEDIG":
                        suggestions.append(f"Flytta {doc_id} från {func} till OP (har {needed}-kompetens)")
                        break
        return suggestions

    def calculate_utilization(self, operations: list, available_rooms: int,
                               op_minutes_per_day: int = 480) -> dict:
        """Beräkna salsutnyttjande."""
        total_min = sum(op.estimated_duration_min for op in operations)
        capacity = available_rooms * op_minutes_per_day
        util = total_min / capacity if capacity > 0 else 0
        rooms_needed = -(-total_min // op_minutes_per_day)  # Ceil division
        return {
            "rooms_needed": rooms_needed,
            "rooms_available": available_rooms,
            "utilization_pct": round(util, 2),
            "total_op_minutes": total_min,
            "capacity_minutes": capacity,
        }

    def get_day_summary(self, date_str: str, operations: list, schedule: dict,
                        doctor_competences: dict) -> dict:
        """Komplett OP-dagsöversikt."""
        match = self.match_competence(operations, schedule, doctor_competences, date_str)
        util = self.calculate_utilization(operations, 5)
        return {
            "date": date_str,
            "total_operations": len(operations),
            "matched": len(match["matched"]),
            "unmatched": len(match["unmatched"]),
            "utilization": util,
            "details": match,
        }


op_planner = OPPlanner()
