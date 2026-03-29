"""
Löneunderlag-export för svenska sjukvårdssystem.
Genererar OB, jourersättning, komp och övertid.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum

from data_model import is_jour
from swedish_calendar import get_swedish_holidays, is_reduced_staffing_day


class PayCode(Enum):
    OB_KVALL = "201"
    OB_NATT = "202"
    OB_HELG = "203"
    OB_STORHELG = "204"
    JOUR_PRIM = "301"
    JOUR_BAK = "302"
    JOUR_HELG_PRIM = "303"
    JOUR_HELG_BAK = "304"
    KOMP_INTJANAD = "502"
    SEMESTER = "601"


PAY_CODE_NAMES = {
    PayCode.OB_KVALL: "OB kväll",
    PayCode.OB_NATT: "OB natt",
    PayCode.OB_HELG: "OB helg",
    PayCode.OB_STORHELG: "OB storhelg",
    PayCode.JOUR_PRIM: "Primärjour",
    PayCode.JOUR_BAK: "Bakjour",
    PayCode.JOUR_HELG_PRIM: "Primärjour helg",
    PayCode.JOUR_HELG_BAK: "Bakjour helg",
    PayCode.KOMP_INTJANAD: "Komp intjänad",
    PayCode.SEMESTER: "Semester",
}


@dataclass
class PayrollEntry:
    doctor_id: str
    doctor_name: str
    personal_number: str
    date: str
    pay_code: PayCode
    hours: float
    note: str = ""


class PayrollExporter:
    """Genererar löneunderlag från schema."""

    def generate_payroll(self, schedule: dict, period_start: str, period_end: str,
                         doctors: list) -> list:
        """Generera löneposter för perioden."""
        entries = []
        start = date.fromisoformat(period_start)
        end = date.fromisoformat(period_end)
        doc_map = {d.id: d for d in doctors}
        holidays = get_swedish_holidays(start.year)

        current = start
        while current <= end:
            ds = current.isoformat()
            wd = current.weekday()
            is_holiday = ds in holidays
            is_weekend = wd >= 5

            for doc_id, days in schedule.items():
                func = days.get(ds) or days.get(str((current - start).days))
                if not func or func == "LEDIG":
                    continue

                doc = doc_map.get(doc_id)
                if not doc:
                    continue
                name = doc.name
                pnr = getattr(doc, "personal_number", "")

                if func == "SEMESTER":
                    entries.append(PayrollEntry(doc_id, name, pnr, ds, PayCode.SEMESTER, 8.0))
                    continue

                # Jour
                if is_jour(func):
                    is_primary = "JOUR_P" in func
                    if is_holiday:
                        code = PayCode.JOUR_HELG_PRIM if is_primary else PayCode.JOUR_HELG_BAK
                        entries.append(PayrollEntry(doc_id, name, pnr, ds, code, 8.0))
                        entries.append(PayrollEntry(doc_id, name, pnr, ds, PayCode.OB_STORHELG, 8.0))
                    elif is_weekend:
                        code = PayCode.JOUR_HELG_PRIM if is_primary else PayCode.JOUR_HELG_BAK
                        entries.append(PayrollEntry(doc_id, name, pnr, ds, code, 8.0))
                        entries.append(PayrollEntry(doc_id, name, pnr, ds, PayCode.OB_HELG, 8.0))
                    else:
                        code = PayCode.JOUR_PRIM if is_primary else PayCode.JOUR_BAK
                        entries.append(PayrollEntry(doc_id, name, pnr, ds, code, 8.0))
                        entries.append(PayrollEntry(doc_id, name, pnr, ds, PayCode.OB_KVALL, 4.0))
                        entries.append(PayrollEntry(doc_id, name, pnr, ds, PayCode.OB_NATT, 8.0))

                    # Komp intjänad
                    comp_hours = 8.0 if is_weekend or is_holiday else 4.0
                    entries.append(PayrollEntry(doc_id, name, pnr, ds, PayCode.KOMP_INTJANAD, comp_hours))

            current += timedelta(days=1)

        return entries

    def export_paxml(self, entries: list, employer_id: str = "COP", period: str = "") -> str:
        """Exportera till PAXml 2.0."""
        root = ET.Element("PAXml", version="2.0")
        header = ET.SubElement(root, "Header")
        ET.SubElement(header, "EmployerId").text = employer_id
        ET.SubElement(header, "Period").text = period

        employees = ET.SubElement(root, "Employees")
        by_doc = {}
        for e in entries:
            by_doc.setdefault(e.doctor_id, []).append(e)

        for doc_id, doc_entries in by_doc.items():
            emp = ET.SubElement(employees, "Employee", ssn=doc_entries[0].personal_number or "")
            ET.SubElement(emp, "Name").text = doc_entries[0].doctor_name
            for e in doc_entries:
                tx = ET.SubElement(emp, "Transaction")
                ET.SubElement(tx, "PayCode").text = e.pay_code.value
                ET.SubElement(tx, "Date").text = e.date
                ET.SubElement(tx, "Hours").text = str(e.hours)

        return ET.tostring(root, encoding="unicode", xml_declaration=True)

    def export_csv(self, entries: list) -> str:
        """CSV med semikolon."""
        lines = ["Läkare;Personnr;Datum;Löneart;Kod;Timmar"]
        for e in entries:
            name = PAY_CODE_NAMES.get(e.pay_code, e.pay_code.value)
            lines.append(f"{e.doctor_name};{e.personal_number};{e.date};{name};{e.pay_code.value};{e.hours}")
        return "\n".join(lines)

    def get_summary(self, entries: list) -> dict:
        """Sammanfattning per löneart."""
        by_code = {}
        by_doc = {}
        for e in entries:
            by_code[e.pay_code.value] = by_code.get(e.pay_code.value, 0) + e.hours
            if e.doctor_id not in by_doc:
                by_doc[e.doctor_id] = {"name": e.doctor_name, "total_hours": 0}
            by_doc[e.doctor_id]["total_hours"] += e.hours
        return {"by_pay_code": by_code, "by_doctor": by_doc, "total_entries": len(entries)}

    def validate(self, entries: list) -> list:
        """Validera löneunderlag."""
        errors = []
        for e in entries:
            if e.hours < 0:
                errors.append(f"{e.doctor_name} {e.date}: negativa timmar ({e.hours})")
            if not e.personal_number:
                errors.append(f"{e.doctor_name}: saknar personnummer")
        return errors


payroll_exporter = PayrollExporter()
