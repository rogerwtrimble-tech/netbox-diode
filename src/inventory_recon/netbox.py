"""Read-only NetBox REST client that returns normalized InventoryState.

All writes go through Diode; this client never modifies NetBox.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import httpx

from .config import Settings
from .models import DeviceState, InterfaceState, InventoryState, IPState

PAGE_SIZE = 500
_RETRY_STATUS = {429, 502, 503, 504}


class NetBoxError(RuntimeError):
    """NetBox could not be read."""


class NetBoxReader:
    """Paginated, retrying reader for the objects this tool reconciles."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(
            base_url=str(settings.netbox_url).rstrip("/"),
            headers={
                "Authorization": f"Bearer {settings.netbox_token.get_secret_value()}",
                "Accept": "application/json",
            },
            timeout=settings.netbox_timeout_s,
            transport=transport,
        )

    def __enter__(self) -> NetBoxReader:
        return self

    def __exit__(self, *_: object) -> None:
        self._http.close()

    def _get(self, url: str, params: dict[str, Any] | None = None, attempts: int = 5) -> dict[str, Any]:
        for attempt in range(1, attempts + 1):
            try:
                resp = self._http.get(url, params=params)
                if resp.status_code not in _RETRY_STATUS:
                    resp.raise_for_status()
                    data: dict[str, Any] = resp.json()
                    return data
            except httpx.TransportError as exc:
                if attempt == attempts:
                    raise NetBoxError(f"GET {url}: {exc}") from exc
            except httpx.HTTPStatusError as exc:
                raise NetBoxError(f"GET {url}: HTTP {exc.response.status_code} {exc.response.text[:200]}") from exc
            time.sleep(min(2**attempt, 10))
        raise NetBoxError(f"GET {url}: still failing after {attempts} attempts")

    def _paginate(self, path: str, **filters: Any) -> Iterator[dict[str, Any]]:
        page = self._get(path, params={"limit": PAGE_SIZE, **filters})
        while True:
            yield from page["results"]
            if not page.get("next"):
                return
            page = self._get(page["next"])

    def status(self) -> dict[str, Any]:
        """NetBox /api/status/ (used as a readiness/auth check)."""
        return self._get("/api/status/")

    def change_count(self, user: str = "diode") -> int:
        """Number of NetBox changelog entries made by ``user`` (the Diode plugin user)."""
        return int(self._get("/api/core/object-changes/", params={"user_name": user, "limit": 1})["count"])

    def fetch_state(self) -> InventoryState:
        """Current devices, interfaces and IP assignments."""
        devices: dict[str, DeviceState] = {}
        key_by_id: dict[int, str] = {}
        for d in self._paginate("/api/dcim/devices/"):
            if not d.get("name") or not d.get("site"):
                continue
            st = DeviceState(
                site=d["site"]["name"],
                name=d["name"],
                role=(d.get("role") or {}).get("name", ""),
                manufacturer=d["device_type"]["manufacturer"]["name"],
                model=d["device_type"]["model"],
                platform=(d.get("platform") or {}).get("name"),
                serial=d.get("serial") or "",
                status=d["status"]["value"],
                tags=frozenset(t["name"] for t in d.get("tags", [])),
            )
            devices[st.key] = st
            key_by_id[d["id"]] = st.key

        interfaces: dict[str, InterfaceState] = {}
        if_key_by_id: dict[int, str] = {}
        for i in self._paginate("/api/dcim/interfaces/"):
            dev_key = key_by_id.get(i["device"]["id"])
            if dev_key is None:
                continue
            ist = InterfaceState(device_key=dev_key, name=i["name"], type=i["type"]["value"], enabled=i["enabled"])
            interfaces[ist.key] = ist
            if_key_by_id[i["id"]] = ist.key

        ips: dict[str, IPState] = {}
        for ip in self._paginate("/api/ipam/ip-addresses/"):
            assigned = None
            if ip.get("assigned_object_type") == "dcim.interface":
                assigned = if_key_by_id.get(ip["assigned_object_id"])
            ips[ip["address"]] = IPState(address=ip["address"], interface_key=assigned)
        return InventoryState(devices=devices, interfaces=interfaces, ips=ips)
