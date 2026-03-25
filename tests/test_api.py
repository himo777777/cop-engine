"""
COP Engine — Integration Tests för REST API
=============================================
Testar alla endpoints via TestClient (ingen server behövs).
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from api import app


@pytest.fixture(scope="module")
def client():
    """FastAPI TestClient — delar state mellan tester i denna modul."""
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def generated_schedule_id(client):
    """Generera ett schema och returnera dess ID."""
    resp = client.post("/schedule/generate", json={
        "clinic_id": "kristianstad",
        "num_weeks": 2,
        "time_limit_seconds": 30,
    })
    assert resp.status_code == 200
    data = resp.json()
    return data["schedule_id"]


class TestHealthEndpoint:
    def test_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["solver_available"] is True

    def test_health_has_version(self, client):
        resp = client.get("/health")
        assert "version" in resp.json()


class TestConfigEndpoint:
    def test_get_config(self, client):
        resp = client.get("/config/kristianstad")
        assert resp.status_code == 200
        data = resp.json()
        assert data["num_doctors"] == 25
        assert data["num_rooms"] == 7

    def test_config_not_found(self, client):
        resp = client.get("/config/nonexistent")
        assert resp.status_code == 404

    def test_config_has_doctors(self, client):
        resp = client.get("/config/kristianstad")
        data = resp.json()
        assert len(data["doctors"]) == 25
        roles = {d["role"] for d in data["doctors"]}
        assert "ÖL" in roles
        assert "SP" in roles

    def test_config_has_rooms(self, client):
        resp = client.get("/config/kristianstad")
        data = resp.json()
        assert len(data["operating_rooms"]) == 7


class TestScheduleGeneration:
    def test_generate_schedule(self, client):
        resp = client.post("/schedule/generate", json={
            "clinic_id": "kristianstad",
            "num_weeks": 1,
            "time_limit_seconds": 15,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("optimal", "feasible")
        assert "schedule_id" in data
        assert "schedule" in data

    def test_generate_returns_statistics(self, client):
        resp = client.post("/schedule/generate", json={
            "clinic_id": "kristianstad",
            "num_weeks": 1,
            "time_limit_seconds": 15,
        })
        data = resp.json()
        assert "statistics" in data
        stats = data["statistics"]
        assert "call_distribution" in stats
        assert "workload_balance" in stats

    def test_generate_invalid_clinic(self, client):
        resp = client.post("/schedule/generate", json={
            "clinic_id": "nonexistent",
            "num_weeks": 1,
        })
        assert resp.status_code == 404


class TestGetSchedule:
    def test_get_schedule(self, client, generated_schedule_id):
        resp = client.get(f"/schedule/{generated_schedule_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["schedule_id"] == generated_schedule_id
        assert len(data["schedule"]) == 25  # 25 läkare

    def test_get_schedule_not_found(self, client):
        resp = client.get("/schedule/nonexistent")
        assert resp.status_code == 404


class TestDoctorSchedule:
    def test_get_doctor_schedule(self, client, generated_schedule_id):
        resp = client.get(f"/schedule/{generated_schedule_id}/doctor/SP1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["doctor_id"] == "SP1"
        assert "schedule" in data
        assert len(data["schedule"]) == 14  # 14 dagar

    def test_doctor_not_found(self, client, generated_schedule_id):
        resp = client.get(f"/schedule/{generated_schedule_id}/doctor/FAKE")
        assert resp.status_code == 404


class TestListSchedules:
    def test_list_schedules(self, client, generated_schedule_id):
        resp = client.get("/schedules")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        ids = [s["schedule_id"] for s in data]
        assert generated_schedule_id in ids


class TestScheduleAdjustment:
    def test_swap(self, client, generated_schedule_id):
        resp = client.post("/schedule/adjust", json={
            "schedule_id": generated_schedule_id,
            "adjustment_type": "swap",
            "doctor_id": "SP1",
            "day": 0,
            "swap_with_doctor_id": "SP2",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "adjusted"

    def test_replace(self, client, generated_schedule_id):
        resp = client.post("/schedule/adjust", json={
            "schedule_id": generated_schedule_id,
            "adjustment_type": "replace",
            "doctor_id": "UL1",
            "day": 0,
            "new_function": "AVD_C",
        })
        assert resp.status_code == 200

    def test_swap_missing_partner(self, client, generated_schedule_id):
        resp = client.post("/schedule/adjust", json={
            "schedule_id": generated_schedule_id,
            "adjustment_type": "swap",
            "doctor_id": "SP1",
            "day": 0,
        })
        assert resp.status_code == 400

    def test_invalid_adjustment_type(self, client, generated_schedule_id):
        resp = client.post("/schedule/adjust", json={
            "schedule_id": generated_schedule_id,
            "adjustment_type": "teleport",
            "doctor_id": "SP1",
            "day": 0,
        })
        assert resp.status_code == 400


class TestAbsenceEndpoint:
    def test_register_absence(self, client):
        resp = client.post("/absence", json={
            "clinic_id": "kristianstad",
            "doctor_id": "OL1",
            "absence_type": "sjuk",
            "start_date": "2026-04-15",
            "end_date": "2026-04-16",
            "reoptimize": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "registered"
        assert data["doctor_name"] == "Dr Andersson"

    def test_absence_invalid_doctor(self, client):
        resp = client.post("/absence", json={
            "clinic_id": "kristianstad",
            "doctor_id": "FAKE",
            "absence_type": "sjuk",
            "start_date": "2026-04-15",
            "end_date": "2026-04-16",
        })
        assert resp.status_code == 404


class TestAbsenceChainEndpoint:
    def test_run_chain(self, client, generated_schedule_id):
        resp = client.post("/absence/chain", json={
            "schedule_id": generated_schedule_id,
            "doctor_id": "SP2",
            "absence_type": "sjuk",
            "start_date": "2026-04-06",
            "end_date": "2026-04-07",
            "auto_select": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "chain_id" in data
        assert data["status"] in ("completed", "atl_violation", "manual_required")
        assert "summary" in data

    def test_chain_manual_mode(self, client, generated_schedule_id):
        resp = client.post("/absence/chain", json={
            "schedule_id": generated_schedule_id,
            "doctor_id": "OL2",
            "absence_type": "semester",
            "start_date": "2026-04-08",
            "end_date": "2026-04-08",
            "auto_select": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        # Bör ha kandidater men inget valt
        if data.get("replacements"):
            assert data["replacements"][0].get("selected") is None

    def test_chain_invalid_schedule(self, client):
        resp = client.post("/absence/chain", json={
            "schedule_id": "fake_schedule",
            "doctor_id": "SP1",
            "absence_type": "sjuk",
            "start_date": "2026-04-06",
            "end_date": "2026-04-06",
        })
        assert resp.status_code == 404

    def test_list_chains(self, client, generated_schedule_id):
        # Kör en kedja först
        client.post("/absence/chain", json={
            "schedule_id": generated_schedule_id,
            "doctor_id": "UL1",
            "absence_type": "vab",
            "start_date": "2026-04-09",
            "end_date": "2026-04-09",
        })
        resp = client.get("/absence/chains")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1


class TestValidation:
    def test_validate_schedule(self, client, generated_schedule_id):
        resp = client.post(f"/validate/{generated_schedule_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert "valid" in data
        assert "summary" in data
        assert "violations" in data

    def test_validate_not_found(self, client):
        resp = client.post("/validate/nonexistent")
        assert resp.status_code == 404


class TestStatistics:
    def test_get_statistics(self, client, generated_schedule_id):
        resp = client.get(f"/statistics/{generated_schedule_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert "call_distribution" in data
        assert "staffing_per_day" in data
        assert "st_matching" in data
        assert "workload_balance" in data

    def test_statistics_not_found(self, client):
        resp = client.get("/statistics/nonexistent")
        assert resp.status_code == 404


class TestReoptimize:
    def test_reoptimize(self, client, generated_schedule_id):
        resp = client.post("/schedule/reoptimize", json={
            "schedule_id": generated_schedule_id,
            "time_limit_seconds": 15,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "reoptimized"
        assert "new_schedule_id" in data
