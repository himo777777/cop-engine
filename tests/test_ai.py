"""
COP Engine — AI Module Tests
==============================
Testar alla 5 AI-moduler med mockad Anthropic-klient.
"""

import pytest
import json
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from data_model import create_kristianstad_example

# Mock Anthropic response helper
def _mock_response(text):
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    resp.usage = MagicMock(input_tokens=100, output_tokens=50)
    return resp


@pytest.fixture
def mock_claude():
    """Mocka Anthropic-klienten."""
    with patch("ai_base._client") as mock:
        mock.messages = MagicMock()
        yield mock


@pytest.fixture
def config():
    return create_kristianstad_example()


class TestAIRules:
    @pytest.mark.asyncio
    async def test_parse_simple_rule(self, mock_claude, config):
        mock_claude.messages.create.return_value = _mock_response(json.dumps({
            "id": "custom_no_night_ol1",
            "name": "Dr Andersson ingen nattjour",
            "category": "preference",
            "is_hard": True,
            "weight": 10,
            "enabled": True,
            "parameters": {"doctor_ids": ["OL1"], "shift_types": ["natt"]}
        }) + "\nRegel: Dr Andersson schemaläggs aldrig för nattjour.")

        from ai_rules import parse_rule
        result = await parse_rule(config, "Dr Andersson ska aldrig jobba natt", clinic_id="test")
        assert result["constraint"] is not None
        assert result["constraint"]["id"] == "custom_no_night_ol1"
        assert result["confidence"] > 0.5
        assert not result.get("error")

    @pytest.mark.asyncio
    async def test_parse_staffing_rule(self, mock_claude, config):
        mock_claude.messages.create.return_value = _mock_response(json.dumps({
            "id": "custom_senior_op",
            "name": "Senior på OP",
            "category": "staffing",
            "is_hard": True,
            "weight": 10,
            "enabled": True,
            "parameters": {"min_count": 1, "roles": ["ÖL", "SP"], "function": "OP"}
        }))

        from ai_rules import parse_rule
        result = await parse_rule(config, "Det måste alltid finnas minst en senior på operation")
        assert result["constraint"]["category"] == "staffing"

    @pytest.mark.asyncio
    async def test_api_error_handling(self, config):
        with patch("ai_base._client", None):
            with patch.dict("os.environ", {}, clear=True):
                # Rensa ANTHROPIC_API_KEY så att ConfigError triggas
                import os
                os.environ.pop("ANTHROPIC_API_KEY", None)
                from ai_rules import parse_rule
                result = await parse_rule(config, "test")
                assert result.get("error") is not None

    @pytest.mark.asyncio
    async def test_malformed_json_fallback(self, mock_claude, config):
        """Malformat JSON-svar ska ge error, inte crash."""
        mock_claude.messages.create.return_value = _mock_response(
            "Här är regeln: ```detta är inte json``` och lite mer text."
        )
        from ai_rules import parse_rule
        result = await parse_rule(config, "Dr X ska aldrig jobba helg")
        assert result.get("error") is not None
        assert result["constraint"] is None


