"""
COP Engine — End-to-End Tester
=================================
Testar hela flödet: login → schemagenerering → frånvarokedja → API.
Simulerar en realistisk användarsession.

Kör: pytest tests/test_e2e.py -v -p no:cacheprovider
"""

import pytest
from httpx import AsyncClient, ASGITransport
from api import app


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def client():
    """Synkron test-transport för API."""
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield c


# ============================================================================
# 1. HEALTH & SYSTEM
# ============================================================================

class TestSystemE2E:
    """Systemet startar korrekt och grundendpoints fungerar."""

    def test_health_returns_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "healthy"
        assert data["solver_available"] is True

    def test_config_returns_doctors(self, client):
        r = client.get("/config")
        assert r.status_code == 200
        data = r.json()
        assert "doctors" in data
        assert len(data["doctors"]) >= 20

    def test_statistics_empty_before_generate(self, client):
        r = client.get("/statistics")
        assert r.status_code == 200

    def test_schedules_list_initially_may_be_empty(self, client):
        r = client.get("/schedules")
        assert r.status_code == 200


# ============================================================================
# 2. AUTH FLOW
# ============================================================================

class TestAuthE2E:
    """Autentisering: login → token → protected endpoint → logout."""

    def test_login_with_valid_credentials(self, client):
        r = client.post("/auth/login", json={
            "username": "admin",
            "password": "cop-admin-2026",
        })
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert data["user"]["role"] == "admin"
        assert data["user"]["username"] == "admin"

    def test_login_with_invalid_credentials(self, client):
        r = client.post("/auth/login", json={
            "username": "admin",
            "password": "wrong",
        })
        assert r.status_code == 401

    def test_protected_endpoint_without_token(self, client):
        r = client.get("/auth/me")
        assert r.status_code in (401, 403)

    def test_protected_endpoint_with_token(self, client):
        # Login
        login = client.post("/auth/login", json={
            "username": "admin", "password": "cop-admin-2026",
        })
        token = login.json()["access_token"]

        # Access protected endpoint
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["username"] == "admin"

    def test_scheduler_login(self, client):
        r = client.post("/auth/login", json={
            "username": "scheduler", "password": "schema-2026",
        })
        assert r.status_code == 200
        assert r.json()["user"]["role"] == "scheduler"

    def test_viewer_login(self, client):
        r = client.post("/auth/login", json={
            "username": "viewer", "password": "viewer-2026",
        })
        assert r.status_code == 200
        assert r.json()["user"]["role"] == "viewer"

    def test_admin_list_users(self, client):
        login = client.post("/auth/login", json={
            "username": "admin", "password": "cop-admin-2026",
        })
        token = login.json()["access_token"]

        r = client.get("/auth/users", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        users = r.json()
        assert len(users) >= 3  # admin, scheduler, viewer

    def test_admin_create_user(self, client):
        login = client.post("/auth/login", json={
            "username": "admin", "password": "cop-admin-2026",
        })
        token = login.json()["access_token"]

        r = client.post("/auth/users", headers={"Authorization": f"Bearer {token}"}, json={
            "username": "test_doc",
            "email": "test@cop.local",
            "full_name": "Test Doktor",
            "password": "test-2026",
            "role": "doctor",
            "doctor_id": "OL1",
        })
        assert r.status_code == 200
        assert r.json()["role"] == "doctor"

    def test_change_password(self, client):
        # Login as viewer
        login = client.post("/auth/login", json={
            "username": "viewer", "password": "viewer-2026",
        })
        token = login.json()["access_token"]

        r = client.post("/auth/change-password",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"old_password": "viewer-2026", "new_password": "new-viewer-2026"})
        assert r.status_code == 200

        # Login with new password
        r2 = client.post("/auth/login", json={
            "username": "viewer", "password": "new-viewer-2026",
        })
        assert r2.status_code == 200

        # Reset password back
        token2 = r2.json()["access_token"]
        client.post("/auth/change-password",
                     headers={"Authorization": f"Bearer {token2}"},
                     json={"old_password": "new-viewer-2026", "new_password": "viewer-2026"})


# ============================================================================
# 3. FULL WORKFLOW: Schema → Frånvaro → Kedja
# ============================================================================

