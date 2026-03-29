"""
Schema-import från Excel/CSV.
"""

import csv
import io
from dataclasses import dataclass, field
from typing import Optional


class SchemaImporter:
    """Importera befintligt schema från Excel eller CSV."""

    def import_from_csv(self, csv_text: str, clinic_id: str, mapping: dict = None) -> dict:
        """Importera schema från CSV-text."""
        mapping = mapping or {}
        reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")

        schedule = {}
        unmatched_doctors = set()
        unmatched_functions = set()
        warnings = []
        imported = 0

        for i, row in enumerate(reader):
            doc_id = row.get("doctor_id", row.get("läkare", "")).strip()
            date_str = row.get("date", row.get("datum", "")).strip()
            func = row.get("function", row.get("funktion", "")).strip()

            if not doc_id or not date_str:
                warnings.append(f"Rad {i+2}: saknar läkare eller datum")
                continue

            mapped_func = mapping.get(func, func)

            if doc_id not in schedule:
                schedule[doc_id] = {}
            schedule[doc_id][date_str] = mapped_func
            imported += 1

        return {
            "schedule": schedule,
            "unmatched_doctors": list(unmatched_doctors),
            "unmatched_functions": list(unmatched_functions),
            "warnings": warnings,
            "imported_rows": imported,
            "imported_doctors": len(schedule),
        }

    def preview_import(self, csv_text: str, clinic_id: str) -> dict:
        """Förhandsgranska utan att spara."""
        result = self.import_from_csv(csv_text, clinic_id)
        result["preview"] = True
        return result

    def import_from_excel(self, file_bytes: bytes, clinic_id: str, mapping: dict = None) -> dict:
        """Importera från Excel (openpyxl)."""
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes))
            ws = wb.active
        except ImportError:
            return {"error": "openpyxl ej installerat", "schedule": {}, "warnings": ["pip install openpyxl"]}
        except Exception as e:
            return {"error": str(e), "schedule": {}, "warnings": []}

        mapping = mapping or {}
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) < 2:
            return {"schedule": {}, "warnings": ["Filen är tom"], "imported_rows": 0}

        headers = [str(h or "").strip() for h in rows[0]]
        schedule = {}
        warnings = []
        imported = 0

        for i, row in enumerate(rows[1:], start=2):
            doc_id = str(row[0] or "").strip()
            if not doc_id:
                continue
            if doc_id not in schedule:
                schedule[doc_id] = {}
            for j, cell in enumerate(row[1:], start=1):
                if j >= len(headers):
                    break
                date_key = headers[j]
                func = str(cell or "").strip()
                if not func:
                    continue
                mapped = mapping.get(func, func)
                schedule[doc_id][date_key] = mapped
                imported += 1

        return {
            "schedule": schedule,
            "unmatched_doctors": [],
            "unmatched_functions": [],
            "warnings": warnings,
            "imported_rows": imported,
            "imported_doctors": len(schedule),
        }


schema_importer = SchemaImporter()
