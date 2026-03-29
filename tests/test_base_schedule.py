"""Tester för grundschema och avvikelser."""

import pytest
from data_model import create_kristianstad_example, BaseSchedule, BaseScheduleSlot, ScheduleDeviation
from solver import generate_base_schedule, resolve_effective_schedule, solve_schedule


@pytest.fixture
def config():
    return create_kristianstad_example()


class TestBaseSchedule:
    def test_generate_base_schedule(self, config):
        """Generera grundschema — alla läkare ska ha tilldelning varje dag."""
        base = generate_base_schedule(config, cycle_weeks=2, time_limit_seconds=30)
        assert base is not None
        assert base.cycle_length_weeks == 2
        assert len(base.slots) > 0
        # Varje läkare ska ha minst 1 slot per dag i cykeln
        doc_ids = {d.id for d in config.doctors}
        slot_docs = {s.doctor_id for s in base.slots}
        assert doc_ids == slot_docs, f"Saknade läkare i grundschema: {doc_ids - slot_docs}"

    def test_resolve_effective_schedule(self, config):
        """Grundschema + avvikelse → korrekt effektivt schema."""
        base = generate_base_schedule(config, cycle_weeks=2, time_limit_seconds=30)
        assert base is not None

        # Skapa avvikelse: OL1 ledig dag 3
        dev = ScheduleDeviation(
            id="dev_1", base_schedule_id=base.id,
            date="2026-04-09",  # Torsdag i vecka 1
            doctor_id="OL1",
            original_function="OP_CSK",
            new_function="LEDIG",
            reason="sjuk",
        )

        schedule = resolve_effective_schedule(base, [dev], "2026-04-06", 2)
        assert schedule is not None
        assert "OL1" in schedule
        # Dag 3 (torsdag) ska vara LEDIG
        assert schedule["OL1"][3] == "LEDIG"

    def test_deviation_override(self, config):
        """Avvikelse ska skriva över grundschema korrekt."""
        base = generate_base_schedule(config, cycle_weeks=2, time_limit_seconds=30)
        assert base is not None

        # Hämta original funktion för SP1 dag 0
        original = None
        for s in base.slots:
            if s.doctor_id == "SP1" and s.cycle_week == 0 and s.weekday == 0:
                original = s.function
                break

        # Skapa avvikelse
        dev = ScheduleDeviation(
            id="dev_2", base_schedule_id=base.id,
            date="2026-04-06",  # Måndag
            doctor_id="SP1",
            original_function=original or "OP_CSK",
            new_function="SEMESTER",
            reason="semester",
        )

        schedule = resolve_effective_schedule(base, [dev], "2026-04-06", 2)
        assert schedule["SP1"][0] == "SEMESTER"
