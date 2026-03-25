"""
COP Engine — Tester för Datamodell
====================================
Verifierar att datamodellen är korrekt konfigurerad.
"""

import pytest
from data_model import (
    Role, Doctor, OperatingRoom, ClinicConfig,
    Function, ShiftType, ATLRules,
    create_kristianstad_example, create_generic_example,
)


class TestRoles:
    """Testa rollhierarkin."""

    def test_all_roles_exist(self):
        roles = [r.value for r in Role]
        assert "UL" in roles
        assert "ST_TIDIG" in roles
        assert "ST_SEN" in roles
        assert "SP" in roles
        assert "ÖL" in roles

    def test_role_count(self):
        assert len(Role) == 5


class TestDoctor:
    """Testa Doctor-dataklassen."""

    def test_basic_creation(self):
        doc = Doctor(id="T1", name="Test", role=Role.SPECIALIST)
        assert doc.id == "T1"
        assert doc.employment_rate == 1.0
        assert doc.can_primary_call is False

    def test_st_with_procedures(self):
        doc = Doctor(
            id="ST1", name="ST Test", role=Role.ST_SEN,
            required_procedures={"höftprotes": 20},
            completed_procedures={"höftprotes": 12},
        )
        remaining = doc.required_procedures["höftprotes"] - doc.completed_procedures["höftprotes"]
        assert remaining == 8

    def test_deltid(self):
        doc = Doctor(id="D1", name="Deltid", role=Role.ÖVERLÄKARE, employment_rate=0.8)
        assert doc.employment_rate == 0.8

    def test_exempt_from_call(self):
        doc = Doctor(id="E1", name="Exempt", role=Role.ÖVERLÄKARE, exempt_from_call=True)
        assert doc.exempt_from_call is True

    def test_site_preference_is_string(self):
        doc = Doctor(id="T1", name="Test", role=Role.SPECIALIST, site_preference="Karolinska")
        assert doc.site_preference == "Karolinska"


class TestKristianstadConfig:
    """Testa Kristianstad-exempelkonfigurationen."""

    def test_total_doctors(self, full_config):
        assert len(full_config.doctors) == 25

    def test_role_distribution(self, full_config):
        by_role = {}
        for d in full_config.doctors:
            by_role[d.role] = by_role.get(d.role, 0) + 1
        assert by_role[Role.ÖVERLÄKARE] == 5
        assert by_role[Role.SPECIALIST] == 8
        assert by_role[Role.ST_SEN] == 3
        assert by_role[Role.ST_TIDIG] == 4
        assert by_role[Role.UNDERLÄKARE] == 5

    def test_operating_rooms(self, full_config):
        assert len(full_config.operating_rooms) == 7
        hassleholm = [r for r in full_config.operating_rooms if r.site == "Hässleholm"]
        csk = [r for r in full_config.operating_rooms if r.site == "CSK"]
        assert len(hassleholm) == 5
        assert len(csk) == 2

    def test_sites_are_strings(self, full_config):
        for site in full_config.sites:
            assert isinstance(site, str)
        assert "CSK" in full_config.sites
        assert "Hässleholm" in full_config.sites

    def test_call_structure(self, full_config):
        cs = full_config.call_structure
        assert Role.ST_SEN in cs.primary_roles
        assert Role.SPECIALIST in cs.primary_roles
        assert Role.ÖVERLÄKARE in cs.backup_roles
        assert cs.max_calls_per_month == 4

    def test_atl_rules(self, full_config):
        atl = full_config.atl_rules
        assert atl.min_daily_rest_hours == 11.0
        assert atl.max_weekly_hours == 48.0
        assert atl.min_weekly_rest_hours == 36.0

    def test_all_st_have_supervisors(self, full_config):
        for d in full_config.doctors:
            if d.role in (Role.ST_SEN, Role.ST_TIDIG):
                assert d.supervisor_id is not None, f"{d.name} saknar handledare"

    def test_supervisor_ids_are_valid(self, full_config):
        doc_ids = {d.id for d in full_config.doctors}
        for d in full_config.doctors:
            if d.supervisor_id:
                assert d.supervisor_id in doc_ids, \
                    f"{d.name} har ogiltig handledare {d.supervisor_id}"

    def test_all_doctors_have_unique_ids(self, full_config):
        ids = [d.id for d in full_config.doctors]
        assert len(ids) == len(set(ids)), "Duplicerade läkar-ID"

    def test_all_rooms_have_unique_ids(self, full_config):
        ids = [r.id for r in full_config.operating_rooms]
        assert len(ids) == len(set(ids)), "Duplicerade sal-ID"

    def test_primary_call_eligibility(self, full_config):
        primary = [d for d in full_config.doctors if d.can_primary_call]
        assert len(primary) >= 3

    def test_backup_call_eligibility(self, full_config):
        backup = [d for d in full_config.doctors if d.can_backup_call]
        assert len(backup) >= 3

    def test_exempt_doctor_exists(self, full_config):
        exempt = [d for d in full_config.doctors if d.exempt_from_call]
        assert len(exempt) >= 1

    def test_preferences_reference_valid_doctors(self, full_config):
        doc_ids = {d.id for d in full_config.doctors}
        for pref in full_config.preferences:
            assert pref.doctor_id in doc_ids, \
                f"Preference refererar till okänd läkare {pref.doctor_id}"


class TestGenericConfig:
    """Testa den generiska exempelkonfigurationen."""

    def test_creates_successfully(self):
        config = create_generic_example()
        assert config is not None
        assert len(config.doctors) == 18
        assert len(config.operating_rooms) == 2

    def test_single_site(self):
        config = create_generic_example()
        assert len(config.sites) == 1
        assert config.sites[0] == "Huvudsjukhuset"

    def test_all_rooms_same_site(self):
        config = create_generic_example()
        for room in config.operating_rooms:
            assert room.site == config.sites[0]

    def test_has_staffing_requirements(self):
        config = create_generic_example()
        assert len(config.staffing_requirements) > 0

    def test_has_call_structure(self):
        config = create_generic_example()
        assert len(config.call_structure.primary_roles) > 0
        assert len(config.call_structure.backup_roles) > 0
