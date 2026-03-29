"""Tester för jourrapport."""
import pytest
from jour_report import JourReporter
from data_model import Doctor, Role


@pytest.fixture
def reporter():
    return JourReporter()


@pytest.fixture
def doctors():
    return [
        Doctor(id="OL1", name="Dr A", role=Role.ÖVERLÄKARE),
        Doctor(id="SP1", name="Dr B", role=Role.SPECIALIST),
        Doctor(id="SP2", name="Dr C", role=Role.SPECIALIST),
    ]


class TestJourReport:
    def test_basic_report(self, reporter, doctors):
        schedule = {
            "OL1": {"2026-04-13": "JOUR_P", "2026-04-14": "LEDIG", "2026-04-15": "JOUR_B"},
            "SP1": {"2026-04-13": "LEDIG", "2026-04-14": "JOUR_P", "2026-04-15": "LEDIG"},
            "SP2": {"2026-04-13": "JOUR_B", "2026-04-14": "LEDIG", "2026-04-15": "JOUR_P"},
        }
        report = reporter.generate_report(schedule, "2026-04-13", "2026-04-15", doctors)
        assert report["summary"]["total_jour_shifts"] == 5
        assert report["by_doctor"]["OL1"]["total_jours"] == 2

    def test_fairness_score(self, reporter, doctors):
        # Ojämn fördelning: OL1 har 5, SP1 har 1, SP2 har 1
        schedule = {
            "OL1": {f"2026-04-{13+i:02d}": "JOUR_P" for i in range(5)},
            "SP1": {"2026-04-18": "JOUR_P"},
            "SP2": {"2026-04-19": "JOUR_P"},
        }
        report = reporter.generate_report(schedule, "2026-04-13", "2026-04-19", doctors)
        assert report["by_doctor"]["OL1"]["total_jours"] > report["by_doctor"]["SP1"]["total_jours"]
        assert report["fairness_analysis"]["max"] > report["fairness_analysis"]["min"]

    def test_compare_periods(self, reporter, doctors):
        r1 = {"by_doctor": {"OL1": {"name": "Dr A", "total_jours": 5}, "SP1": {"name": "Dr B", "total_jours": 3}}}
        r2 = {"by_doctor": {"OL1": {"name": "Dr A", "total_jours": 3}, "SP1": {"name": "Dr B", "total_jours": 5}}}
        comp = reporter.compare_periods(r1, r2)
        assert comp["total_changes"] == 2
        assert comp["changes"]["OL1"]["diff"] == -2

    def test_holiday_coverage(self, reporter, doctors):
        schedule = {"OL1": {"2026-12-25": "JOUR_P"}}
        report = reporter.generate_report(schedule, "2026-12-25", "2026-12-25", doctors)
        assert report["by_doctor"]["OL1"]["holiday_jours"] == 1
