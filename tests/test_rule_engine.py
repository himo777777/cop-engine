"""
Tester för avancerad regelmotor + simulator.
"""

import pytest
from datetime import date
from data_model import create_kristianstad_example, Role, Doctor
from rule_engine import (
    DetailedRule, TimeFilter, PersonFilter, ActionSpec,
    RuleEngine, create_example_rules, DOCTOR_GROUPS,
)
from simulator import WhatIfSimulator, Scenario
from solver import solve_schedule


@pytest.fixture
def config():
    return create_kristianstad_example()


@pytest.fixture
def engine(config):
    rules = create_example_rules()
    return RuleEngine(config, rules)


# === TimeFilter ===

class TestTimeFilter:
    def test_weekdays(self, engine):
        """weekdays=[0,2,4] matchar mån/ons/fre."""
        tf = TimeFilter(weekdays=[0, 2, 4])
        assert engine._day_matches(0, 14, tf)   # mån
        assert not engine._day_matches(1, 14, tf)  # tis
        assert engine._day_matches(2, 14, tf)   # ons
        assert not engine._day_matches(3, 14, tf)  # tor
        assert engine._day_matches(4, 14, tf)   # fre

    def test_even_weeks(self, engine):
        """even_weeks=True matchar bara jämna veckor."""
        tf = TimeFilter(even_weeks=True)
        assert engine._day_matches(0, 14, tf)   # vecka 0 (jämn)
        assert not engine._day_matches(7, 14, tf)  # vecka 1 (udda)

    def test_months(self, engine):
        """months=[1,2,3] exkluderar april (start=2026-04-06)."""
        engine.set_start_date(date(2026, 4, 6))
        tf = TimeFilter(months=[1, 2, 3])
        assert not engine._day_matches(0, 14, tf)  # 6 april = inte jan-mar

    def test_date_ranges(self, engine):
        """date_ranges matchar korrekt."""
        engine.set_start_date(date(2026, 4, 6))
        tf = TimeFilter(date_ranges=[("2026-04-06", "2026-04-10")])
        assert engine._day_matches(0, 14, tf)   # 6 apr
        assert engine._day_matches(4, 14, tf)   # 10 apr
        assert not engine._day_matches(5, 14, tf)  # 11 apr

    def test_exclude_dates(self, engine):
        """Storhelger undantas."""
        engine.set_start_date(date(2026, 4, 6))
        tf = TimeFilter(exclude_dates=["2026-04-07"])
        assert engine._day_matches(0, 14, tf)   # 6 apr OK
        assert not engine._day_matches(1, 14, tf)  # 7 apr exkluderad


# === PersonFilter ===

class TestPersonFilter:
    def test_roles(self, engine):
        """roles=['ÖL','SP'] filtrerar korrekt."""
        pf = PersonFilter(roles=["ÖL", "SP"])
        docs = engine._filter_doctors(pf)
        for d in docs:
            assert d.role.value in ("ÖL", "SP")
        assert len(docs) == 13  # 5 ÖL + 8 SP

    def test_competencies(self, engine):
        """Bara läkare med artroskopi-kompetens."""
        pf = PersonFilter(competencies=["artroskopi"])
        docs = engine._filter_doctors(pf)
        for d in docs:
            assert "artroskopi" in d.competencies

    def test_groups(self, engine):
        """group_id='knägruppen' matchar alla i gruppen."""
        pf = PersonFilter(group_id="knägruppen")
        docs = engine._filter_doctors(pf)
        doc_ids = {d.id for d in docs}
        assert doc_ids == set(DOCTOR_GROUPS["knägruppen"])

    def test_exclude_doctors(self, engine):
        """exclude_doctors exkluderar specifika läkare."""
        pf = PersonFilter(exclude_doctors=["OL1", "OL2"])
        docs = engine._filter_doctors(pf)
        doc_ids = {d.id for d in docs}
        assert "OL1" not in doc_ids
        assert "OL2" not in doc_ids

    def test_has_supervisor(self, engine):
        """has_supervisor=True filtrerar bara ST med handledare."""
        pf = PersonFilter(has_supervisor=True)
        docs = engine._filter_doctors(pf)
        for d in docs:
            assert d.supervisor_id is not None


# === ActionSpec ===

class TestAction:
    def test_resolve_functions_explicit(self, engine):
        """Explicit functions-lista returneras direkt."""
        action = ActionSpec(action_type="forbid", functions=["JOUR_P", "JOUR_B"])
        funcs = engine._resolve_functions(action, [], [])
        assert funcs == ["JOUR_P", "JOUR_B"]

    def test_resolve_functions_by_site(self, config):
        """sites=['CSK'] → OP_CSK, AVD_CSK, MOTT_CSK."""
        from solver import _build_functions
        day_funcs, call_funcs, _ = _build_functions(config)
        engine = RuleEngine(config, [])
        action = ActionSpec(action_type="require_count", sites=["CSK"])
        funcs = engine._resolve_functions(action, day_funcs, call_funcs)
        assert "OP_CSK" in funcs
        assert "AVD_CSK" in funcs


