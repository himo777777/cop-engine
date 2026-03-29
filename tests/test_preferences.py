"""Tester för schemaönskemål."""
import pytest
from preferences import PreferenceManager, ScheduleWish, WishPeriod


@pytest.fixture
def pm():
    return PreferenceManager()


class TestPreferences:
    def test_create_period(self, pm):
        p = pm.create_period("krist", "Maj 2026", "2026-05-01", "2026-05-31", "2026-04-15")
        assert p.id.startswith("wp_")
        assert p.status == "open"

    def test_submit_wish(self, pm):
        pm.create_period("krist", "Maj", "2026-05-01", "2026-05-31", "2026-04-15")
        w = ScheduleWish(id="w1", doctor_id="OL1", period_id="wp_1",
                         wish_type="ledig_dag", priority="normal", dates=["2026-05-15"])
        pm.submit_wish(w)
        wishes = pm.get_wishes_for_period("wp_1")
        assert len(wishes) == 1

    def test_collision_detection(self, pm):
        pm.create_period("krist", "Maj", "2026-05-01", "2026-05-31", "2026-04-15")
        for i in range(6):
            pm.submit_wish(ScheduleWish(
                id=f"w{i}", doctor_id=f"SP{i+1}", period_id="wp_1",
                wish_type="ledig_dag", dates=["2026-05-15"],
            ))
        collisions = pm.get_collision_report("wp_1", max_concurrent=3)
        assert "2026-05-15" in collisions
        assert collisions["2026-05-15"]["count"] == 6

    def test_convert_to_solver(self, pm):
        w = ScheduleWish(id="w1", doctor_id="OL1", period_id="wp_1",
                         wish_type="ledig_dag", priority="must", dates=["2026-05-15"])
        pm.submit_wish(w)
        prefs = pm.convert_to_solver_input([w], None)
        assert len(prefs) == 1
        assert prefs[0].priority == 1  # MÅSTE → priority 1

    def test_fulfillment_report(self, pm):
        pm.create_period("krist", "Maj", "2026-05-01", "2026-05-31", "2026-04-15")
        pm.submit_wish(ScheduleWish(
            id="w1", doctor_id="OL1", period_id="wp_1",
            wish_type="ledig_dag", dates=["2026-05-15"],
        ))
        schedule = {"OL1": {0: "LEDIG", 1: "OP_CSK"}}
        report = pm.get_fulfillment_report("wp_1", schedule)
        assert report["total"] == 1