class TestWorkflowE2E:
    """Fullständigt arbetsflöde: generera → hämta → frånvaro → kedja."""

    def test_generate_and_retrieve_schedule(self, client):
        """Generera 1-veckors schema och hämta det."""
        r = client.post("/schedule/generate", json={
            "clinic_id": "kristianstad",
            "num_weeks": 1,
            "time_limit_seconds": 60,
        })
        assert r.status_code == 200
        data = r.json()
        assert "schedule_id" in data or "job_id" in data

        # Om asynkront: vänta på jobb
        if "job_id" in data:
            import time
            job_id = data["job_id"]
            for _ in range(120):
                jr = client.get(f"/job/{job_id}")
                jdata = jr.json()
                if jdata.get("status") in ("completed", "failed"):
                    break
                time.sleep(1)
            assert jdata["status"] == "completed"
            schedule_id = jdata["result"]["schedule_id"]
        else:
            schedule_id = data["schedule_id"]

        # Hämta schemat
        r2 = client.get(f"/schedule/{schedule_id}")
        assert r2.status_code == 200
        sched = r2.json()
        assert sched["schedule_id"] == schedule_id

        # Lista scheman
        r3 = client.get("/schedules")
        assert r3.status_code == 200
        assert any(s["schedule_id"] == schedule_id for s in r3.json())

        # Hämta specifik läkares schema
        r4 = client.get(f"/schedule/{schedule_id}/doctor/OL1")
        assert r4.status_code == 200

        # Hämta statistik
        r5 = client.get(f"/statistics/{schedule_id}")
        assert r5.status_code == 200

        return schedule_id

    def test_absence_chain_flow(self, client):
        """Registrera frånvaro och kör kedja."""
        # Generera schema först
        r = client.post("/schedule/generate", json={
            "clinic_id": "kristianstad",
            "num_weeks": 1,
            "time_limit_seconds": 60,
        })
        data = r.json()

        if "job_id" in data:
            import time
            job_id = data["job_id"]
            for _ in range(120):
                jr = client.get(f"/job/{job_id}")
                jdata = jr.json()
                if jdata.get("status") in ("completed", "failed"):
                    break
                time.sleep(1)
            schedule_id = jdata["result"]["schedule_id"]
        else:
            schedule_id = data.get("schedule_id", list(data.keys())[0] if data else "")

        # Kör frånvarokedja
        r2 = client.post("/absence/chain", json={
            "schedule_id": schedule_id,
            "doctor_id": "OL1",
            "absence_type": "SJUK",
            "start_date": "2026-04-07",
            "end_date": "2026-04-08",
            "auto_select": True,
        })
        assert r2.status_code == 200
        chain = r2.json()
        assert "chain_id" in chain
        assert chain["status"] in ("COMPLETED", "PARTIAL", "FAILED", "completed", "partial", "failed")

        # Hämta kedja
        r3 = client.get(f"/absence/chain/{chain['chain_id']}")
        assert r3.status_code == 200

        # Lista kedjor
        r4 = client.get("/absence/chains")
        assert r4.status_code == 200

    def test_validate_schedule(self, client):
        """Validera ATL-efterlevnad för ett schema."""
        # Generera schema
        r = client.post("/schedule/generate", json={
            "clinic_id": "kristianstad",
            "num_weeks": 1,
            "time_limit_seconds": 60,
        })
        data = r.json()

        if "job_id" in data:
            import time
            job_id = data["job_id"]
            for _ in range(120):
                jr = client.get(f"/job/{job_id}")
                jdata = jr.json()
                if jdata.get("status") in ("completed", "failed"):
                    break
                time.sleep(1)
            schedule_id = jdata["result"]["schedule_id"]
        else:
            schedule_id = data.get("schedule_id", "")

        # Validera
        r2 = client.post(f"/validate/{schedule_id}")
        assert r2.status_code == 200
        val = r2.json()
        assert "valid" in val or "atl_compliant" in val or "violations" in val


# ============================================================================
# 4. WEBSOCKET
# ============================================================================

class TestWebSocketE2E:
    """WebSocket-anslutning och statistik."""

    def test_ws_stats_endpoint(self, client):
        r = client.get("/ws/stats")
        assert r.status_code == 200
        data = r.json()
        assert "total_connections" in data

    def test_ws_connect(self, client):
        """Test WebSocket-anslutning."""
        with client.websocket_connect("/ws/schedule") as ws:
            # Borde få välkomstmeddelande
            data = ws.receive_json()
            assert data["event"] == "connected"
            assert "schedule" in data["data"]["channels"]

            # Skicka ping
            ws.send_json({"action": "ping"})
            pong = ws.receive_json()
            assert pong["event"] == "pong"

    def test_ws_subscribe_channel(self, client):
        """Test att subscriba till ny kanal."""
        with client.websocket_connect("/ws/schedule") as ws:
            ws.receive_json()  # welcome

            ws.send_json({"action": "subscribe", "channel": "absence"})
            resp = ws.receive_json()
            assert resp["event"] == "subscribed"
            assert resp["channel"] == "absence"

    def test_ws_history(self, client):
        """Test att hämta event-historik."""
        with client.websocket_connect("/ws/schedule") as ws:
            ws.receive_json()  # welcome

            ws.send_json({"action": "get_history", "channel": "schedule", "limit": 10})
            resp = ws.receive_json()
            assert resp["event"] == "history"
            assert isinstance(resp["events"], list)


# ============================================================================
# 5. ERROR HANDLING
# ============================================================================

class TestErrorsE2E:
    """Felhantering: felaktiga request, 404, etc."""

    def test_nonexistent_schedule(self, client):
        r = client.get("/schedule/nonexistent-id")
        assert r.status_code == 404

    def test_nonexistent_chain(self, client):
        r = client.get("/absence/chain/nonexistent-id")
        assert r.status_code == 404

    def test_nonexistent_config(self, client):
        r = client.get("/config/nonexistent-clinic")
        assert r.status_code == 404

    def test_invalid_schedule_request(self, client):
        r = client.post("/schedule/generate", json={
            "clinic_id": "kristianstad",
            "num_weeks": 100,  # Over limit
        })
        assert r.status_code == 422  # Validation error
