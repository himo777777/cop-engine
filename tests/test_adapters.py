"""
Tester för integrationsadaptrar — alla med mocks.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date
from adapters.base import AdapterConfig, AdapterType, SyncDirection, SyncResult
from adapters.timecare_adapter import TimeCareAdapter
from adapters.heroma_adapter import HeromaAdapter
from adapters.adapter_manager import AdapterManager, ADAPTER_REGISTRY


@pytest.fixture
def timecare_config():
    return AdapterConfig(
        adapter_type=AdapterType.TIME_CARE,
        host="timecare.example.com",
        port=443,
        username="client_id",
        password="client_secret",
    )


@pytest.fixture
def heroma_config():
    return AdapterConfig(
        adapter_type=AdapterType.HEROMA,
        host="heroma.example.com",
        port=443,
        username="user",
        password="pass",
    )


class TestTimeCareAdapter:
    def test_shift_code_mapping(self, timecare_config):
        """TimeCare numeriska koder ska mappas korrekt."""
        adapter = TimeCareAdapter(timecare_config)
        assert adapter._map_shift_code(1) == "DAG"
        assert adapter._map_shift_code(2) == "JOUR_P"
        assert adapter._map_shift_code(3) == "JOUR_B"
        assert adapter._map_shift_code(5) == "OP"
        assert adapter._map_shift_code(0) == "LEDIG"

    def test_func_to_shift_code(self, timecare_config):
        """COP-funktioner ska mappas till TimeCare-koder."""
        adapter = TimeCareAdapter(timecare_config)
        assert adapter._func_to_shift_code("OP_CSK") == 5
        assert adapter._func_to_shift_code("AVD_H") == 6
        assert adapter._func_to_shift_code("JOUR_P") == 2
        assert adapter._func_to_shift_code("JOUR_B") == 3

    def test_default_role_mapping(self, timecare_config):
        """Standard-rollmappning ska finnas."""
        adapter = TimeCareAdapter(timecare_config)
        mapping = adapter._default_role_mapping()
        assert "1" in mapping  # ÖL
        assert "2" in mapping  # SP
        assert mapping["1"] == "ÖL"


class TestHeromaAdapter:
    def test_soap_envelope(self, heroma_config):
        """SOAP-envelope ska ha korrekt struktur."""
        adapter = HeromaAdapter(heroma_config)
        envelope = adapter._build_envelope("GetEmployees", "<her:Dept>Ortopedi</her:Dept>")
        assert "soapenv:Envelope" in envelope
        assert "wsse:Security" in envelope
        assert "wsse:Username" in envelope
        assert "GetEmployees" in envelope
        assert "Ortopedi" in envelope

    def test_heroma_function_mapping(self, heroma_config):
        """Heroma-funktionskoder ska mappas korrekt."""
        adapter = HeromaAdapter(heroma_config)
        assert adapter._map_heroma_function("JOUR_PRIMÄR") == "JOUR_P"
        assert adapter._map_heroma_function("JOUR_BAK") == "JOUR_B"
        assert adapter._map_heroma_function("OPERATION") == "OP"

    def test_func_to_heroma(self, heroma_config):
        """COP-funktioner ska mappas till Heroma-koder."""
        adapter = HeromaAdapter(heroma_config)
        assert adapter._func_to_heroma("OP_CSK") == "OPERATION"
        assert adapter._func_to_heroma("JOUR_P") == "JOUR_PRIMÄR"
        assert adapter._func_to_heroma("AVD_H") == "AVDELNING"

    def test_default_role_mapping(self, heroma_config):
        adapter = HeromaAdapter(heroma_config)
        mapping = adapter._default_role_mapping()
        assert "LÄKARE_ÖL" in mapping
        assert mapping["LÄKARE_ÖL"] == "ÖL"


class TestAdapterManager:
    def test_registry_has_all_adapters(self):
        """Alla 4 adaptrar ska finnas i registret."""
        assert AdapterType.TESSA in ADAPTER_REGISTRY
        assert AdapterType.CSV in ADAPTER_REGISTRY
        assert AdapterType.TIME_CARE in ADAPTER_REGISTRY
        assert AdapterType.HEROMA in ADAPTER_REGISTRY

    def test_available_adapters(self):
        """get_available_adapters ska lista alla."""
        available = AdapterManager.get_available_adapters()
        assert "tessa" in available
        assert "csv" in available
        assert "time_care" in available
        assert "heroma" in available


class TestIntegrationEndpoints:
    """Testa API-endpoints för integrationer."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from api import app
        return TestClient(app)

    def test_list_integrations(self, client):
        resp = client.get("/integrations")
        assert resp.status_code == 200
        data = resp.json()
        assert "available" in data
        assert "tessa" in data["available"]
        assert "time_care" in data["available"]
        assert "heroma" in data["available"]
