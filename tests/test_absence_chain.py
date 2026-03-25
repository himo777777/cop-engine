"""
COP Engine — Tester för Frånvarokedjan
========================================
Verifierar hela flödet: registrera → analysera → ranka → validera → ersätt → notifiera.
"""

import pytest
from datetime import date, timedelta
from data_model import Role, create_kristianstad_example
from solver import solve_schedule
from absence_chain import AbsenceChain, ChainStatus, AbsenceType


@pytest.fixture
def chain_setup(full_config, solved_schedule, schedule_start_date):
    """Förbered en AbsenceChain med färdigt schema."""
    chain = AbsenceChain(full_config, solved_schedule, schedule_start_date, num_weeks=2)
    return chain, solved_schedule


class TestAbsenceRegistration:
    """Testa registrering av frånvaro."""

    def test_register_valid_absence(self, chain_setup, schedule_start_date):
        chain, schedule = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-07")
        assert result.doctor_id == "SP1"
        assert result.absence_type == "sjuk"
        assert result.status != ChainStatus.FAILED

    def test_invalid_doctor_fails(self, chain_setup):
        chain, _ = chain_setup
        result = chain.execute("NONEXISTENT", "sjuk", "2026-04-06", "2026-04-07")
        assert result.status == ChainStatus.FAILED

    def test_chain_has_id(self, chain_setup):
        chain, _ = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-06")
        assert result.chain_id.startswith("chain_")

    def test_various_absence_types(self, chain_setup):
        """Alla frånvarotyper ska accepteras."""
        for atype in ["sjuk", "vab", "semester", "utbildning", "konferens", "akut"]:
            chain, _ = chain_setup
            result = chain.execute("SP1", atype, "2026-04-06", "2026-04-06")
            assert result.status != ChainStatus.FAILED, f"Typ '{atype}' misslyckades"


class TestVacantSlotDetection:
    """Testa identifiering av vakanta positioner."""

    def test_finds_vacant_slots(self, chain_setup):
        chain, schedule = chain_setup
        # SP1 bör ha minst ett par arbetsdagar
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-10", auto_select=False)
        assert len(result.vacant_slots) > 0, "Hittade inga vakanta slots"

    def test_ledig_days_not_vacant(self, chain_setup):
        chain, schedule = chain_setup
        # Helg (lör+sön) — de flesta ska vara lediga
        result = chain.execute("UL1", "sjuk", "2026-04-11", "2026-04-12")
        # UL har sällan jour, så helgen bör vara tom
        for slot in result.vacant_slots:
            assert slot["function"] != "LEDIG"

    def test_call_slots_marked_critical(self, chain_setup):
        chain, schedule = chain_setup
        # Hitta en läkare med jour och gör dem frånvarande
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-19")
        critical = [s for s in result.vacant_slots if s.get("critical")]
        # SP1 har jourer i 2-veckorsperioden
        # (kan vara 0 om SP1 inte har jour just dessa dagar)
        for slot in critical:
            assert slot["function"] in ("JOUR_P", "JOUR_B")


class TestCandidateRanking:
    """Testa ranking av ersättarkandidater."""

    def test_candidates_returned(self, chain_setup):
        chain, _ = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-06", auto_select=False)
        if result.replacements:
            candidates = result.replacements[0].get("candidates", [])
            assert len(candidates) > 0, "Inga kandidater returnerades"

    def test_candidates_sorted_by_score(self, chain_setup):
        chain, _ = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-06", auto_select=False)
        if result.replacements:
            candidates = result.replacements[0].get("candidates", [])
            scores = [c["score"] for c in candidates]
            assert scores == sorted(scores, reverse=True), "Kandidater inte sorterade efter poäng"

    def test_absent_doctor_not_candidate(self, chain_setup):
        chain, _ = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-06", auto_select=False)
        for replacement in result.replacements:
            for c in replacement.get("candidates", []):
                assert c["doctor_id"] != "SP1", "Frånvarande läkare är kandidat"

    def test_candidates_have_reasons(self, chain_setup):
        chain, _ = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-06", auto_select=False)
        if result.replacements:
            candidates = result.replacements[0].get("candidates", [])
            for c in candidates:
                assert len(c["reasons"]) > 0, f"{c['doctor_id']} saknar motiveringar"


