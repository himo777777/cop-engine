"""Tester för OP-planering."""
import pytest
from op_planning import OPPlanner, Operation


@pytest.fixture
def planner():
    return OPPlanner()


class TestOPPlanning:
    def test_match_competence(self, planner):
        ops = [Operation(id="op1", procedure="Höftprotes", required_competence="höftprotes")]
        schedule = {"OL1": {"2026-04-06": "OP_CSK"}, "SP1": {"2026-04-06": "AVD_CSK"}}
        comps = {"OL1": ["höftprotes", "trauma"], "SP1": ["artroskopi"]}
        result = planner.match_competence(ops, schedule, comps, "2026-04-06")
        assert len(result["matched"]) == 1
        assert result["matched"][0]["surgeon"] == "OL1"

    def test_unmatched_warning(self, planner):
        ops = [Operation(id="op1", procedure="Ryggop", required_competence="rygg")]
        schedule = {"SP1": {"2026-04-06": "OP_CSK"}}
        comps = {"SP1": ["trauma"]}  # Ingen rygg-kompetens
        result = planner.match_competence(ops, schedule, comps, "2026-04-06")
        assert len(result["unmatched"]) == 1

    def test_room_utilization(self, planner):
        ops = [Operation(id=f"op{i}", estimated_duration_min=120) for i in range(4)]
        result = planner.calculate_utilization(ops, 3)
        assert result["rooms_available"] == 3
        assert result["total_op_minutes"] == 480
        assert result["utilization_pct"] > 0

    def test_suggest_changes(self, planner):
        unmatched = [{"operation": "op1", "required": "rygg", "reason": "Ingen ryggkirurg"}]
        schedule = {"SP2": {"2026-04-06": "AVD_CSK"}}
        comps = {"SP2": ["rygg", "trauma"]}
        suggestions = planner.suggest_changes(unmatched, schedule, comps, "2026-04-06")
        assert len(suggestions) >= 1
        assert "rygg" in suggestions[0]