class TestAIConflicts:
    @pytest.mark.asyncio
    async def test_detect_conflict(self, mock_claude, config):
        mock_claude.messages.create.return_value = _mock_response(json.dumps({
            "conflicts": [{"rule_id": "max_workdays", "description": "Konflikt med max 5 dagar", "severity": "high"}],
            "feasibility": "warning",
            "suggestions_sv": ["Öka till 6 arbetsdagar eller minska bemanningskrav"]
        }))

        from ai_conflicts import check_conflicts
        result = await check_conflicts(config, {"id": "min_6_days", "name": "Min 6 dagar"})
        assert len(result["conflicts"]) == 1
        assert result["feasibility"] == "warning"

    @pytest.mark.asyncio
    async def test_no_conflicts(self, mock_claude, config):
        mock_claude.messages.create.return_value = _mock_response(json.dumps({
            "conflicts": [],
            "feasibility": "ok",
            "suggestions_sv": []
        }))

        from ai_conflicts import check_conflicts
        result = await check_conflicts(config, {"id": "new_rule"})
        assert result["feasibility"] == "ok"

    @pytest.mark.asyncio
    async def test_rule_based_fallback_no_ai(self, config):
        """Utan AI: regelbaserad fallback ska returnera ett svar."""
        with patch("ai_base._client", None):
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)
            from ai_conflicts import check_conflicts
            result = await check_conflicts(
                config,
                {"id": "test_rule", "category": "preference", "is_hard": False, "parameters": {}}
            )
            assert "conflicts" in result
            assert "feasibility" in result
            assert result.get("fallback") is True

    @pytest.mark.asyncio
    async def test_rule_based_fallback_doctor_overlap(self, config):
        """Fallback ska detektera kollision på doctor_ids + shift_types."""
        from ai_conflicts import _rule_based_conflicts
        # Bygg en existing-lista med en hård regel för OL1 + natt
        existing = [{"id": "r1", "name": "OL1 ingen natt", "category": "preference",
                     "is_hard": True, "weight": 10,
                     "parameters": {"doctor_ids": ["OL1"], "shift_types": ["natt"]}}]
        new_rule = {"id": "r2", "category": "preference", "is_hard": True,
                    "parameters": {"doctor_ids": ["OL1"], "shift_types": ["natt"]}}
        result = _rule_based_conflicts(existing, new_rule)
        assert len(result["conflicts"]) >= 1
        assert result["conflicts"][0]["severity"] == "high"


class TestAIExplain:
    @pytest.mark.asyncio
    async def test_explain_assignment(self, mock_claude, config):
        mock_claude.messages.create.return_value = _mock_response(json.dumps({
            "explanation_sv": "Dr Andersson fick JOUR_B pga bakjour-behörighet och rättvis fördelning.",
            "constraints_applied": ["call_fairness", "atl_daily_rest"],
            "alternatives_considered": [{"doctor": "OL2", "reason_rejected": "Redan jour denna vecka"}],
            "quality": "optimal"
        }))

        schedule = {"schedule": {"OL1": {"2026-04-06": "JOUR_B"}, "OL2": {"2026-04-06": "MOTT_CSK"}}}
        from ai_explain import explain_assignment
        result = await explain_assignment(config, schedule, "OL1", "2026-04-06")
        assert "Andersson" in result["explanation_sv"]
        assert len(result["constraints_applied"]) > 0

    @pytest.mark.asyncio
    async def test_constraint_fallback_no_ai(self, config):
        """Utan AI: constraint-listning ska returnera förklaring."""
        with patch("ai_base._client", None):
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)
            schedule = {"schedule": {"OL1": {"2026-04-06": "JOUR_B"}}}
            from ai_explain import explain_assignment
            result = await explain_assignment(config, schedule, "OL1", "2026-04-06")
            assert result.get("fallback") is True
            assert len(result["explanation_sv"]) > 0
            assert result.get("error") is None


class TestAIPredict:
    @pytest.mark.asyncio
    async def test_predict_with_data(self, mock_claude):
        mock_claude.messages.create.return_value = _mock_response(json.dumps({
            "predictions": [{"date": "2026-04-07", "risk_level": "high", "reason": "Historiskt hög frånvaro"}],
            "patterns_sv": ["Måndagar har 30% högre sjukfrånvaro"],
            "recommendations_sv": ["Extra bemanning måndag"],
            "overall_risk": "medium"
        }))

        from ai_predict import predict_absence
        history = [{"doctor_id": "SP1", "date": "2026-03-01", "type": "sjuk"}]
        result = await predict_absence(history, "2026-04-06", "2026-04-12", 25)
        assert result["overall_risk"] == "medium"

    @pytest.mark.asyncio
    async def test_predict_no_data(self):
        from ai_predict import predict_absence
        result = await predict_absence([], "2026-04-06", "2026-04-12", 25)
        assert result["overall_risk"] == "unknown"
        assert not result.get("error")

    @pytest.mark.asyncio
    async def test_statistical_fallback_no_ai(self):
        """Utan AI: statistisk fallback ska räkna veckodagar."""
        with patch("ai_base._client", None):
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)
            from ai_predict import predict_absence
            history = [
                {"doctor_id": "SP1", "date": "2026-03-02", "type": "sjuk"},  # måndag
                {"doctor_id": "SP2", "date": "2026-03-09", "type": "sjuk"},  # måndag
                {"doctor_id": "SP3", "date": "2026-03-03", "type": "sjuk"},  # tisdag
            ]
            result = await predict_absence(history, "2026-04-06", "2026-04-12", 25)
            assert result.get("fallback") is True
            assert result["overall_risk"] in ("high", "medium", "low", "unknown")
            assert len(result["patterns_sv"]) > 0


