"""Tester för löneunderlag-export."""
import pytest
from payroll_export import PayrollExporter, PayCode
from data_model import Doctor, Role


@pytest.fixture
def exporter():
    return PayrollExporter()


@pytest.fixture
def doctors():
    return [
        Doctor(id="OL1", name="Dr Andersson", role=Role.ÖVERLÄKARE, personal_number="19700101-1234"),
        Doctor(id="SP1", name="Dr Fredriksson", role=Role.SPECIALIST, personal_number="19800515-5678"),
    ]


class TestPayroll:
    def test_generate_basic(self, exporter, doctors):
        schedule = {"OL1": {"2026-04-13": "OP_CSK", "2026-04-14": "JOUR_P"},
                    "SP1": {"2026-04-13": "JOUR_B", "2026-04-14": "AVD_CSK"}}
        entries = exporter.generate_payroll(schedule, "2026-04-13", "2026-04-14", doctors)
        assert len(entries) > 0

    def test_ob_kvall(self, exporter, doctors):
        schedule = {"OL1": {"2026-04-13": "JOUR_P"}}  # Måndag
        entries = exporter.generate_payroll(schedule, "2026-04-13", "2026-04-13", doctors)
        ob_kvall = [e for e in entries if e.pay_code == PayCode.OB_KVALL]
        assert len(ob_kvall) == 1
        assert ob_kvall[0].hours == 4.0

    def test_ob_natt(self, exporter, doctors):
        schedule = {"OL1": {"2026-04-13": "JOUR_P"}}
        entries = exporter.generate_payroll(schedule, "2026-04-13", "2026-04-13", doctors)
        ob_natt = [e for e in entries if e.pay_code == PayCode.OB_NATT]
        assert len(ob_natt) == 1
        assert ob_natt[0].hours == 8.0

    def test_ob_helg(self, exporter, doctors):
        schedule = {"OL1": {"2026-04-18": "JOUR_P"}}  # Lördag
        entries = exporter.generate_payroll(schedule, "2026-04-18", "2026-04-18", doctors)
        ob_helg = [e for e in entries if e.pay_code == PayCode.OB_HELG]
        assert len(ob_helg) == 1

    def test_ob_storhelg(self, exporter, doctors):
        schedule = {"OL1": {"2026-12-25": "JOUR_P"}}  # Juldagen
        entries = exporter.generate_payroll(schedule, "2026-12-25", "2026-12-25", doctors)
        ob_storhelg = [e for e in entries if e.pay_code == PayCode.OB_STORHELG]
        assert len(ob_storhelg) == 1

    def test_jour_prim(self, exporter, doctors):
        schedule = {"OL1": {"2026-04-13": "JOUR_P"}}
        entries = exporter.generate_payroll(schedule, "2026-04-13", "2026-04-13", doctors)
        jour = [e for e in entries if e.pay_code == PayCode.JOUR_PRIM]
        assert len(jour) == 1

    def test_export_paxml(self, exporter, doctors):
        schedule = {"OL1": {"2026-04-13": "JOUR_P"}}
        entries = exporter.generate_payroll(schedule, "2026-04-13", "2026-04-13", doctors)
        xml = exporter.export_paxml(entries, "COP", "2026-04")
        assert "<PAXml" in xml
        assert "<PayCode>" in xml

    def test_export_csv(self, exporter, doctors):
        schedule = {"OL1": {"2026-04-13": "JOUR_P"}}
        entries = exporter.generate_payroll(schedule, "2026-04-13", "2026-04-13", doctors)
        csv = exporter.export_csv(entries)
        assert "Läkare;Personnr" in csv
        assert "Dr Andersson" in csv

    def test_validate(self, exporter, doctors):
        schedule = {"OL1": {"2026-04-13": "JOUR_P"},
                    "SP1": {"2026-04-13": "JOUR_B"}}
        entries = exporter.generate_payroll(schedule, "2026-04-13", "2026-04-13", doctors)
        errors = exporter.validate(entries)
        assert all("negativa" not in e for e in errors)
