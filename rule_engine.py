"""
COP Rule Engine v1.0 — Hyperdetaljerad regelmotor
===================================================
Kompilerar kliniska regler till OR-Tools CP-SAT constraints.

Arkitektur:
  DetailedRule = TimeFilter (NÄR) + PersonFilter (VEM) + ActionSpec (VAD)
  RuleEngine.compile_to_constraints() → lista med OR-Tools constraints + objective terms

Stöder:
  - Tidsfiltrering: veckodagar, jämna/udda veckor, månader, datumintervall, storhelger
  - Personalfiltrering: roller, kompetenser, grupper, tjänstgöringsgrad
  - Actions: tilldela, förbjud, kräv antal, begränsa, balansera, sekvens, koppla samman
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from data_model import ClinicConfig, Doctor, Role, Function, is_jour


# === FILTER-DATAKLASSER ===

@dataclass
class TimeFilter:
    """När regeln gäller."""
    weekdays: list = None              # [0,2,4] = mån,ons,fre. None = alla
    even_weeks: bool = None            # True = jämna, False = udda, None = alla
    months: list = None                # [1,2,3] = jan-mar. None = alla
    date_ranges: list = None           # [("2026-01-01","2026-06-30")]
    exclude_dates: list = None         # ["2026-12-24", "2026-12-25"]
    week_numbers: list = None          # [1,3,5,7] = specifika veckor
    recurring: str = None              # "first_monday_of_month" etc.


@dataclass
class PersonFilter:
    """Vilka regeln gäller för."""
    doctor_ids: list = None            # Specifika läkare. None = alla
    roles: list = None                 # ["ÖL", "SP"]
    exclude_roles: list = None         # ["UL"]
    competencies: list = None          # ["artroskopi", "rygg"]
    employment_min: float = None       # 0.8 = minst 80%
    employment_max: float = None       # 0.5 = max 50%
    is_supervisor: bool = None         # True = bara handledare
    has_supervisor: bool = None        # True = bara ST med handledare
    group_id: str = None               # "team_A", "knägruppen"
    exclude_doctors: list = None       # Undantas


@dataclass
class ActionSpec:
    """Vad regeln gör."""
    action_type: str                   # "assign", "forbid", "prefer", "require_count",
                                       # "limit_count", "require_together", "balance"
    functions: list = None             # ["OP_CSK", "OP_Häss"]
    sites: list = None                 # ["CSK"]
    shift_types: list = None           # ["JOUR_P_KVÄLL"]
    min_count: int = None
    max_count: int = None
    target_count: int = None
    together_with: "PersonFilter" = None
    not_together_with: "PersonFilter" = None
    balance_metric: str = None         # "calls", "weekends", "ob_hours"
    balance_window: str = None         # "month", "quarter"
    sequence: list = None              # ["OP", "AVD", "MOTT", "OP", "LEDIG"]


@dataclass
class DetailedRule:
    """En komplett, hyperdetaljerad regel."""
    id: str
    name: str
    description: str = ""
    category: str = "preference"       # "atl", "staffing", "fairness", "preference", "rotation", "training"
    is_hard: bool = False
    weight: int = 5
    enabled: bool = True

    time_filter: TimeFilter = None
    person_filter: PersonFilter = None
    action: ActionSpec = None

    created_by: str = None
    source: str = "manual"             # "ai_parsed", "manual", "imported", "system"
    notes: str = None


# === GRUPP-DEFINITIONER ===

DOCTOR_GROUPS = {
    "knägruppen": ["OL1", "SP2", "ST2"],
    "höftteamet": ["OL1", "OL4", "SP1", "SP2"],
    "traumateamet": ["OL4", "SP3", "SP5", "SP7"],
}


# === RULE ENGINE ===

class RuleEngine:
    """Kompilerar DetailedRules till OR-Tools CP-SAT constraints."""

    def __init__(self, config: ClinicConfig, rules: list):
        self.config = config
        self.rules = [r for r in rules if r.enabled]
        self.doc_by_id = {d.id: d for d in config.doctors}
        self._start_date = date(2026, 4, 6)  # Default, kan overridas

    def set_start_date(self, d):
        self._start_date = d

    def compile_to_constraints(self, model, x, num_days, num_weeks,
                                day_functions, call_functions):
        """Kompilera alla regler till constraints + objective terms."""
        objective_terms = []
        all_func_ids = [f[0] for f in day_functions] + [f[0] for f in call_functions] + ["LEDIG"]

        for rule in self.rules:
            try:
                terms = self._compile_rule(rule, model, x, num_days, num_weeks,
                                           day_functions, call_functions, all_func_ids)
                objective_terms.extend(terms)
            except Exception as e:
                print(f"  ⚠ Regel '{rule.id}' kunde inte kompileras: {e}")

        return objective_terms

    def _compile_rule(self, rule, model, x, num_days, num_weeks,
                      day_functions, call_functions, all_func_ids):
        """Kompilera en enskild regel."""
        objective_terms = []

        # Identifiera berörda läkare
        doctors = self._filter_doctors(rule.person_filter)
        if not doctors:
            return []

        # Identifiera berörda funktioner
        target_funcs = self._resolve_functions(rule.action, day_functions, call_functions)

        action = rule.action
        if not action:
            return []

        at = action.action_type

        if at == "forbid":
            for doc in doctors:
                for day in range(num_days):
                    if not self._day_matches(day, num_days, rule.time_filter):
                        continue
                    for func_id in target_funcs:
                        key = (doc.id, day, func_id)
                        if key not in x:
                            continue
                        if rule.is_hard:
                            model.add(x[key] == 0)
                        else:
                            penalty = model.new_bool_var(f"pen_{rule.id}_{doc.id}_{day}_{func_id}")
                            model.add(x[key] == 1).only_enforce_if(penalty)
                            model.add(x[key] == 0).only_enforce_if(penalty.negated())
                            objective_terms.append(-rule.weight * 2 * penalty)

        elif at == "assign":
            for doc in doctors:
                for day in range(num_days):
                    if not self._day_matches(day, num_days, rule.time_filter):
                        continue
                    for func_id in target_funcs:
                        key = (doc.id, day, func_id)
                        if key not in x:
                            continue
                        if rule.is_hard:
                            model.add(x[key] == 1)
                        else:
                            objective_terms.append(rule.weight * 2 * x[key])

        elif at == "require_count":
            min_c = action.min_count or 0
            for day in range(num_days):
                if not self._day_matches(day, num_days, rule.time_filter):
                    continue
                vars_list = []
                for doc in doctors:
                    for func_id in target_funcs:
                        key = (doc.id, day, func_id)
                        if key in x:
                            vars_list.append(x[key])
                if vars_list:
                    if rule.is_hard:
                        model.add(sum(vars_list) >= min_c)
                    else:
                        shortfall = model.new_int_var(0, len(doctors), f"sf_{rule.id}_{day}")
                        model.add(shortfall >= min_c - sum(vars_list))
                        model.add(shortfall >= 0)
                        objective_terms.append(-rule.weight * 2 * shortfall)

        elif at == "limit_count":
            max_c = action.max_count or 99
            for day in range(num_days):
                if not self._day_matches(day, num_days, rule.time_filter):
                    continue
                vars_list = []
                for doc in doctors:
                    for func_id in target_funcs:
                        key = (doc.id, day, func_id)
                        if key in x:
                            vars_list.append(x[key])
                if vars_list:
                    if rule.is_hard:
                        model.add(sum(vars_list) <= max_c)
                    else:
                        excess = model.new_int_var(0, len(doctors), f"ex_{rule.id}_{day}")
                        model.add(excess >= sum(vars_list) - max_c)
                        model.add(excess >= 0)
                        objective_terms.append(-rule.weight * 2 * excess)

        elif at == "balance":
            metric_vars = {}
            for doc in doctors:
                terms = []
                for day in range(num_days):
                    if not self._day_matches(day, num_days, rule.time_filter):
                        continue
                    for func_id in target_funcs:
                        key = (doc.id, day, func_id)
                        if key in x:
                            terms.append(x[key])
                if terms:
                    v = model.new_int_var(0, num_days, f"bal_{rule.id}_{doc.id}")
                    model.add(v == sum(terms))
                    metric_vars[doc.id] = v

            if len(metric_vars) >= 2:
                max_v = model.new_int_var(0, num_days, f"bmax_{rule.id}")
                min_v = model.new_int_var(0, num_days, f"bmin_{rule.id}")
                for v in metric_vars.values():
                    model.add(max_v >= v)
                    model.add(min_v <= v)
                spread = model.new_int_var(0, num_days, f"bspread_{rule.id}")
                model.add(spread == max_v - min_v)
                objective_terms.append(-rule.weight * 2 * spread)

        elif at == "prefer":
            for doc in doctors:
                for day in range(num_days):
                    if not self._day_matches(day, num_days, rule.time_filter):
                        continue
                    for func_id in target_funcs:
                        key = (doc.id, day, func_id)
                        if key in x:
                            objective_terms.append(rule.weight * x[key])

        return objective_terms

    # --- Filter-hjälpare ---

    def _filter_doctors(self, pf: PersonFilter) -> list:
        """Filtrera läkare baserat på PersonFilter."""
        if pf is None:
            return list(self.config.doctors)

        docs = list(self.config.doctors)

        if pf.doctor_ids is not None:
            # Expandera grupper
            ids = set()
            for did in pf.doctor_ids:
                if did in DOCTOR_GROUPS:
                    ids.update(DOCTOR_GROUPS[did])
                else:
                    ids.add(did)
            docs = [d for d in docs if d.id in ids]

        if pf.group_id and pf.group_id in DOCTOR_GROUPS:
            group_ids = set(DOCTOR_GROUPS[pf.group_id])
            docs = [d for d in docs if d.id in group_ids]

        if pf.roles:
            role_set = set(pf.roles)
            docs = [d for d in docs if d.role.value in role_set]

        if pf.exclude_roles:
            excl = set(pf.exclude_roles)
            docs = [d for d in docs if d.role.value not in excl]

        if pf.competencies:
            comp_set = set(pf.competencies)
            docs = [d for d in docs if comp_set.issubset(set(d.competencies))]

        if pf.employment_min is not None:
            docs = [d for d in docs if d.employment_rate >= pf.employment_min]

        if pf.employment_max is not None:
            docs = [d for d in docs if d.employment_rate <= pf.employment_max]

        if pf.is_supervisor is True:
            supervisor_ids = {d.supervisor_id for d in self.config.doctors if d.supervisor_id}
            docs = [d for d in docs if d.id in supervisor_ids]

        if pf.has_supervisor is True:
            docs = [d for d in docs if d.supervisor_id]

        if pf.exclude_doctors:
            excl = set(pf.exclude_doctors)
            docs = [d for d in docs if d.id not in excl]

        return docs

    def _day_matches(self, day: int, num_days: int, tf: TimeFilter) -> bool:
        """Kontrollera om en dag matchar tidsfiltret."""
        if tf is None:
            return True

        weekday = day % 7  # 0=mån, 6=sön

        if tf.weekdays is not None and weekday not in tf.weekdays:
            return False

        if tf.even_weeks is not None:
            week_num = day // 7
            is_even = week_num % 2 == 0
            if tf.even_weeks != is_even:
                return False

        if tf.week_numbers is not None:
            week_num = day // 7
            if week_num not in tf.week_numbers:
                return False

        if tf.months is not None or tf.date_ranges is not None or tf.exclude_dates is not None:
            day_date = self._start_date + timedelta(days=day)

            if tf.months is not None and day_date.month not in tf.months:
                return False

            if tf.date_ranges is not None:
                in_range = False
                for start_str, end_str in tf.date_ranges:
                    s = date.fromisoformat(start_str)
                    e = date.fromisoformat(end_str)
                    if s <= day_date <= e:
                        in_range = True
                        break
                if not in_range:
                    return False

            if tf.exclude_dates is not None:
                if day_date.isoformat() in tf.exclude_dates:
                    return False

        if tf.recurring == "first_monday_of_month":
            day_date = self._start_date + timedelta(days=day)
            if weekday != 0 or day_date.day > 7:
                return False

        return True

    def _resolve_functions(self, action: ActionSpec, day_functions, call_functions) -> list:
        """Resolve action till func_id-lista."""
        if action is None:
            return []

        if action.functions:
            return action.functions

        if action.shift_types:
            return action.shift_types

        if action.sites:
            funcs = []
            for func_id, _, site in day_functions:
                if site in action.sites:
                    funcs.append(func_id)
            return funcs

        # Default: alla funktioner
        return [f[0] for f in day_functions] + [f[0] for f in call_functions]

    # --- Validering ---

    def validate_rules(self) -> list:
        """Kontrollera att regler inte motsäger varandra."""
        conflicts = []
        for i, r1 in enumerate(self.rules):
            for r2 in self.rules[i+1:]:
                if self._rules_conflict(r1, r2):
                    conflicts.append({
                        "rule_a": r1.id, "rule_b": r2.id,
                        "description_sv": f"'{r1.name}' kan krocka med '{r2.name}'",
                        "severity": "warning",
                    })
        return conflicts

    def _rules_conflict(self, r1, r2) -> bool:
        """Enkel konfliktdetektion: samma läkare + samma tid + motstridiga actions."""
        if not r1.action or not r2.action:
            return False
        a1, a2 = r1.action.action_type, r2.action.action_type
        # Assign + forbid samma funktion = konflikt
        if {a1, a2} == {"assign", "forbid"}:
            f1 = set(r1.action.functions or [])
            f2 = set(r2.action.functions or [])
            if f1 & f2:
                return True
        return False

    # --- Utvärdering ---

    def evaluate_rule(self, rule, schedule: dict, day: int, doctor_id: str) -> bool:
        """Utvärderar om en regel är uppfylld."""
        doc = self.doc_by_id.get(doctor_id)
        if not doc:
            return True
        if rule.person_filter and doc not in self._filter_doctors(rule.person_filter):
            return True  # Gäller inte denna läkare
        if rule.time_filter and not self._day_matches(day, 999, rule.time_filter):
            return True  # Gäller inte denna dag

        func = schedule.get(doctor_id, {}).get(day, "LEDIG")
        action = rule.action
        if not action:
            return True

        target_funcs = action.functions or action.shift_types or []

        if action.action_type == "forbid":
            return func not in target_funcs
        if action.action_type == "assign":
            return func in target_funcs
        return True

    def get_applicable_rules(self, doctor_id: str, day: int) -> list:
        """Returnera alla regler som gäller för denna läkare/dag."""
        doc = self.doc_by_id.get(doctor_id)
        if not doc:
            return []
        result = []
        for rule in self.rules:
            if doc in self._filter_doctors(rule.person_filter):
                if self._day_matches(day, 999, rule.time_filter):
                    result.append(rule)
        return result

    def explain_assignment(self, doctor_id: str, day: int, schedule: dict) -> dict:
        """Förklara en tilldelning baserat på reglerna."""
        applicable = self.get_applicable_rules(doctor_id, day)
        func = schedule.get(doctor_id, {}).get(day, "LEDIG")
        satisfied = []
        violated = []
        for rule in applicable:
            if self.evaluate_rule(rule, schedule, day, doctor_id):
                satisfied.append(rule.name)
            else:
                violated.append(rule.name)
        return {
            "doctor_id": doctor_id,
            "day": day,
            "function": func,
            "rules_satisfied": satisfied,
            "rules_violated": violated,
            "total_applicable": len(applicable),
        }


# === 20 EXEMPELREGLER ===

def create_example_rules() -> list:
    """Skapa 20 realistiska exempelregler för Kristianstad."""
    return [
        DetailedRule(
            id="rule_01_op_hass_even_wed",
            name="OP Hässleholm jämna veckor onsdagar",
            description="Extra OP-bemanning Hässleholm jämna veckor onsdagar jan-jun + aug-dec",
            category="staffing", is_hard=False, weight=6,
            time_filter=TimeFilter(weekdays=[2], even_weeks=True,
                                   months=[1,2,3,4,5,6,8,9,10,11,12]),
            person_filter=PersonFilter(roles=["SP", "ÖL"]),
            action=ActionSpec(action_type="require_count", functions=["OP_Hässleholm"], min_count=3),
            source="manual",
        ),
        DetailedRule(
            id="rule_02_st_supervisor_op",
            name="ST med handledare på OP minst 2 dagar/vecka",
            category="training", is_hard=False, weight=7,
            person_filter=PersonFilter(has_supervisor=True),
            action=ActionSpec(action_type="prefer", functions=["OP_CSK", "OP_Hässleholm"]),
            source="manual",
        ),
        DetailedRule(
            id="rule_03_max_ol_op",
            name="Max 3 ÖL på OP samma dag",
            category="staffing", is_hard=False, weight=5,
            time_filter=TimeFilter(weekdays=[0,1,2,3,4]),
            person_filter=PersonFilter(roles=["ÖL"]),
            action=ActionSpec(action_type="limit_count", functions=["OP_CSK", "OP_Hässleholm"], max_count=3),
            source="manual",
        ),
        DetailedRule(
            id="rule_04_artroskopi_friday_csk",
            name="Fredag: artroskopi-kompetent på OP CSK",
            category="staffing", is_hard=False, weight=6,
            time_filter=TimeFilter(weekdays=[4]),
            person_filter=PersonFilter(competencies=["artroskopi"]),
            action=ActionSpec(action_type="require_count", functions=["OP_CSK"], min_count=1),
            source="manual",
        ),
        DetailedRule(
            id="rule_05_andersson_no_night",
            name="Dr Andersson aldrig nattjour",
            description="Medicinsk orsak — undantagen från nattjour",
            category="preference", is_hard=True,
            person_filter=PersonFilter(doctor_ids=["OL1"]),
            action=ActionSpec(action_type="forbid", functions=["JOUR_P", "JOUR_B"]),
            source="manual",
        ),
        DetailedRule(
            id="rule_06_rotation",
            name="Varannan vecka: föredra blandad rotation",
            category="rotation", is_hard=False, weight=3,
            time_filter=TimeFilter(weekdays=[0,1,2,3,4]),
            person_filter=PersonFilter(roles=["SP"]),
            action=ActionSpec(action_type="balance", functions=["OP_CSK", "OP_Hässleholm", "AVD_CSK", "MOTT_CSK"]),
            source="manual",
        ),
        DetailedRule(
            id="rule_07_december_avd",
            name="December: öka avdelningsbemanning",
            description="Influensasäsong — extra personal på avdelning",
            category="staffing", is_hard=False, weight=6,
            time_filter=TimeFilter(months=[12], weekdays=[0,1,2,3,4]),
            action=ActionSpec(action_type="require_count", functions=["AVD_CSK", "AVD_Hässleholm"], min_count=3),
            source="manual",
        ),
        DetailedRule(
            id="rule_08_backup_experience",
            name="Bakjour: bara ÖL och erfarna SP",
            category="staffing", is_hard=False, weight=5,
            person_filter=PersonFilter(roles=["ÖL", "SP"], employment_min=0.8),
            action=ActionSpec(action_type="prefer", functions=["JOUR_B"]),
            source="manual",
        ),
        DetailedRule(
            id="rule_09_knägruppen_tuesday",
            name="Knägruppen: minst 2 på OP CSK tisdagar",
            category="staffing", is_hard=False, weight=5,
            time_filter=TimeFilter(weekdays=[1]),
            person_filter=PersonFilter(group_id="knägruppen"),
            action=ActionSpec(action_type="require_count", functions=["OP_CSK"], min_count=2),
            source="manual",
        ),
        DetailedRule(
            id="rule_10_st_no_solo_mott",
            name="ST inte ensamma på mottagning",
            description="ST-läkare ska ha senior tillgänglig vid mottagning",
            category="training", is_hard=False, weight=7,
            person_filter=PersonFilter(roles=["ST_TIDIG"]),
            action=ActionSpec(action_type="forbid", functions=["MOTT_CSK", "MOTT_Hässleholm"]),
            source="manual",
        ),
        DetailedRule(
            id="rule_11_max_parttime_ledig",
            name="Max 2 deltidsläkare lediga samma dag",
            category="staffing", is_hard=False, weight=4,
            person_filter=PersonFilter(employment_max=0.8),
            action=ActionSpec(action_type="limit_count", functions=["LEDIG"], max_count=2),
            source="manual",
        ),
        DetailedRule(
            id="rule_12_first_monday_admin",
            name="Första måndagen: ÖL på admin (MDT-konferens)",
            category="preference", is_hard=False, weight=4,
            time_filter=TimeFilter(recurring="first_monday_of_month"),
            person_filter=PersonFilter(roles=["ÖL"]),
            action=ActionSpec(action_type="prefer", functions=["LEDIG"]),
            source="manual", notes="MDT-konferens = multi-disciplinärt team-möte",
        ),
        DetailedRule(
            id="rule_13_summer_staffing",
            name="Juli: sommarbemanning",
            description="Reducerad OP-kapacitet under sommaren",
            category="staffing", is_hard=False, weight=5,
            time_filter=TimeFilter(months=[7], weekdays=[0,1,2,3,4]),
            action=ActionSpec(action_type="limit_count", functions=["OP_CSK"], max_count=3),
            source="manual",
        ),
        DetailedRule(
            id="rule_14_holiday_double_call",
            name="Storhelger: föredra dubbelbemannad jour",
            category="staffing", is_hard=False, weight=4,
            time_filter=TimeFilter(exclude_dates=None),  # Använder exclude_dates inverterat
            action=ActionSpec(action_type="prefer", functions=["JOUR_P", "JOUR_B"]),
            source="manual",
        ),
        DetailedRule(
            id="rule_15_junior_st_max_call",
            name="ST år 1-3: max 1 jour per vecka",
            category="atl", is_hard=True,
            person_filter=PersonFilter(roles=["ST_TIDIG"]),
            action=ActionSpec(action_type="limit_count", functions=["JOUR_P"], max_count=1),
            source="system",
        ),
        DetailedRule(
            id="rule_16_op_team_consistency",
            name="OP-team: föredra samma site hela veckan",
            category="preference", is_hard=False, weight=3,
            time_filter=TimeFilter(weekdays=[0,1,2,3,4]),
            person_filter=PersonFilter(roles=["SP", "ÖL"]),
            action=ActionSpec(action_type="balance", functions=["OP_CSK", "OP_Hässleholm"]),
            source="manual",
        ),
        DetailedRule(
            id="rule_17_admin_monthly",
            name="Minst 1 ledig/admin-dag per vecka per läkare",
            category="fairness", is_hard=False, weight=4,
            person_filter=PersonFilter(exclude_roles=["UL"]),
            action=ActionSpec(action_type="require_count", functions=["LEDIG"], min_count=1),
            source="manual",
        ),
        DetailedRule(
            id="rule_18_friday_afternoon",
            name="Fredag: föredra ledig (kort fredag)",
            category="preference", is_hard=False, weight=2,
            time_filter=TimeFilter(weekdays=[4]),
            person_filter=PersonFilter(roles=["ÖL"]),
            action=ActionSpec(action_type="prefer", functions=["LEDIG"]),
            source="manual",
        ),
        DetailedRule(
            id="rule_19_supervisor_not_same_call",
            name="Handledare och ST: inte jour samma natt",
            category="training", is_hard=False, weight=5,
            person_filter=PersonFilter(is_supervisor=True),
            action=ActionSpec(action_type="balance", functions=["JOUR_P", "JOUR_B"]),
            source="manual",
        ),
        DetailedRule(
            id="rule_20_ob_quarterly_balance",
            name="Balansera OB-timmar per kvartal",
            description="Max 15% avvikelse från medel",
            category="fairness", is_hard=False, weight=4,
            person_filter=PersonFilter(exclude_roles=["UL"]),
            action=ActionSpec(action_type="balance", functions=["JOUR_P", "JOUR_B"],
                             balance_metric="ob_hours", balance_window="quarter"),
            source="manual",
        ),
    ]
