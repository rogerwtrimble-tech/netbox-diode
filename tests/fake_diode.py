"""In-memory stand-in for Diode + NetBox with Diode's observed semantics.

Observed against Diode 2.3.1 / NetBox 4.7.2 (see docs/limitations-plan.md):
- devices match on site+name; unset fields are untouched; tags merge;
  custom-field values are replaced
- VLANs match on site+vid, prefixes on prefix, cables on terminations (either order)
- a cable whose interface already has another cable is accepted by the API but
  rejected by NetBox ("Duplicate termination") - nothing changes
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from inventory_recon.models import (
    CableState,
    DeviceState,
    InterfaceState,
    InventoryState,
    IPState,
    PrefixState,
    VLANState,
)


@dataclass
class FakeResponse:
    errors: list[str] = field(default_factory=list)


class _Rejected(Exception):
    """An entity NetBox would reject; the rest of the request still applies."""


class FakeDiode:
    def __init__(
        self,
        state: InventoryState | None = None,
        drop_calls: int = 0,
        errors: list[str] | None = None,
        async_lifo: bool = False,
    ):
        self.state = state or InventoryState()
        self.drop_calls = drop_calls  # first N ingest calls are accepted but lost
        self.calls = 0
        self.writes = 0
        self.failed_applies: list[str] = []
        self._errors = errors or []
        self._next_id = 1
        # async_lifo: requests are accepted, then applied on the next NetBox read in reverse
        # order - an adversarial model of Diode not ordering work across requests.
        self.async_lifo = async_lifo
        self._queue: list[Any] = []

    # -- NetBox side ------------------------------------------------------------
    def read_state(self, scope: frozenset[str] | None = None) -> InventoryState:
        while self._queue:
            self._apply(self._queue.pop())
        return self.state

    def change_count(self) -> int:
        return self.writes

    def delete(self, object_type: str, netbox_id: int) -> str:
        attr = {"ip_address": "ips", "prefix": "prefixes"}.get(object_type, f"{object_type}s")
        key = next((k.split(":", 1)[1] for k, v in self.state.ids.items() if v == netbox_id), None)
        if key is None:
            return "gone"
        objs = dict(getattr(self.state, attr))
        objs.pop(key, None)
        ids = {k: v for k, v in self.state.ids.items() if v != netbox_id}
        update: dict[str, Any] = {attr: objs, "ids": ids}
        if object_type == "device":  # NetBox cascades interfaces
            update["interfaces"] = {k: i for k, i in self.state.interfaces.items() if i.device_key != key}
        self.state = self.state.model_copy(update=update)
        self.writes += 1
        return "deleted"

    # -- Diode side -------------------------------------------------------------
    def ingest(self, entities: Any) -> FakeResponse:
        self.calls += 1
        if self.calls <= self.drop_calls:
            return FakeResponse()
        if self.async_lifo:
            self._queue.append(list(entities))
        else:
            self._apply(entities)
        return FakeResponse(list(self._errors))

    def _apply(self, entities: Any) -> None:
        self._s = {
            k: dict(getattr(self.state, k))
            for k in ("devices", "interfaces", "ips", "vlans", "prefixes", "cables", "ids")
        }
        self._s["custom_fields"] = self.state.custom_fields
        for e in entities:
            try:
                self._entity(e)
            except _Rejected:
                continue
        self.state = InventoryState(**self._s)

    def _entity(self, e: Any) -> None:
        kind = e.WhichOneof("entity")
        if kind == "device":
            self._device(e.device)
        elif kind == "interface":
            self._interface(e.interface)
        elif kind == "ip_address":
            ifk = self._interface(e.ip_address.assigned_object_interface)
            self._put(
                "ips", "ip_address", e.ip_address.address, IPState(address=e.ip_address.address, interface_key=ifk)
            )
        elif kind == "vlan":
            v = e.vlan
            self._put(
                "vlans",
                "vlan",
                f"{v.site.name}/{v.vid}",
                VLANState(site=v.site.name, vid=v.vid, name=v.name, status=v.status or "active"),
            )
        elif kind == "prefix":
            p = e.prefix
            vlan = f"{p.vlan.site.name}/{p.vlan.vid}" if p.HasField("vlan") else None
            self._put(
                "prefixes",
                "prefix",
                p.prefix,
                PrefixState(prefix=p.prefix, site=p.scope_site.name or None, vlan=vlan, status=p.status or "active"),
            )
        elif kind == "cable":
            self._cable(e.cable)
        elif kind == "custom_field":
            self._s["custom_fields"] = self._s["custom_fields"] | {e.custom_field.name}

    def _put(self, attr: str, t: str, key: str, obj: Any) -> None:
        if self._s[attr].get(key) != obj:
            self._s[attr][key] = obj
            self.writes += 1
        if f"{t}:{key}" not in self._s["ids"]:
            self._s["ids"][f"{t}:{key}"] = self._next_id
            self._next_id += 1

    def _device(self, d: Any) -> str:
        key = f"{d.site.name}/{d.name}"
        cur = self._s["devices"].get(key)
        upd: dict[str, Any] = {}
        for attr, val in (
            ("role", d.role.name if d.HasField("role") else None),
            ("manufacturer", d.device_type.manufacturer.name if d.HasField("device_type") else None),
            ("model", d.device_type.model if d.HasField("device_type") else None),
            ("platform", d.platform.name if d.HasField("platform") else None),
            ("serial", d.serial or None),
            ("status", d.status or None),
        ):
            if val is not None:
                upd[attr] = val
        unknown = set(d.custom_fields) - self._s["custom_fields"]
        if unknown:  # NetBox rejects data for custom fields that do not exist yet
            self.failed_applies.append(f"Unknown custom field {sorted(unknown)}: {key}")
            return key
        if "recon_state" in d.custom_fields:
            upd["recon_state"] = d.custom_fields["recon_state"].text
        if "recon_stale_since" in d.custom_fields:
            ts = d.custom_fields["recon_stale_since"].date.ToDatetime()
            upd["stale_since"] = date(ts.year, ts.month, ts.day)
        if cur is None and not {"role", "model"} <= upd.keys():
            self.failed_applies.append(f"device_type/role required: {key}")  # what NetBox says
            raise _Rejected(key)
        new = cur.model_copy(update=upd) if cur else DeviceState(site=d.site.name, name=d.name, **upd)
        self._put("devices", "device", key, new)
        return key

    def _interface(self, i: Any) -> str:
        dev_key = self._device(i.device)
        st = InterfaceState(device_key=dev_key, name=i.name, type=i.type, enabled=i.enabled)
        self._put("interfaces", "interface", st.key, st)
        return st.key

    def _cable(self, c: Any) -> None:
        ends = sorted(self._interface(t.object_interface) for t in [*c.a_terminations, *c.b_terminations])
        st = CableState(ends=(ends[0], ends[1]), status=c.status or "connected")
        held = {e: k for k, cab in self._s["cables"].items() for e in cab.ends if k != st.key}
        if any(e in held for e in st.ends):
            self.failed_applies.append(f"Duplicate termination: {st.key}")
            return
        self._put("cables", "cable", st.key, st)