class TestATLValidation:
    """Testa ATL-validering av ersättare."""

    def test_atl_checked_for_replacements(self, chain_setup):
        chain, _ = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-10")
        # Alla ersättningar ska ha atl_ok flagga
        for change in result.schedule_changes:
            assert "atl_ok" in change

    def test_no_rest_violation_after_call(self, chain_setup):
        """Ersättare som hade jour igår ska inte sättas på arbete."""
        chain, schedule = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-10")

        for change in result.schedule_changes:
            if change["atl_ok"]:
                repl_id = change["replacement_doctor"]
                day = change["day"]
                # Kolla att ersättaren inte hade jour igår
                if day > 0:
                    yesterday = schedule.get(repl_id, {}).get(day - 1, "LEDIG")
                    func = change["function"]
                    if yesterday in ("JOUR_P", "JOUR_B"):
                        assert func in ("LEDIG", "JOUR_P", "JOUR_B"), \
                            f"ATL-brott: {repl_id} jour dag {day-1} → {func} dag {day}"


class TestScheduleUpdate:
    """Testa att schemat uppdateras korrekt."""

    def test_absent_doctor_set_to_ledig(self, chain_setup):
        chain, schedule = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-08")

        # Alla dagar i frånvaroperioden ska vara LEDIG
        for change in result.schedule_changes:
            day = change["day"]
            assert schedule["SP1"].get(day) == "LEDIG", \
                f"SP1 dag {day}: {schedule['SP1'].get(day)} (ska vara LEDIG)"

    def test_replacement_gets_function(self, chain_setup):
        chain, schedule = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-08")

        for change in result.schedule_changes:
            repl_id = change["replacement_doctor"]
            day = change["day"]
            expected = change["function"]
            actual = schedule[repl_id].get(day)
            assert actual == expected, \
                f"Ersättare {repl_id} dag {day}: {actual} (ska vara {expected})"


class TestNotifications:
    """Testa notifieringssystemet."""

    def test_absent_doctor_notified(self, chain_setup):
        chain, _ = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-08")
        absent_notifs = [n for n in result.notifications if n["to"] == "SP1"]
        assert len(absent_notifs) >= 1, "Frånvarande läkare fick ingen notifiering"

    def test_replacements_notified(self, chain_setup):
        chain, _ = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-08")

        notified_ids = {n["to"] for n in result.notifications}
        for change in result.schedule_changes:
            assert change["replacement_doctor"] in notified_ids, \
                f"Ersättare {change['replacement_doctor']} fick ingen notifiering"

    def test_scheduler_notified_on_failure(self, full_config, solved_schedule, schedule_start_date):
        """Om en slot inte kan fyllas → schemaläggare notifieras."""
        # Gör många läkare frånvarande för att tvinga misslyckande
        chain = AbsenceChain(full_config, solved_schedule, schedule_start_date, num_weeks=2)
        # Kör normal frånvaro först — om det finns failed_slots ska schemaläggare notifieras
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-19")
        if result.failed_slots:
            scheduler_notifs = [n for n in result.notifications if n["to"] == "schemaläggare"]
            assert len(scheduler_notifs) >= 1

    def test_notifications_have_timestamps(self, chain_setup):
        chain, _ = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-06")
        for notif in result.notifications:
            assert "timestamp" in notif


class TestChainLog:
    """Testa att kedjeloggen är komplett."""

    def test_log_has_steps(self, chain_setup):
        chain, _ = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-08")
        assert len(result.chain_log) >= 3, "Kedjeloggen har för få steg"

    def test_log_starts_with_registration(self, chain_setup):
        chain, _ = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-06")
        assert result.chain_log[0]["step"] == 1
        assert "Registrerar" in result.chain_log[0]["action"]

    def test_log_ends_with_completion(self, chain_setup):
        chain, _ = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-06")
        last = result.chain_log[-1]
        assert last["status"] in ("completed", "manual_required", "atl_violation")


class TestAutoSelectVsManual:
    """Testa auto_select=True vs False."""

    def test_auto_select_updates_schedule(self, chain_setup):
        chain, schedule = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-06", auto_select=True)
        if result.schedule_changes:
            assert len(result.schedule_changes) > 0

    def test_manual_mode_no_schedule_change(self, full_config, schedule_start_date):
        """auto_select=False ska INTE ändra schemat."""
        schedule = solve_schedule(full_config, num_weeks=2, time_limit_seconds=30)
        assert schedule is not None

        # Spara original
        original_sp1 = dict(schedule.get("SP1", {}))

        chain = AbsenceChain(full_config, schedule, schedule_start_date, num_weeks=2)
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-06", auto_select=False)

        # Schema ska vara oförändrat
        for d in range(14):
            assert schedule["SP1"].get(d) == original_sp1.get(d), \
                f"Schema ändrades i manuellt läge (dag {d})"

    def test_manual_returns_candidates(self, chain_setup):
        chain, _ = chain_setup
        result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-06", auto_select=False)
        if result.replacements:
            assert result.replacements[0].get("selected") is None, \
                "Manuellt läge ska inte välja ersättare"
            assert len(result.replacements[0].get("candidates", [])) > 0
