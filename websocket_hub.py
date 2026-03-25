"""
COP Engine — WebSocket Hub
============================
Realtidskommunikation för schemaändringar, frånvaro och notifieringar.

Kanaler:
  /ws/schedule    — Schemauppdateringar i realtid
  /ws/absence     — Frånvarokedjenotifieringar
  /ws/dashboard   — KPI-uppdateringar för dashboard
  /ws/notify      — Personliga notifieringar per användare

Användning:
  from websocket_hub import ws_router, hub

  app.include_router(ws_router)

  # Broadcast en schemaändring
  await hub.broadcast("schedule", {"type": "shift_changed", ...})

  # Skicka till specifik användare
  await hub.send_to_user("usr_123", {"type": "absence_approved", ...})
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from dataclasses import dataclass, field

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query


# ---------------------------------------------------------------------------
# Event Types
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    # Schema
    SCHEDULE_GENERATED = "schedule_generated"
    SHIFT_CHANGED = "shift_changed"
    SCHEDULE_OPTIMIZED = "schedule_optimized"

    # Frånvaro
    ABSENCE_REPORTED = "absence_reported"
    ABSENCE_CHAIN_STARTED = "absence_chain_started"
    ABSENCE_CHAIN_COMPLETED = "absence_chain_completed"
    REPLACEMENT_ASSIGNED = "replacement_assigned"
    REPLACEMENT_NEEDED = "replacement_needed"  # Manuell ersättning krävs

    # ATL
    ATL_WARNING = "atl_warning"
    ATL_VIOLATION = "atl_violation"

    # System
    SOLVER_STARTED = "solver_started"
    SOLVER_PROGRESS = "solver_progress"
    SOLVER_COMPLETED = "solver_completed"
    SYSTEM_ALERT = "system_alert"

    # Dashboard
    KPI_UPDATE = "kpi_update"
    STAFFING_UPDATE = "staffing_update"


@dataclass
class WSEvent:
    """WebSocket event med metadata."""
    event_type: str
    data: dict
    channel: str = "schedule"
    target_user: Optional[str] = None  # None = broadcast
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> str:
        return json.dumps({
            "event": self.event_type,
            "channel": self.channel,
            "data": self.data,
            "timestamp": self.timestamp,
        }, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Connection Manager
# ---------------------------------------------------------------------------

@dataclass
class WSConnection:
    """En aktiv WebSocket-anslutning."""
    websocket: WebSocket
    user_id: Optional[str] = None
    channels: set = field(default_factory=lambda: {"schedule"})
    connected_at: float = field(default_factory=time.time)
    last_ping: float = field(default_factory=time.time)


class WebSocketHub:
    """
    Central hub för alla WebSocket-anslutningar.
    Hanterar kanaler, broadcast och riktade meddelanden.
    """

    def __init__(self):
        self._connections: list[WSConnection] = []
        self._event_log: list[WSEvent] = []
        self._max_log = 1000
        self._lock = asyncio.Lock()

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: WebSocket, user_id: Optional[str] = None,
                      channels: Optional[list[str]] = None) -> WSConnection:
        """Acceptera ny WebSocket-anslutning."""
        await websocket.accept()
        conn = WSConnection(
            websocket=websocket,
            user_id=user_id,
            channels=set(channels or ["schedule"]),
        )
        async with self._lock:
            self._connections.append(conn)

        # Skicka välkomstmeddelande
        await self._send(conn, WSEvent(
            event_type="connected",
            channel="system",
            data={
                "message": "Ansluten till COP realtidsuppdateringar",
                "channels": list(conn.channels),
                "active_connections": self.connection_count,
            },
        ))

        return conn

    async def disconnect(self, conn: WSConnection):
        """Stäng WebSocket-anslutning."""
        async with self._lock:
            if conn in self._connections:
                self._connections.remove(conn)

    async def broadcast(self, channel: str, data: dict,
                        event_type: str = "update"):
        """Broadcast till alla anslutningar på en kanal."""
        event = WSEvent(
            event_type=event_type,
            channel=channel,
            data=data,
        )
        self._log_event(event)

        async with self._lock:
            targets = [c for c in self._connections if channel in c.channels]

        for conn in targets:
            await self._send(conn, event)

    async def send_to_user(self, user_id: str, data: dict,
                           event_type: str = "notification"):
        """Skicka meddelande till specifik användare."""
        event = WSEvent(
            event_type=event_type,
            channel="notify",
            target_user=user_id,
            data=data,
        )
        self._log_event(event)

        async with self._lock:
            targets = [c for c in self._connections if c.user_id == user_id]

        for conn in targets:
            await self._send(conn, event)

    async def broadcast_schedule_change(self, schedule_id: str,
                                        change_type: str, details: dict):
        """Convenience: broadcast schemaändring."""
        await self.broadcast("schedule", {
            "schedule_id": schedule_id,
            "change_type": change_type,
            **details,
        }, event_type=EventType.SHIFT_CHANGED)

    async def broadcast_absence_chain(self, chain_id: str, status: str,
                                       details: dict):
        """Convenience: broadcast frånvarokedja-status."""
        event_type = (EventType.ABSENCE_CHAIN_COMPLETED
                      if status == "completed"
                      else EventType.ABSENCE_CHAIN_STARTED)
        await self.broadcast("absence", {
            "chain_id": chain_id,
            "status": status,
            **details,
        }, event_type=event_type)

    async def broadcast_solver_progress(self, schedule_id: str,
                                         progress_pct: int, message: str):
        """Convenience: broadcast solver-progress."""
        await self.broadcast("dashboard", {
            "schedule_id": schedule_id,
            "progress": progress_pct,
            "message": message,
        }, event_type=EventType.SOLVER_PROGRESS)

    async def broadcast_kpi_update(self, kpis: dict):
        """Convenience: broadcast KPI-uppdatering."""
        await self.broadcast("dashboard", kpis,
                             event_type=EventType.KPI_UPDATE)

    async def notify_atl_warning(self, doctor_id: str, doctor_name: str,
                                  warning: str, user_id: Optional[str] = None):
        """Skicka ATL-varning till schemaläggare."""
        data = {
            "doctor_id": doctor_id,
            "doctor_name": doctor_name,
            "warning": warning,
            "severity": "warning",
        }
        if user_id:
            await self.send_to_user(user_id, data,
                                    event_type=EventType.ATL_WARNING)
        else:
            await self.broadcast("schedule", data,
                                 event_type=EventType.ATL_WARNING)

    async def _send(self, conn: WSConnection, event: WSEvent):
        """Skicka event till en anslutning (med felhantering)."""
        try:
            await conn.websocket.send_text(event.to_json())
        except Exception:
            await self.disconnect(conn)

    def _log_event(self, event: WSEvent):
        """Logga event (rullande buffer)."""
        self._event_log.append(event)
        if len(self._event_log) > self._max_log:
            self._event_log = self._event_log[-self._max_log:]

    def get_recent_events(self, channel: Optional[str] = None,
                          limit: int = 50) -> list[dict]:
        """Hämta senaste events (för reconnect / catch-up)."""
        events = self._event_log
        if channel:
            events = [e for e in events if e.channel == channel]
        return [
            {
                "event": e.event_type,
                "channel": e.channel,
                "data": e.data,
                "timestamp": e.timestamp,
            }
            for e in events[-limit:]
        ]

    def get_stats(self) -> dict:
        """Statistik för WebSocket-hubben."""
        channel_counts = {}
        for conn in self._connections:
            for ch in conn.channels:
                channel_counts[ch] = channel_counts.get(ch, 0) + 1

        return {
            "total_connections": self.connection_count,
            "channels": channel_counts,
            "total_events_logged": len(self._event_log),
        }


# ---------------------------------------------------------------------------
# Global Hub Instance
# ---------------------------------------------------------------------------

hub = WebSocketHub()


# ---------------------------------------------------------------------------
# FastAPI WebSocket Router
# ---------------------------------------------------------------------------

ws_router = APIRouter(tags=["WebSocket"])


@ws_router.websocket("/ws/{channel}")
async def websocket_endpoint(
    websocket: WebSocket,
    channel: str,
    user_id: Optional[str] = Query(None),
    token: Optional[str] = Query(None),
):
    """
    WebSocket endpoint.

    Anslut till en kanal:
      ws://localhost:8000/ws/schedule
      ws://localhost:8000/ws/absence?user_id=usr_123
      ws://localhost:8000/ws/dashboard?token=jwt_token

    Klienten kan skicka:
      {"action": "subscribe", "channel": "absence"}
      {"action": "unsubscribe", "channel": "schedule"}
      {"action": "ping"}
      {"action": "get_history", "channel": "schedule", "limit": 20}
    """
    valid_channels = {"schedule", "absence", "dashboard", "notify", "system"}
    if channel not in valid_channels:
        await websocket.close(code=4001, reason=f"Ogiltig kanal: {channel}")
        return

    # TODO: Validera token om auth krävs
    conn = await hub.connect(websocket, user_id=user_id, channels=[channel])

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({
                    "error": "Ogiltigt JSON-format"
                }))
                continue

            action = msg.get("action", "")

            if action == "ping":
                conn.last_ping = time.time()
                await websocket.send_text(json.dumps({
                    "event": "pong",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }))

            elif action == "subscribe":
                new_channel = msg.get("channel", "")
                if new_channel in valid_channels:
                    conn.channels.add(new_channel)
                    await websocket.send_text(json.dumps({
                        "event": "subscribed",
                        "channel": new_channel,
                    }))

            elif action == "unsubscribe":
                old_channel = msg.get("channel", "")
                conn.channels.discard(old_channel)
                await websocket.send_text(json.dumps({
                    "event": "unsubscribed",
                    "channel": old_channel,
                }))

            elif action == "get_history":
                hist_channel = msg.get("channel", channel)
                limit = min(msg.get("limit", 50), 200)
                events = hub.get_recent_events(hist_channel, limit)
                await websocket.send_text(json.dumps({
                    "event": "history",
                    "channel": hist_channel,
                    "events": events,
                }))

            else:
                await websocket.send_text(json.dumps({
                    "error": f"Okänd action: {action}",
                    "valid_actions": ["ping", "subscribe", "unsubscribe", "get_history"],
                }))

    except WebSocketDisconnect:
        await hub.disconnect(conn)


@ws_router.get("/ws/stats")
async def ws_stats():
    """WebSocket-statistik."""
    return hub.get_stats()
