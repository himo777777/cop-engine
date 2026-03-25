"""
COP Frånvarokedja v1.0 — Automatisk ersättarkedja
===================================================
Hela flödet vid oplanerad frånvaro (sjukdom, VAB, etc.):

  1. REGISTRERA  — Frånvaron loggas med typ, tid, påverkade dagar
  2. ANALYSERA   — Identifiera vilka funktioner som blir vakanta
  3. RANKA       — Hitta ersättarkandidater, poängsätt och ranka
  4. VALIDERA    — Kontrollera ATL-regler för vald ersättare
  5. VERKSTÄLL   — Uppdatera schemat
  6. NOTIFIERA   — Skicka notifieringar till berörda

Rankningskriterier (ersättare):
  - Har rätt kompetens för funktionen          +30p
  - Är LEDIG den aktuella dagen                +25p
  - Samma roll som frånvarande                 +20p
  - Lägst arbetsbelastning (normaliserat)      +15p
  - Samma site preference                      +10p
  - Ingen jour igår eller imorgon (ATL-säker)  +20p
  - Handledare för ST (om OP-dag)              +15p
  - Undvik: redan haft jour samma vecka         -50p
  - Undvik: deltid och redan fullt              -30p
"""

import uuid
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
from collections import defaultdict

from data_model import (
    ClinicConfig, Role, Doctor, Function, ShiftType,
)


# === ENUMS & DATAKLASSER ===

class AbsenceType(Enum):
    SJUK = "sjuk"
    VAB = "vab"
    SEMESTER = "semester"
    UTBILDNING = "utbildning"
    KONFERENS = "konferens"
    PERMISSION = "permission"
    AKUT = "akut"  # Akut frånvaro, okänd anledning


class ChainStatus(Enum):
    REGISTERED = "registered"
    ANALYZING = "analyzing"
    CANDIDATES_FOUND = "candidates_found"
    REPLACEMENT_SELECTED = "replacement_selected"
    ATL_VALIDATED = "atl_validated"
    ATL_VIOLATION = "atl_violation"
    SCHEDULE_UPDATED = "schedule_updated"
    NOTIFIED = "notified"
    COMPLETED = "completed"
    FAILED = "failed"
    MANUAL_REQUIRED = "manual_required"


class Severity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class VacantSlot:
    """En vakant position som behöver fyllas."""
    day_index: int
    day_date: date
    function: str          # T.ex. "OP_H", "JOUR_P", "AVD_C"
    is_call: bool          # Jour = kritisk
    site: Optional[str]    # Härlett från funktionsnamn
    weekday: int           # 0=mån, 6=sön


@dataclass
class Candidate:
    """En möjlig ersättare med poäng."""
    doctor_id: str
    doctor_name: str
    role: str
    score: float
    current_function: str  # Vad kandidaten gör den dagen just nu
    reasons: list[str]     # Motiveringar för poängen
    atl_ok: bool = True    # Klarar ATL-validering?
    atl_warnings: list[str] = field(default_factory=list)


@dataclass
class ChainStep:
    """Ett steg i kedjan med tidsstämpel."""
    step: int
    action: str
    status: ChainStatus
    timestamp: str
    details: dict = field(default_factory=dict)


@dataclass
class AbsenceChainResult:
    """Komplett resultat från en frånvarokedja."""
    chain_id: str
    absence_type: str
    doctor_id: str
    doctor_name: str
    start_date: str
    end_date: str
    status: ChainStatus
    vacant_slots: list[dict]
    replacements: list[dict]      # Valda ersättare per vakant slot
    failed_slots: list[dict]      # Slots som inte kunde fyllas
    chain_log: list[dict]         # Alla steg i kedjan
    notifications: list[dict]     # Skickade notifieringar
    atl_violations: list[dict]    # ATL-problem
    schedule_changes: list[dict]  # Faktiska schemaändringar


# === FRÅNVAROKEDJA ===

