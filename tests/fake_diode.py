"""In-memory stand-in for Diode + NetBox with Diode's observed semantics.

Matches devices on site+name, applies partial updates (unset fields untouched),
merges tags, upserts interfaces and IPs. Used to test convergence logic offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from inventory_recon.models import DeviceState, InterfaceState, InventoryState, IPState


@dataclass
class FakeResponse:
    errors: list[str] = field(default_factory=list)


class FakeDiode:
    def __init__(self, state: InventoryState | None = None, drop_calls: int = 0, errors: list[str] | None = None):
        self.state = state or InventoryState()
        self.drop_calls = drop_calls  # first N ingest calls are accepted but lost
        self.calls = 0
        self.writes = 0
        self._errors = errors or []

    def read_state(self) -> InventoryState:
        return self.state

    def change_count(self) -> int:
        return self.writes

    def ingest(self, entities: Any) -> FakeResponse:
        self.calls += 1
        if self.calls <= self.drop_calls:
            return FakeResponse()
        devices, interfaces, ips = dict(self.state.devices), dict(self.state.interfaces), dict(self.state.ips)
        for e in entities:
            kind = e.WhichOneof("entity")
            if kind == "device":
                self._device(devices, e.device)
            elif kind == "interface":
                self._interface(devices, interfaces, e.interface)
            elif kind == "ip_address":
                ifk = self._interface(devices, interfaces, e.ip_address.assigned_object_interface)
                new = IPState(address=e.ip_address.address, interface_key=ifk)
                if ips.get(new.address) != new:
                    ips[new.address] = new
                    self.writes += 1
        self.state = InventoryState(devices=devices, interfaces=interfaces, ips=ips)
        return FakeResponse(list(self._errors))

    def _device(self, devices: dict[str, DeviceState], d: Any) -> str:
        key = f"{d.site.name}/{d.name}"
        cur = devices.get(key)
        upd: dict[str, Any] = {"tags": (cur.tags if cur else frozenset()) | {t.name for t in d.tags}}
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
        new = cur.model_copy(update=upd) if cur else DeviceState(site=d.site.name, name=d.name, **upd)
        if new != cur:
            devices[key] = new
            self.writes += 1
        return key

    def _interface(self, devices: dict[str, DeviceState], interfaces: dict[str, InterfaceState], i: Any) -> str:
        dev_key = self._device(devices, i.device)
        new = InterfaceState(device_key=dev_key, name=i.name, type=i.type, enabled=i.enabled)
        if interfaces.get(new.key) != new:
            interfaces[new.key] = new
            self.writes += 1
        return new.key