class TestAIChat:
    @pytest.mark.asyncio
    async def test_query_intent(self, mock_claude, config):
        mock_claude.messages.create.return_value = _mock_response(json.dumps({
            "response_sv": "Nästa helg jobbar Dr Fredriksson (primärjour) och Dr Andersson (bakjour).",
            "intent": "query",
            "action": None,
            "suggestions": ["Vill du se hela veckoschemat?"]
        }))

        schedule = {"schedule": {"SP1": {"2026-04-11": "JOUR_P"}, "OL1": {"2026-04-11": "JOUR_B"}}}
        from ai_chat import chat
        result = await chat(config, schedule, "SP1", "Vem jobbar nästa helg?")
        assert result["intent"] == "query"
        assert "helg" in result["response_sv"].lower() or "jour" in result["response_sv"].lower()

    @pytest.mark.asyncio
    async def test_swap_intent(self, mock_claude, config):
        mock_claude.messages.create.return_value = _mock_response(json.dumps({
            "response_sv": "Jag kan försöka byta ditt torsdagspass. Vem vill du byta med?",
            "intent": "swap",
            "action": {"type": "swap", "details": {"day": "torsdag"}},
            "suggestions": ["Dr Gustafsson är ledig torsdag"]
        }))

        from ai_chat import chat
        result = await chat(config, {}, "SP1", "Byt mitt torsdagspass med någon")
        assert result["intent"] == "swap"

    @pytest.mark.asyncio
    async def test_static_fallback_no_ai(self, config):
        """Utan AI: statisk fallback-text ska returneras."""
        with patch("ai_base._client", None):
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)
            from ai_chat import chat
            result = await chat(config, {}, "SP1", "Vem jobbar i helgen?")
            assert result.get("fallback") is True
            assert "otillgänglig" in result["response_sv"].lower()


class TestAIBase:
    def test_rate_limiting(self):
        from ai_base import _check_rate_limit, _record_call
        clinic = "test_rate"
        for _ in range(10):
            assert _check_rate_limit(clinic)
            _record_call(clinic)
        assert not _check_rate_limit(clinic)  # 11th call blocked

    def test_cache(self):
        from ai_base import _cache_key, _get_cached, _cache
        key = _cache_key("sys", [{"role": "user", "content": "hi"}])
        assert _get_cached(key) is None
        import time
        from ai_base import _set_cached
        _set_cached(key, {"text": "cached"})
        assert _get_cached(key) == {"text": "cached"}

    def test_extract_json_direct(self):
        from ai_base import _extract_json
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_extract_json_embedded(self):
        from ai_base import _extract_json
        text = 'Här är resultatet: {"foo": "bar"} och lite mer text.'
        assert _extract_json(text) == {"foo": "bar"}

    def test_extract_json_code_block(self):
        from ai_base import _extract_json
        text = '```json\n{"key": "value"}\n```'
        result = _extract_json(text)
        assert result == {"key": "value"}

    def test_extract_json_returns_none_on_garbage(self):
        from ai_base import _extract_json
        assert _extract_json("detta är ingen json alls") is None
        assert _extract_json("") is None

    @pytest.mark.asyncio
    async def test_retry_on_transient_error(self, mock_claude):
        """Tre transienta fel + ett lyckat svar → funktionen returnerar OK."""
        import anthropic as _anthropic

        call_count = 0
        good_response = _mock_response('{"text": "ok"}')

        def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _anthropic.APIConnectionError(request=MagicMock())
            return good_response

        mock_claude.messages.create.side_effect = side_effect

        from ai_base import call_claude
        import ai_base
        # Sätt retry delays till 0 för snabba tester
        original_delays = ai_base.RETRY_DELAYS
        ai_base.RETRY_DELAYS = [0, 0, 0]
        try:
            result = await call_claude("sys", [{"role": "user", "content": "hi"}], clinic_id="retry_test")
            assert "error" not in result or not result["error"]
        finally:
            ai_base.RETRY_DELAYS = original_delays