class AbsenceChain:
    """
    Orkestrerar hela frånvarokedjan.

    Användning:
        chain = AbsenceChain(config, schedule_data)
        result = chain.execute(doctor_id, absence_type, start_date, end_date)
    """

    # Poängvikter
    SCORE_COMPETENCE = 30
    SCORE_IS_FREE = 25
    SCORE_SAME_ROLE = 20
    SCORE_LOW_WORKLOAD = 15
    SCORE_SAME_SITE = 10
    SCORE_ATL_SAFE = 20
    SCORE_SUPERVISOR_MATCH = 15
    PENALTY_CALL_SAME_WEEK = -50
    PENALTY_PARTTIME_FULL = -30

    def __init__(self, config: ClinicConfig, raw_schedule: dict,
                 schedule_start_date: date, num_weeks: int):
        self.config = config
        self.schedule = raw_schedule  # {doctor_id: {day_index: function}}
        self.schedule_start = schedule_start_date
        self.num_weeks = num_weeks
        self.num_days = num_weeks * 7
        self.doc_by_id = {d.id: d for d in config.doctors}

        # Build dynamic function→site and function→roles mappings from config
        self.FUNCTION_SITE = {}
        self.FUNCTION_ROLES = {}

        # OP per site (from operating rooms)
        for site in sorted(set(r.site for r in config.operating_rooms)):
            func_id = f"OP_{site}"
            self.FUNCTION_SITE[func_id] = site
            self.FUNCTION_ROLES[func_id] = [Role.ÖVERLÄKARE, Role.SPECIALIST, Role.ST_SEN, Role.ST_TIDIG]

        # AVD and MOTT per site
        for site in config.sites:
            avd_id = f"AVD_{site}"
            mott_id = f"MOTT_{site}"
            self.FUNCTION_SITE[avd_id] = site
            self.FUNCTION_SITE[mott_id] = site
            self.FUNCTION_ROLES[avd_id] = [Role.SPECIALIST, Role.ST_SEN, Role.ST_TIDIG, Role.UNDERLÄKARE]
            self.FUNCTION_ROLES[mott_id] = [Role.ÖVERLÄKARE, Role.SPECIALIST, Role.ST_SEN]

        # Override from staffing requirements if available
        for req in config.staffing_requirements:
            if req.function in (Function.PRIMÄRJOUR, Function.BAKJOUR, Function.OPERATION):
                continue
            func_id = f"{req.function.value}_{req.site}"
            if req.required_roles:
                self.FUNCTION_ROLES[func_id] = list(req.required_roles)

        # Jour (universal)
        self.FUNCTION_SITE["JOUR_P"] = config.sites[0] if config.sites else None
        self.FUNCTION_SITE["JOUR_B"] = config.sites[0] if config.sites else None
        self.FUNCTION_ROLES["JOUR_P"] = list(config.call_structure.primary_roles)
        self.FUNCTION_ROLES["JOUR_B"] = list(config.call_structure.backup_roles)
        self.chain_log: list[ChainStep] = []
        self.notifications: list[dict] = []
        self.webhook_urls: list[str] = []

    def _log(self, step: int, action: str, status: ChainStatus, details: dict = None):
        """Logga ett steg i kedjan."""
        entry = ChainStep(
            step=step,
            action=action,
            status=status,
            timestamp=datetime.now().isoformat(),
            details=details or {},
        )
        self.chain_log.append(entry)

    def execute(self, doctor_id: str, absence_type: str,
                start_date: str, end_date: str,
                auto_select: bool = True) -> AbsenceChainResult:
        """
        Kör hela frånvarokedjan.

        Args:
            doctor_id: Frånvarande läkares ID
            absence_type: Typ av frånvaro
            start_date: Startdatum (YYYY-MM-DD)
            end_date: Slutdatum (YYYY-MM-DD)
            auto_select: Välj bästa ersättare automatiskt (True) eller returnera kandidatlista (False)
        """
        chain_id = f"chain_{uuid.uuid4().hex[:10]}"
        doc = self.doc_by_id.get(doctor_id)

        if not doc:
            return self._fail_result(chain_id, doctor_id, absence_type,
                                     start_date, end_date, f"Läkare {doctor_id} finns inte")

        abs_start = date.fromisoformat(start_date)
        abs_end = date.fromisoformat(end_date)

        # === STEG 1: REGISTRERA ===
        self._log(1, "Registrerar frånvaro", ChainStatus.REGISTERED, {
            "doctor": f"{doc.name} ({doc.id})",
            "type": absence_type,
            "period": f"{start_date} → {end_date}",
            "days": (abs_end - abs_start).days + 1,
        })

        # === STEG 2: ANALYSERA — hitta vakanta slots ===
        self._log(2, "Analyserar vakanta positioner", ChainStatus.ANALYZING)
        vacant_slots = self._find_vacant_slots(doctor_id, abs_start, abs_end)

        if not vacant_slots:
            self._log(2, "Inga vakanta positioner (alla dagar redan LEDIG)", ChainStatus.COMPLETED)
            return AbsenceChainResult(
                chain_id=chain_id, absence_type=absence_type,
                doctor_id=doctor_id, doctor_name=doc.name,
                start_date=start_date, end_date=end_date,
                status=ChainStatus.COMPLETED,
                vacant_slots=[], replacements=[], failed_slots=[],
                chain_log=[self._step_to_dict(s) for s in self.chain_log],
                notifications=[], atl_violations=[], schedule_changes=[],
            )

        self._log(2, f"Hittade {len(vacant_slots)} vakanta positioner", ChainStatus.CANDIDATES_FOUND, {
            "vacant": [{"day": v.day_index, "date": v.day_date.isoformat(),
                        "function": v.function, "critical": v.is_call} for v in vacant_slots],
            "critical_count": sum(1 for v in vacant_slots if v.is_call),
        })

        # Sortera: jourer först (mest kritiskt)
        vacant_slots.sort(key=lambda v: (not v.is_call, v.day_index))

        # === STEG 3-5: FÖR VARJE VAKANT SLOT ===
        replacements = []
        failed_slots = []
        schedule_changes = []
        atl_violations = []

        for slot in vacant_slots:
            # STEG 3: RANKA kandidater
            candidates = self._rank_candidates(slot, doctor_id)

            if not candidates:
                failed_slots.append({
                    "day": slot.day_index,
                    "date": slot.day_date.isoformat(),
                    "function": slot.function,
                    "reason": "Inga kvalificerade ersättare tillgängliga",
                })
                self._log(3, f"Dag {slot.day_index} ({slot.function}): Ingen ersättare",
                          ChainStatus.MANUAL_REQUIRED)
                continue

            if not auto_select:
                # Returnera kandidatlista utan att välja
                replacements.append({
                    "day": slot.day_index,
                    "date": slot.day_date.isoformat(),
                    "function": slot.function,
                    "candidates": [self._candidate_to_dict(c) for c in candidates[:5]],
                    "selected": None,
                })
                continue

            # STEG 4: VALIDERA bästa kandidaten
            selected = None
            for candidate in candidates:
                atl_result = self._validate_atl(candidate, slot)
                if atl_result["ok"]:
                    selected = candidate
                    break
                else:
                    atl_violations.append({
                        "candidate": candidate.doctor_id,
                        "day": slot.day_index,
                        "violations": atl_result["violations"],
                    })

            if not selected:
                # Ingen klarar ATL — ta bästa och flagga
                selected = candidates[0]
                selected.atl_ok = False
                selected.atl_warnings.append("⚠️ ATL-dispens kan krävas")
                self._log(4, f"Dag {slot.day_index}: ATL-varning för {selected.doctor_name}",
                          ChainStatus.ATL_VIOLATION)

            # STEG 5: VERKSTÄLL — uppdatera schemat
            old_func = self.schedule.get(selected.doctor_id, {}).get(slot.day_index, "LEDIG")

            # Uppdatera frånvarande → LEDIG
            if doctor_id in self.schedule:
                self.schedule[doctor_id][slot.day_index] = "LEDIG"

            # Uppdatera ersättare → ny funktion
            if selected.doctor_id in self.schedule:
                self.schedule[selected.doctor_id][slot.day_index] = slot.function

            change = {
                "day": slot.day_index,
                "date": slot.day_date.isoformat(),
                "function": slot.function,
                "absent_doctor": doctor_id,
                "replacement_doctor": selected.doctor_id,
                "replacement_name": selected.doctor_name,
                "replacement_old_function": old_func,
                "score": selected.score,
                "atl_ok": selected.atl_ok,
            }
            schedule_changes.append(change)

            replacements.append({
                "day": slot.day_index,
                "date": slot.day_date.isoformat(),
                "function": slot.function,
                "candidates": [self._candidate_to_dict(c) for c in candidates[:3]],
                "selected": self._candidate_to_dict(selected),
            })

            self._log(5, f"Dag {slot.day_index} ({slot.function}): {selected.doctor_name} ersätter",
                      ChainStatus.SCHEDULE_UPDATED, change)

        # === STEG 6: NOTIFIERA ===
        self._generate_notifications(doc, schedule_changes, failed_slots)
        self._log(6, f"Skickat {len(self.notifications)} notifieringar", ChainStatus.NOTIFIED)

        # === SLUTSTATUS ===
        if failed_slots:
            final_status = ChainStatus.MANUAL_REQUIRED
        elif atl_violations:
            final_status = ChainStatus.ATL_VIOLATION
        else:
            final_status = ChainStatus.COMPLETED

        self._log(7, "Kedja avslutad", final_status, {
            "replaced": len(schedule_changes),
            "failed": len(failed_slots),
            "atl_issues": len(atl_violations),
        })

        return AbsenceChainResult(
            chain_id=chain_id,
            absence_type=absence_type,
            doctor_id=doctor_id,
            doctor_name=doc.name,
            start_date=start_date,
            end_date=end_date,
            status=final_status,
            vacant_slots=[{"day": v.day_index, "date": v.day_date.isoformat(),
                          "function": v.function, "critical": v.is_call} for v in vacant_slots],
            replacements=replacements,
            failed_slots=failed_slots,
            chain_log=[self._step_to_dict(s) for s in self.chain_log],
            notifications=self.notifications,
            atl_violations=atl_violations,
            schedule_changes=schedule_changes,
        )

    # === INTERNMETODER ===

    def _find_vacant_slots(self, doctor_id: str, abs_start: date, abs_end: date) -> list[VacantSlot]:
        """Hitta alla positioner som blir vakanta."""
        slots = []
        doc_schedule = self.schedule.get(doctor_id, {})

        for day_idx in range(self.num_days):
            day_date = self.schedule_start + timedelta(days=day_idx)
            if day_date < abs_start or day_date > abs_end:
                continue

            func = doc_schedule.get(day_idx, "LEDIG")
            if func == "LEDIG":
                continue  # Redan ledig, inget att ersätta

            is_call = func in ("JOUR_P", "JOUR_B")
            site = self.FUNCTION_SITE.get(func)
            weekday = day_date.weekday()

            slots.append(VacantSlot(
                day_index=day_idx,
                day_date=day_date,
                function=func,
                is_call=is_call,
                site=site,
                weekday=weekday,
            ))

        return slots

    def _rank_candidates(self, slot: VacantSlot, absent_id: str) -> list[Candidate]:
        """Ranka alla möjliga ersättare för en vakant slot."""
        candidates = []

        # Vilka roller kan utföra funktionen?
        allowed_roles = self.FUNCTION_ROLES.get(slot.function, [])

        for doc in self.config.doctors:
            if doc.id == absent_id:
                continue  # Hoppa över den frånvarande

            # Rollfilter
            if doc.role not in allowed_roles:
                continue

            # Jourspecifikt: kolla can_primary_call / can_backup_call
            if slot.function == "JOUR_P" and not doc.can_primary_call:
                continue
            if slot.function == "JOUR_B" and not doc.can_backup_call:
                continue
            if doc.exempt_from_call and slot.is_call:
                continue

            # Poängsättning
            score = 0.0
            reasons = []

            current_func = self.schedule.get(doc.id, {}).get(slot.day_index, "LEDIG")

            # 1. Är LEDIG? (bäst — ingen kaskadeffekt)
            if current_func == "LEDIG":
                score += self.SCORE_IS_FREE
                reasons.append(f"+{self.SCORE_IS_FREE} ledig denna dag")

            # 2. Kompetens matchar
            absent_doc = self.doc_by_id[absent_id]
            if slot.function.startswith("OP_"):
                # OP kräver rätt kompetenser
                shared = set(doc.competencies) & set(absent_doc.competencies)
                if shared:
                    score += self.SCORE_COMPETENCE
                    reasons.append(f"+{self.SCORE_COMPETENCE} matchande kompetens: {', '.join(list(shared)[:3])}")

            # 3. Samma roll
            if doc.role == absent_doc.role:
                score += self.SCORE_SAME_ROLE
                reasons.append(f"+{self.SCORE_SAME_ROLE} samma roll ({doc.role.value})")

            # 4. Låg arbetsbelastning
            work_days = sum(1 for d in range(self.num_days)
                          if self.schedule.get(doc.id, {}).get(d, "LEDIG") != "LEDIG")
            max_possible = self.num_days * doc.employment_rate
            utilization = work_days / max_possible if max_possible > 0 else 1.0
            workload_bonus = self.SCORE_LOW_WORKLOAD * (1 - utilization)
            score += workload_bonus
            reasons.append(f"+{workload_bonus:.1f} belastning ({utilization:.0%})")

            # 5. Site preference
            if slot.site and doc.site_preference:
                if doc.site_preference == slot.site:
                    score += self.SCORE_SAME_SITE
                    reasons.append(f"+{self.SCORE_SAME_SITE} föredrar {slot.site}")

            # 6. ATL-säker (ingen jour igår/imorgon)
            yesterday_func = self.schedule.get(doc.id, {}).get(slot.day_index - 1, "LEDIG") if slot.day_index > 0 else "LEDIG"
            tomorrow_func = self.schedule.get(doc.id, {}).get(slot.day_index + 1, "LEDIG") if slot.day_index < self.num_days - 1 else "LEDIG"

            if yesterday_func not in ("JOUR_P", "JOUR_B") and tomorrow_func not in ("JOUR_P", "JOUR_B"):
                score += self.SCORE_ATL_SAFE
                reasons.append(f"+{self.SCORE_ATL_SAFE} ATL-säker (ingen angränsande jour)")

            # 7. Handledare-matchning (ST på OP)
            if slot.function.startswith("OP_"):
                # Kolla om denna doc är handledare för någon ST som redan är på OP samma dag
                for st_doc in self.config.doctors:
                    if st_doc.supervisor_id == doc.id:
                        st_func = self.schedule.get(st_doc.id, {}).get(slot.day_index, "LEDIG")
                        if st_func == slot.function:
                            score += self.SCORE_SUPERVISOR_MATCH
                            reasons.append(f"+{self.SCORE_SUPERVISOR_MATCH} handledare för {st_doc.name}")
                            break

            # 8. Straff: jour samma vecka
            week_start = (slot.day_index // 7) * 7
            week_calls = sum(1 for d in range(week_start, min(week_start + 7, self.num_days))
                           if self.schedule.get(doc.id, {}).get(d) in ("JOUR_P", "JOUR_B"))
            if week_calls > 0 and slot.is_call:
                score += self.PENALTY_CALL_SAME_WEEK
                reasons.append(f"{self.PENALTY_CALL_SAME_WEEK} redan jour denna vecka")

            # 9. Straff: deltid och redan fullt
            if doc.employment_rate < 1.0:
                expected_days = self.num_days * doc.employment_rate * (5/7)
                if work_days >= expected_days:
                    score += self.PENALTY_PARTTIME_FULL
                    reasons.append(f"{self.PENALTY_PARTTIME_FULL} deltid redan fullbelagd")

            candidates.append(Candidate(
                doctor_id=doc.id,
                doctor_name=doc.name,
                role=doc.role.value,
                score=round(score, 1),
                current_function=current_func,
                reasons=reasons,
            ))

        # Sortera fallande poäng
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    def _validate_atl(self, candidate: Candidate, slot: VacantSlot) -> dict:
        """Validera ATL-regler för en kandidat på en specifik slot."""
        violations = []
        doc_id = candidate.doctor_id

        # 1. Dygnsvila: 11h efter jour
        if slot.day_index > 0:
            yesterday = self.schedule.get(doc_id, {}).get(slot.day_index - 1, "LEDIG")
            if yesterday in ("JOUR_P", "JOUR_B") and slot.function not in ("LEDIG", "JOUR_P", "JOUR_B"):
                violations.append("Bryter 11h dygnsvila (jour igår)")

        # 2. Dygnsvila: om vi sätter jour, kolla imorgon
        if slot.is_call and slot.day_index < self.num_days - 1:
            tomorrow = self.schedule.get(doc_id, {}).get(slot.day_index + 1, "LEDIG")
            if tomorrow not in ("LEDIG", "JOUR_P", "JOUR_B"):
                violations.append("Bryter 11h dygnsvila (arbete imorgon efter jour)")

        # 3. Max 1 jour per vecka
        if slot.is_call:
            week_start = (slot.day_index // 7) * 7
            week_calls = sum(1 for d in range(week_start, min(week_start + 7, self.num_days))
                           if self.schedule.get(doc_id, {}).get(d) in ("JOUR_P", "JOUR_B"))
            if week_calls >= 1:
                violations.append(f"Max 1 jour/vecka ({week_calls} redan denna vecka)")

        # 4. Max 5 arbetsdagar per vecka
        week_start = (slot.day_index // 7) * 7
        work_days = sum(1 for d in range(week_start, min(week_start + 7, self.num_days))
                       if self.schedule.get(doc_id, {}).get(d, "LEDIG") != "LEDIG")
        if work_days >= 5 and candidate.current_function == "LEDIG":
            violations.append(f"Max 5 arbetsdagar/vecka ({work_days} redan)")

        # 5. Veckovila: minst 36h sammanhängande per 7 dagar
        # Förenklad kontroll: minst 1 LEDIG-dag per vecka
        week_free = sum(1 for d in range(week_start, min(week_start + 7, self.num_days))
                       if self.schedule.get(doc_id, {}).get(d, "LEDIG") == "LEDIG")
        # Om vi tar bort en LEDIG-dag (ersättaren var ledig)
        if candidate.current_function == "LEDIG" and week_free <= 2:
            violations.append(f"Risk för bruten veckovila (bara {week_free} lediga dagar kvar)")

        return {"ok": len(violations) == 0, "violations": violations}

    def _generate_notifications(self, absent_doc: Doctor,
                                 changes: list[dict], failed: list[dict]):
        """Generera notifieringar till berörda."""
        # 1. Bekräftelse till frånvarande
        self.notifications.append({
            "type": "absence_confirmed",
            "to": absent_doc.id,
            "to_name": absent_doc.name,
            "severity": "info",
            "message": f"Din frånvaro är registrerad. {len(changes)} pass har fått ersättare.",
            "timestamp": datetime.now().isoformat(),
        })

        # 2. Notifiera varje ersättare
        for change in changes:
            self.notifications.append({
                "type": "replacement_assigned",
                "to": change["replacement_doctor"],
                "to_name": change["replacement_name"],
                "severity": "warning" if not change["atl_ok"] else "info",
                "message": (
                    f"Du har tilldelats {change['function']} den {change['date']} "
                    f"som ersättare för {absent_doc.name}. "
                    f"{'⚠️ ATL-dispens kan krävas.' if not change['atl_ok'] else ''}"
                ),
                "details": {
                    "old_function": change["replacement_old_function"],
                    "new_function": change["function"],
                    "date": change["date"],
                },
                "timestamp": datetime.now().isoformat(),
            })

        # 3. Notifiera schemaläggare om misslyckade slots
        if failed:
            self.notifications.append({
                "type": "manual_action_required",
                "to": "schemaläggare",
                "to_name": "Schemaläggare",
                "severity": "critical",
                "message": (
                    f"⚠️ {len(failed)} positioner kunde inte fyllas automatiskt "
                    f"vid frånvaro av {absent_doc.name}. Manuell hantering krävs."
                ),
                "details": {"failed_slots": failed},
                "timestamp": datetime.now().isoformat(),
            })

        # 4. Notifiera schemaläggare om ATL-varningar
        atl_issues = [c for c in changes if not c["atl_ok"]]
        if atl_issues:
            self.notifications.append({
                "type": "atl_warning",
                "to": "schemaläggare",
                "to_name": "Schemaläggare",
                "severity": "warning",
                "message": (
                    f"⚠️ {len(atl_issues)} ersättningar har ATL-varningar. "
                    f"Kontrollera och godkänn."
                ),
                "details": {"issues": atl_issues},
                "timestamp": datetime.now().isoformat(),
            })

    def _step_to_dict(self, step: ChainStep) -> dict:
        return {
            "step": step.step,
            "action": step.action,
            "status": step.status.value,
            "timestamp": step.timestamp,
            "details": step.details,
        }

    def _candidate_to_dict(self, c: Candidate) -> dict:
        return {
            "doctor_id": c.doctor_id,
            "doctor_name": c.doctor_name,
            "role": c.role,
            "score": c.score,
            "current_function": c.current_function,
            "reasons": c.reasons,
            "atl_ok": c.atl_ok,
            "atl_warnings": c.atl_warnings,
        }

    def _fail_result(self, chain_id, doctor_id, absence_type, start_date, end_date, error) -> AbsenceChainResult:
        self._log(0, f"FEL: {error}", ChainStatus.FAILED)
        return AbsenceChainResult(
            chain_id=chain_id, absence_type=absence_type,
            doctor_id=doctor_id, doctor_name="?",
            start_date=start_date, end_date=end_date,
            status=ChainStatus.FAILED,
            vacant_slots=[], replacements=[], failed_slots=[],
            chain_log=[self._step_to_dict(s) for s in self.chain_log],
            notifications=[], atl_violations=[], schedule_changes=[],
        )


# === DEMO ===

def run_demo():
    """Testa frånvarokedjan med exempeldata."""
    from data_model import create_kristianstad_example
    from solver import solve_schedule

    print("=" * 70)
    print("COP FRÅNVAROKEDJA — DEMO")
    print("=" * 70)

    # 1. Skapa config och generera schema
    print("\n📋 Genererar schema...")
    config = create_kristianstad_example()
    schedule = solve_schedule(config, num_weeks=2, time_limit_seconds=30)

    if not schedule:
        print("❌ Kunde inte generera schema")
        return

    start_date = date(2026, 4, 6)  # Måndag
    print(f"✅ Schema genererat: {start_date} → {start_date + timedelta(weeks=2)}")

    # 2. Visa SP1:s schema innan frånvaro
    sp1_sched = schedule.get("SP1", {})
    print(f"\n👤 SP1 (Dr Fredriksson) schema före frånvaro:")
    for d in range(14):
        day_date = start_date + timedelta(days=d)
        func = sp1_sched.get(d, "LEDIG")
        marker = " 🔴" if func in ("JOUR_P", "JOUR_B") else ""
        print(f"   {day_date.strftime('%a %d/%m')}: {func}{marker}")

    # 3. Simulera sjukdom mån-ons (dag 0-2)
    print(f"\n🤒 SP1 sjukanmäler sig 6-8 april (mån-ons)...")
    print("-" * 50)

    chain = AbsenceChain(config, schedule, start_date, num_weeks=2)
    result = chain.execute("SP1", "sjuk", "2026-04-06", "2026-04-08")

    # 4. Visa resultat
    print(f"\n📊 RESULTAT: {result.status.value}")
    print(f"   Vakanta positioner: {len(result.vacant_slots)}")
    print(f"   Ersatta: {len(result.schedule_changes)}")
    print(f"   Misslyckade: {len(result.failed_slots)}")
    print(f"   ATL-varningar: {len(result.atl_violations)}")

    print("\n📝 KEDJELOGG:")
    for step in result.chain_log:
        icon = {"completed": "✅", "failed": "❌", "manual_required": "⚠️",
                "atl_violation": "🚨"}.get(step["status"], "🔄")
        print(f"   {icon} Steg {step['step']}: {step['action']}")

    print("\n🔄 ERSÄTTNINGAR:")
    for change in result.schedule_changes:
        print(f"   {change['date']} | {change['function']}")
        print(f"     {change['absent_doctor']} → {change['replacement_name']} "
              f"(poäng: {change['score']}, ATL: {'✅' if change['atl_ok'] else '⚠️'})")
        print(f"     Ersättarens gamla: {change['replacement_old_function']}")

    if result.replacements:
        print("\n🏆 KANDIDATRANKING (första slot):")
        first = result.replacements[0]
        for i, cand in enumerate(first.get("candidates", [])[:5]):
            marker = " ← VALD" if cand == first.get("selected") else ""
            print(f"   {i+1}. {cand['doctor_name']} ({cand['role']}) — {cand['score']}p{marker}")
            for r in cand["reasons"][:3]:
                print(f"      {r}")

    if result.failed_slots:
        print("\n⚠️ KUNDE EJ ERSÄTTAS:")
        for slot in result.failed_slots:
            print(f"   {slot['date']} | {slot['function']} — {slot['reason']}")

    print("\n📬 NOTIFIERINGAR:")
    for notif in result.notifications:
        icon = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(notif["severity"], "📬")
        print(f"   {icon} Till {notif['to_name']}: {notif['message'][:80]}")

    # 5. Visa uppdaterat schema
    print(f"\n👤 SP1 schema EFTER frånvarokedjan:")
    for d in range(14):
        day_date = start_date + timedelta(days=d)
        func = schedule.get("SP1", {}).get(d, "LEDIG")
        print(f"   {day_date.strftime('%a %d/%m')}: {func}")


if __name__ == "__main__":
    run_demo()