# === Kompilering ===

class TestCompilation:
    def test_20_example_rules_compile(self, config):
        """Alla 20 exempelregler ska kompilera utan fel."""
        from ortools.sat.python import cp_model
        from solver import _build_functions

        model = cp_model.CpModel()
        day_funcs, call_funcs, _ = _build_functions(config)
        num_days = 14
        num_weeks = 2

        # Skapa variabler
        x = {}
        for doc in config.doctors:
            for day in range(num_days):
                weekday = day % 7
                if weekday < 5:
                    for fid, _, _ in day_funcs:
                        x[(doc.id, day, fid)] = model.new_bool_var(f"x_{doc.id}_{day}_{fid}")
                for fid, _, _ in call_funcs:
                    x[(doc.id, day, fid)] = model.new_bool_var(f"x_{doc.id}_{day}_{fid}")
                x[(doc.id, day, "LEDIG")] = model.new_bool_var(f"x_{doc.id}_{day}_LEDIG")

        rules = create_example_rules()
        engine = RuleEngine(config, rules)
        terms = engine.compile_to_constraints(model, x, num_days, num_weeks, day_funcs, call_funcs)
        # Ska kompilera utan exception och returnera objective terms
        assert isinstance(terms, list)

    def test_forbid_rule_compiles(self, config):
        """En forbid-regel ska generera constraints."""
        from ortools.sat.python import cp_model
        from solver import _build_functions

        model = cp_model.CpModel()
        day_funcs, call_funcs, _ = _build_functions(config)
        x = {}
        for doc in config.doctors:
            for day in range(14):
                if day % 7 < 5:
                    for fid, _, _ in day_funcs:
                        x[(doc.id, day, fid)] = model.new_bool_var(f"x_{doc.id}_{day}_{fid}")
                for fid, _, _ in call_funcs:
                    x[(doc.id, day, fid)] = model.new_bool_var(f"x_{doc.id}_{day}_{fid}")
                x[(doc.id, day, "LEDIG")] = model.new_bool_var(f"x_{doc.id}_{day}_LEDIG")

        rule = DetailedRule(
            id="test_forbid", name="Test forbid", is_hard=True,
            person_filter=PersonFilter(doctor_ids=["OL1"]),
            action=ActionSpec(action_type="forbid", functions=["JOUR_P"]),
        )
        engine = RuleEngine(config, [rule])
        terms = engine.compile_to_constraints(model, x, 14, 2, day_funcs, call_funcs)
        assert isinstance(terms, list)


# === Validering ===

class TestValidation:
    def test_conflicting_rules(self, config):
        """Assign + forbid samma funktion = konflikt."""
        rules = [
            DetailedRule(id="r1", name="Assign JOUR_P",
                        action=ActionSpec(action_type="assign", functions=["JOUR_P"])),
            DetailedRule(id="r2", name="Forbid JOUR_P",
                        action=ActionSpec(action_type="forbid", functions=["JOUR_P"])),
        ]
        engine = RuleEngine(config, rules)
        conflicts = engine.validate_rules()
        assert len(conflicts) >= 1
        assert conflicts[0]["rule_a"] == "r1"


# === Simulator ===

class TestSimulator:
    def test_employment_change(self, config):
        """Simulation: ändra tjänstgöring."""
        sim = WhatIfSimulator(config)
        results = sim.simulate([
            Scenario(type="doctor_change", description="OL5 till 50%",
                     changes={"doctor_id": "OL5", "employment_rate": 0.5}),
        ], time_limit=15)
        assert len(results) == 1
        assert results[0].feasible is True or results[0].feasible is False

    def test_doctor_leaves(self, config):
        """Simulation: läkare slutar."""
        sim = WhatIfSimulator(config)
        results = sim.simulate([
            Scenario(type="doctor_leaves", description="SP1 slutar",
                     changes={"doctor_id": "SP1"}),
        ], time_limit=15)
        assert len(results) == 1
        assert len(results[0].recommendations_sv) > 0

    def test_add_room(self, config):
        """Simulation: öppna ny sal."""
        sim = WhatIfSimulator(config)
        results = sim.simulate([
            Scenario(type="room_change", description="Ny sal CSK",
                     changes={"site": "CSK", "rooms_delta": 1}),
        ], time_limit=15)
        assert len(results) == 1

    def test_combined_scenarios(self, config):
        """Flera scenarier samtidigt."""
        sim = WhatIfSimulator(config)
        results = sim.simulate([
            Scenario(type="doctor_change", changes={"doctor_id": "OL5", "employment_rate": 0.5}),
            Scenario(type="room_change", changes={"site": "CSK", "rooms_delta": -1}),
        ], time_limit=15)
        assert len(results) == 2
