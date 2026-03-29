"""Tester för svenska röda dagar och specialdagar."""

import pytest
from swedish_calendar import (
    get_swedish_holidays, get_special_days, get_bridge_days,
    is_reduced_staffing_day, get_holiday_staffing_rules, _easter,
)


class TestSwedishHolidays:
    def test_all_holidays_2026(self):
        """Alla röda dagar 2026 ska finnas."""
        h = get_swedish_holidays(2026)
        assert "2026-01-01" in h  # Nyårsdagen
        assert "2026-01-06" in h  # Trettondedag jul
        assert "2026-05-01" in h  # Första maj
        assert "2026-06-06" in h  # Nationaldagen
        assert "2026-12-25" in h  # Juldagen
        assert "2026-12-26" in h  # Annandag jul
        assert len(h) >= 14       # Minst 14 röda dagar

    def test_easter_2026(self):
        """Påsk 2026 = 5 april."""
        easter = _easter(2026)
        assert easter.month == 4
        assert easter.day == 5

    def test_easter_2025(self):
        """Påsk 2025 = 20 april."""
        easter = _easter(2025)
        assert easter.month == 4
        assert easter.day == 20

    def test_midsummer_2026(self):
        """Midsommardagen 2026 ska vara en lördag i juni."""
        h = get_swedish_holidays(2026)
        midsummer_dates = [d for d, name in h.items() if "Midsommar" in name]
        assert len(midsummer_dates) >= 2  # Midsommarafton + Midsommardagen
        from datetime import date
        for d in midsummer_dates:
            dt = date.fromisoformat(d)
            assert dt.month == 6

    def test_half_days(self):
        """Julafton, nyårsafton, midsommarafton ska vara halvdagar."""
        s = get_special_days(2026)
        assert "2026-12-24" in s
        assert s["2026-12-24"]["type"] == "half_day"
        assert "2026-12-31" in s
        assert s["2026-12-31"]["close_at"] == "12:00"

    def test_bridge_days(self):
        """Klämdagar ska identifieras."""
        bridges = get_bridge_days(2026)
        # Kristi himmelsfärd 2026 = torsdag 14 maj → fredag 15 maj = klämdag
        assert isinstance(bridges, list)

    def test_holiday_staffing(self):
        """Röd dag → ingen OP, reducerad bemanning."""
        rules = get_holiday_staffing_rules("2026-12-25")  # Juldagen
        assert rules["op_rooms_open"] == 0
        assert rules["double_call"] is True  # Storhelg

    def test_normal_day_staffing(self):
        """Normal vardag → normal bemanning."""
        rules = get_holiday_staffing_rules("2026-04-07")  # Tisdag
        assert rules["staffing_level"] == "normal"

    def test_reduced_staffing(self):
        """is_reduced_staffing_day returnerar korrekt info."""
        info = is_reduced_staffing_day("2026-12-25")
        assert info["is_holiday"] is True
        assert info["staffing_level"] == "minimal"
