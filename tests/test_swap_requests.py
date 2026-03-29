"""Tester för peer-to-peer bytesförfrågningar."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from api import app
    return TestClient(app)


class TestSwapRequests:
    def test_create_swap_request(self, client):
        """Skapa bytesförfrågan → status pending_peer."""
        resp = client.post("/swap-requests", json={
            "clinic_id": "kristianstad",
            "requester_id": "SP1",
            "requester_date": "2026-04-07",
            "requester_function": "OP_CSK",
            "target_id": "SP2",
            "target_date": "2026-04-07",
            "target_function": "AVD_CSK",
            "message": "Kan vi byta?",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "created"
        assert "id" in data

    def test_list_swap_requests(self, client):
        """Lista förfrågningar."""
        # Skapa först
        client.post("/swap-requests", json={
            "requester_id": "SP3", "requester_date": "2026-04-08",
            "requester_function": "OP_H", "target_id": "SP4",
            "target_date": "2026-04-08", "target_function": "AVD_H",
        })
        resp = client.get("/swap-requests")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["requests"]) >= 1

    def test_peer_accept_flow(self, client):
        """Motpart godkänner → status atl_validated."""
        create = client.post("/swap-requests", json={
            "requester_id": "SP5", "requester_date": "2026-04-09",
            "requester_function": "JOUR_P", "target_id": "SP6",
            "target_date": "2026-04-09", "target_function": "OP_CSK",
        })
        req_id = create.json()["id"]

        resp = client.post(f"/swap-requests/{req_id}/peer-respond", json={"accept": True})
        assert resp.status_code == 200
        assert resp.json()["status"] == "atl_validated"

    def test_peer_reject(self, client):
        """Motpart nekar → status peer_rejected."""
        create = client.post("/swap-requests", json={
            "requester_id": "SP7", "requester_date": "2026-04-10",
            "requester_function": "AVD_CSK", "target_id": "SP8",
            "target_date": "2026-04-10", "target_function": "OP_H",
        })
        req_id = create.json()["id"]

        resp = client.post(f"/swap-requests/{req_id}/peer-respond", json={"accept": False})
        assert resp.status_code == 200
        assert resp.json()["status"] == "peer_rejected"

    def test_cancel_request(self, client):
        """Avbryt förfrågan → status cancelled."""
        create = client.post("/swap-requests", json={
            "requester_id": "OL1", "requester_date": "2026-04-11",
            "requester_function": "MOTT_CSK", "target_id": "OL2",
            "target_date": "2026-04-11", "target_function": "AVD_CSK",
        })
        req_id = create.json()["id"]

        resp = client.post(f"/swap-requests/{req_id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"
