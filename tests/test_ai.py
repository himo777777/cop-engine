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
            from ai_rules import parse_rule
            result = await parse_rule(config, "test")
            assert result.get("error") is not None


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
        _cache[key] = (time.time(), {"text": "cached"})
        assert _get_cached(key) == {"text": "cached"}
