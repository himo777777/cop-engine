"""
TimeCare-adapter (Evimeria) — REST API-integration.
OAuth2 client credentials + JSON REST.
"""

import asyncio
import time
from datetime import date, datetime
from typing import Optional

from data_model import Doctor, OperatingRoom, Role, Function, ShiftType, is_jour
from adapters.base import (
    BaseAdapter, AdapterConfig, AdapterType, SyncDirection, SyncResult, ExternalScheduleEntry,
)


class TimeCareAdapter(BaseAdapter):
    """Adapter för TimeCare (Evimeria) REST API."""

    def __init__(self, config: AdapterConfig):
        super().__init__(config)
        self._token: Optional[str] = None
        self._token_expires: float = 0
        self._session = None
        self._base_url = f"https://{config.host}:{config.port or 443}/api"

    @property
    def adapter_type(self) -> AdapterType:
        return AdapterType.TIME_CARE

    # --- Anslutning ---

    async def connect(self) -> bool:
        """Anslut via OAuth2 client credentials."""
        try:
            await self._ensure_token()
            self._connected = True
            return True
        except Exception as e:
            self._connected = False
            raise ConnectionError(f"TimeCare: kunde inte ansluta — {e}")

    async def disconnect(self):
        if self._session:
            await self._session.close()
            self._session = None
        self._connected = False
        self._token = None

    async def test_connection(self) -> dict:
        await self._ensure_token()
        return {
            "system_name": "TimeCare (Evimeria)",
            "base_url": self._base_url,
            "authenticated": self._token is not None,
            "token_expires_in": max(0, int(self._token_expires - time.time())),
        }

    # --- OAuth2 ---

    async def _ensure_token(self):
        """Hämta eller förnya OAuth2-token."""
        if self._token and time.time() < self._token_expires - 60:
            return
        data = await self._request("POST", "/oauth/token", json={
            "grant_type": "client_credentials",
            "client_id": self.config.username,
            "client_secret": self.config.password,
        }, auth=False)
        self._token = data.get("access_token")
        self._token_expires = time.time() + data.get("expires_in", 3600)

    # --- HTTP med retry ---

    async def _request(self, method: str, path: str, json=None, params=None, auth=True, retries=3):
        """HTTP-anrop med exponential backoff och rate limiting."""
        import aiohttp
        if not self._session:
            self._session = aiohttp.ClientSession()

        headers = {}
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        url = f"{self._base_url}{path}"
        for attempt in range(retries):
            try:
                async with self._session.request(method, url, json=json, params=params, headers=headers) as resp:
                    if resp.status == 429:  # Rate limited
                        wait = min(2 ** attempt * 2, 30)
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except Exception as e:
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(2 ** attempt)
        return {}

    # --- Pull ---

    async def pull_doctors(self) -> list[Doctor]:
        """Hämta läkare från TimeCare."""
        data = await self._request("GET", "/employees", params={"department": self.config.role_mapping.get("_department", "")})
        employees = data.get("employees", data if isinstance(data, list) else [])
        doctors = []
        for emp in employees:
            role_code = str(emp.get("role_code", emp.get("category", "")))
            role = self.map_role(role_code)
            if not role:
                continue
            doctors.append(Doctor(
                id=str(emp.get("employee_id", emp.get("id", ""))),
                name=emp.get("name", f"{emp.get('first_name', '')} {emp.get('last_name', '')}".strip()),
                role=role,
                employment_rate=emp.get("employment_rate", 1.0),
                can_primary_call=emp.get("can_primary_call", role in (Role.ST_SEN, Role.SPECIALIST)),
                can_backup_call=emp.get("can_backup_call", role in (Role.SPECIALIST, Role.ÖVERLÄKARE)),
                site_preference=self.map_site(str(emp.get("site_code", ""))),
            ))
        return doctors

    async def pull_rooms(self) -> list[OperatingRoom]:
        data = await self._request("GET", "/rooms")
        rooms_list = data.get("rooms", data if isinstance(data, list) else [])
        return [
            OperatingRoom(
                id=str(r.get("room_id", r.get("id", ""))),
                site=self.map_site(str(r.get("site_code", ""))) or "Site_A",
                name=r.get("name", ""),
                available_days=r.get("available_days", [0, 1, 2, 3, 4]),
            )
            for r in rooms_list
        ]

    async def pull_schedule(self, start_date: date, end_date: date) -> list[ExternalScheduleEntry]:
        data = await self._request("GET", "/schedules", params={
            "from": start_date.isoformat(),
            "to": end_date.isoformat(),
        })
        entries = data.get("shifts", data if isinstance(data, list) else [])
        result = []
        for e in entries:
            func_code = self._map_shift_code(e.get("shift_code", 0))
            result.append(ExternalScheduleEntry(
                external_id=str(e.get("shift_id", "")),
                doctor_external_id=str(e.get("employee_id", "")),
                date=e.get("date", ""),
                function_code=func_code,
                site_code=str(e.get("site_code", "")),
                start_time=e.get("start_time", ""),
                end_time=e.get("end_time", ""),
                raw_data=e,
            ))
        return result

    async def pull_absences(self, start_date: date, end_date: date) -> list[dict]:
        data = await self._request("GET", "/absences", params={
            "from": start_date.isoformat(),
            "to": end_date.isoformat(),
        })
        return data.get("absences", data if isinstance(data, list) else [])

    # --- Push ---

    async def push_schedule(self, schedule: dict, start_date: date) -> SyncResult:
        items_ok, items_fail, errors = 0, 0, []
        for doc_id, days in schedule.items():
            for day_idx, func in days.items():
                if func == "LEDIG":
                    continue
                shift_date = start_date.isoformat() if isinstance(start_date, date) else start_date
                try:
                    await self._request("PUT", f"/schedules/{doc_id}", json={
                        "employee_id": doc_id,
                        "date": shift_date,
                        "shift_code": self._func_to_shift_code(func),
                    })
                    items_ok += 1
                except Exception as e:
                    items_fail += 1
                    errors.append(str(e))
        return SyncResult(
            success=items_fail == 0, direction=SyncDirection.PUSH,
            adapter_type=self.adapter_type, items_synced=items_ok,
            items_failed=items_fail, errors=errors,
        )

    async def push_absence(self, doctor_id, absence_type, start_date, end_date) -> SyncResult:
        try:
            await self._request("POST", "/absences", json={
                "employee_id": doctor_id,
                "type": absence_type,
                "from": str(start_date),
                "to": str(end_date),
            })
            return SyncResult(success=True, direction=SyncDirection.PUSH, adapter_type=self.adapter_type, items_synced=1)
        except Exception as e:
            return SyncResult(success=False, direction=SyncDirection.PUSH, adapter_type=self.adapter_type, errors=[str(e)])

    # --- Mappning ---

    def _map_shift_code(self, code) -> str:
        """TimeCare numeriska koder → COP func_id."""
        mapping = {1: "DAG", 2: "JOUR_P", 3: "JOUR_B", 4: "LEDIG", 5: "OP", 6: "AVD", 7: "MOTT"}
        return mapping.get(int(code), "LEDIG") if code else "LEDIG"

    def _func_to_shift_code(self, func: str) -> int:
        """COP func_id → TimeCare numerisk kod."""
        if func.startswith("OP"):
            return 5
        if func.startswith("AVD"):
            return 6
        if func.startswith("MOTT"):
            return 7
        if func == "JOUR_B" or func.startswith("JOUR_B"):
            return 3
        if func == "JOUR_P" or is_jour(func):
            return 2
        return 1

    def _default_role_mapping(self):
        return {"1": "ÖL", "2": "SP", "3": "ST_SEN", "4": "ST_TIDIG", "5": "UL",
                "ÖL": "ÖL", "SP": "SP", "ST": "ST_SEN", "UL": "UL"}

    def _default_site_mapping(self):
        return {"1": "CSK", "2": "Hässleholm"}

    def _default_function_mapping(self):
        return {"1": "DAG", "2": "JOUR_P", "3": "JOUR_B", "5": "OP", "6": "AVD", "7": "MOTT"}
