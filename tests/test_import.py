"""Tester för schema-import."""
import pytest
from schema_import import SchemaImporter


@pytest.fixture
def importer():
    return SchemaImporter()


class TestImport:
    def test_import_csv_basic(self, importer):
        csv_text = "doctor_id;date;function\nOL1;2026-04-06;OP_CSK\nOL1;2026-04-07;AVD_CSK\nSP1;2026-04-06;JOUR_P"
        result = importer.import_from_csv(csv_text, "krist")
        assert result["imported_doctors"] == 2
        assert result["imported_rows"] == 3
        assert result["schedule"]["OL1"]["2026-04-06"] == "OP_CSK"

    def test_import_unmatched(self, importer):
        csv_text = "doctor_id;date;function\nOL1;2026-04-06;OP_CSK\n;2026-04-07;AVD"
        result = importer.import_from_csv(csv_text, "krist")
        assert len(result["warnings"]) >= 1  # Rad utan doctor_id

    def test_preview(self, importer):
        csv_text = "doctor_id;date;function\nSP1;2026-04-06;MOTT_CSK"
        result = importer.preview_import(csv_text, "krist")
        assert result["preview"] is True
        assert result["imported_rows"] == 1
