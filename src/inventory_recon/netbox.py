"""Read-only NetBox REST client that returns normalized InventoryState.

All writes go through Diode; this client never modifies NetBox.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx

from .config import Settings
from .models import (
    CableState,
    DeviceState,
    InterfaceState,
    InventoryState,
    IPState,
    PrefixState,
    VLANState,
)

PAGE_SIZE = 500
ID_CHUNK = 100  # ids per filter request (keeps URLs well under server limits)
CF_STATE, CF_STALE_SINCE = "recon_state", "recon_stale_since"
_DEVICE_FIELDS = "id,name,site,role,device_type,platform,serial,status,custom_fields"
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

    def change_count(self, user: str | None = "diode") -> int:
        """Number of NetBox changelog entries made by ``user`` (default: the Diode plugin user; None = all)."""
        params: dict[str, Any] = {"limit": 1} | ({"user_name": user} if user else {})
        return int(self._get("/api/core/object-changes/", params=params)["count"])

    def site_ids(self, names: frozenset[str]) -> list[int]:
        """NetBox ids of the named sites (missing sites are simply absent)."""
        return [s["id"] for s in self._paginate("/api/dcim/sites/", name=sorted(names), fields="id")]

    def fetch_state(self, scope_sites: frozenset[str] | None = None) -> InventoryState:
        """Current state; with ``scope_sites`` only those sites are read (filtered server-side).

        Only the fields used for comparison are requested (``fields=``), which keeps
        payloads small at scale.
        """
        custom_fields = frozenset(
            c["name"]
            for c in self._paginate("/api/extras/custom-fields/", name=[CF_STATE, CF_STALE_SINCE], fields="name")
        )
        scope: dict[str, Any] = {}
        if scope_sites is not None:
            site_ids = self.site_ids(scope_sites)
            if not site_ids:
                return InventoryState(custom_fields=custom_fields)
            scope = {"site_id": site_ids}
        ids: dict[str, int] = {}
        devices = self._devices(scope, ids)
        dev_by_id = {ids[f"device:{k}"]: k for k in devices}
        interfaces = self._interfaces(scope, ids, dev_by_id)
        if_by_id = {ids[f"interface:{k}"]: k for k in interfaces}
        vlans = self._vlans(scope, ids)
        return InventoryState(
            devices=devices,
            interfaces=interfaces,
            ips=self._ips(scope, ids, sorted(dev_by_id), if_by_id),
            vlans=vlans,
            prefixes=self._prefixes(scope, ids, {ids[f"vlan:{k}"]: k for k in vlans}),
            cables=self._cables(scope, ids, if_by_id),
            ids=ids,
            custom_fields=custom_fields,
        )

    def _devices(self, scope: dict[str, Any], ids: dict[str, int]) -> dict[str, DeviceState]:
        out = {}
        for d in self._paginate("/api/dcim/devices/", fields=_DEVICE_FIELDS, **scope):
            if not d.get("name") or not d.get("site"):
                continue
            cf = d.get("custom_fields") or {}
            st = DeviceState(
                site=d["site"]["name"],
                name=d["name"],
                role=(d.get("role") or {}).get("name", ""),
                manufacturer=d["device_type"]["manufacturer"]["name"],
                model=d["device_type"]["model"],
                platform=(d.get("platform") or {}).get("name"),
                serial=d.get("serial") or "",
                status=d["status"]["value"],
                recon_state=cf.get(CF_STATE),
                stale_since=date.fromisoformat(cf[CF_STALE_SINCE]) if cf.get(CF_STALE_SINCE) else None,
            )
            out[st.key] = st
            ids[f"device:{st.key}"] = d["id"]
        return out

    def _interfaces(
        self, scope: dict[str, Any], ids: dict[str, int], dev_by_id: dict[int, str]
    ) -> dict[str, InterfaceState]:
        out = {}
        for i in self._paginate("/api/dcim/interfaces/", fields="id,name,type,enabled,device", **scope):
            if (dev_key := dev_by_id.get(i["device"]["id"])) is None:
                continue
            st = InterfaceState(device_key=dev_key, name=i["name"], type=i["type"]["value"], enabled=i["enabled"])
            out[st.key] = st
            ids[f"interface:{st.key}"] = i["id"]
        return out

    def _ips(
        self, scope: dict[str, Any], ids: dict[str, int], device_ids: list[int], if_by_id: dict[int, str]
    ) -> dict[str, IPState]:
        fields = "id,address,assigned_object_type,assigned_object_id"
        if scope:  # IPs have no site: read those assigned to in-scope devices, in chunks
            pages = [
                self._paginate("/api/ipam/ip-addresses/", fields=fields, device_id=device_ids[n : n + ID_CHUNK])
                for n in range(0, len(device_ids), ID_CHUNK)
            ]
        else:
            pages = [self._paginate("/api/ipam/ip-addresses/", fields=fields)]
        out = {}
        for ip in (row for page in pages for row in page):
            assigned = (
                if_by_id.get(ip["assigned_object_id"]) if ip.get("assigned_object_type") == "dcim.interface" else None
            )
            out[ip["address"]] = IPState(address=ip["address"], interface_key=assigned)
            ids[f"ip_address:{ip['address']}"] = ip["id"]
        return out

    def _vlans(self, scope: dict[str, Any], ids: dict[str, int]) -> dict[str, VLANState]:
        out = {}
        for v in self._paginate("/api/ipam/vlans/", fields="id,vid,name,status,site", **scope):
            if v.get("site"):
                st = VLANState(site=v["site"]["name"], vid=v["vid"], name=v["name"], status=v["status"]["value"])
                out[st.key] = st
                ids[f"vlan:{st.key}"] = v["id"]
        return out

    def _prefixes(
        self, scope: dict[str, Any], ids: dict[str, int], vlan_by_id: dict[int, str]
    ) -> dict[str, PrefixState]:
        out = {}
        for p in self._paginate("/api/ipam/prefixes/", fields="id,prefix,status,scope_type,scope,vlan", **scope):
            site = (p.get("scope") or {}).get("name") if p.get("scope_type") == "dcim.site" else None
            vlan = vlan_by_id.get((p.get("vlan") or {}).get("id", -1))
            out[p["prefix"]] = PrefixState(prefix=p["prefix"], site=site, vlan=vlan, status=p["status"]["value"])
            ids[f"prefix:{p['prefix']}"] = p["id"]
        return out

    def _cables(self, scope: dict[str, Any], ids: dict[str, int], if_by_id: dict[int, str]) -> dict[str, CableState]:
        out = {}
        for c in self._paginate("/api/dcim/cables/", fields="id,status,a_terminations,b_terminations", **scope):
            ends = [_cable_end(c[side], if_by_id) for side in ("a_terminations", "b_terminations")]
            st = CableState(ends=(min(ends), max(ends)), status=c["status"]["value"])
            out[st.key] = st
            ids[f"cable:{st.key}"] = c["id"]
        return out


def _cable_end(terminations: list[dict[str, Any]], if_key_by_id: dict[int, str]) -> str:
    """Interface key of a cable end; ends outside the read scope are kept but marked."""
    keys = []
    for t in terminations:
        obj = t.get("object") or {}
        known = if_key_by_id.get(t.get("object_id", -1)) if t.get("object_type") == "dcim.interface" else None
        keys.append(known or f"?/{(obj.get('device') or {}).get('name', '?')}/{obj.get('name', t.get('object_id'))}")
    return "+".join(sorted(keys)) or "?"
