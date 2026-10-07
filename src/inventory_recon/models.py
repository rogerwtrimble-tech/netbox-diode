"""Inventory snapshot schema (input) and normalized state (for comparison)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from ipaddress import IPv4Interface
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

DeviceStatus = Literal["active", "planned", "staged", "failed", "offline", "inventory", "decommissioning"]
# Site/device names are path segments of object keys, so they cannot contain "/".
Name = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
IfName = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/:-]*$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class InterfaceRecord(_Strict):
    """One interface as reported by a source."""

    name: IfName
    type: str = Field(min_length=1, description="NetBox interface type slug, e.g. 1000base-t")
    enabled: bool = True
    ip: IPv4Interface | None = None


class DeviceRecord(_Strict):
    """One device as reported by a source."""

    name: Name
    site: Name
    role: str = Field(min_length=1)
    manufacturer: str = Field(min_length=1)
    model: str = Field(min_length=1)
    platform: str | None = None
    serial: str = Field(min_length=1, max_length=50)
    status: DeviceStatus = "active"
    interfaces: tuple[InterfaceRecord, ...] = ()

    @property
    def key(self) -> str:
        """Identity used by Diode and by this tool: site/name."""
        return f"{self.site}/{self.name}"

    @model_validator(mode="after")
    def _unique_interfaces(self) -> DeviceRecord:
        dupes = [n for n, c in Counter(i.name for i in self.interfaces).items() if c > 1]
        if dupes:
            raise ValueError(f"{self.name}: duplicate interfaces {dupes}")
        return self


class InventorySnapshot(_Strict):
    """A complete inventory export from one source at one point in time."""

    source: str = Field(pattern=r"^[a-z0-9-]+$", description="Producer id, e.g. cmdb or discovery")
    collected_at: datetime
    devices: tuple[DeviceRecord, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _integrity(self) -> InventorySnapshot:
        errors: list[str] = []
        for label, values in (
            ("device", [d.key for d in self.devices]),
            ("serial", [d.serial for d in self.devices]),
            ("ip", [str(i.ip) for d in self.devices for i in d.interfaces if i.ip]),
        ):
            dupes = sorted(v for v, c in Counter(values).items() if c > 1)
            if dupes:
                errors.append(f"duplicate {label}: {', '.join(dupes)}")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    @property
    def sites(self) -> frozenset[str]:
        """Sites covered by this snapshot; reconciliation scope."""
        return frozenset(d.site for d in self.devices)

    @classmethod
    def load(cls, path: Path) -> InventorySnapshot:
        """Load and validate a snapshot JSON file."""
        return cls.model_validate_json(path.read_text())


# ---------------------------------------------------------------- normalized state
class DeviceState(_Strict):
    """Comparable view of a device, from a snapshot or from NetBox."""

    site: str
    name: str
    role: str
    manufacturer: str
    model: str
    platform: str | None
    serial: str
    status: str
    tags: frozenset[str] = frozenset()

    @property
    def key(self) -> str:
        """site/name identity."""
        return f"{self.site}/{self.name}"


class InterfaceState(_Strict):
    """Comparable view of an interface."""

    device_key: str
    name: str
    type: str
    enabled: bool

    @property
    def key(self) -> str:
        """site/device/interface identity."""
        return f"{self.device_key}/{self.name}"


class IPState(_Strict):
    """Comparable view of an IP address and where it is assigned."""

    address: str
    interface_key: str | None


class InventoryState(_Strict):
    """Devices, interfaces and IPs keyed for comparison."""

    devices: dict[str, DeviceState] = {}
    interfaces: dict[str, InterfaceState] = {}
    ips: dict[str, IPState] = {}

    @classmethod
    def from_snapshot(cls, snap: InventorySnapshot) -> InventoryState:
        """Desired state described by a snapshot."""
        devices, interfaces, ips = {}, {}, {}
        for d in snap.devices:
            devices[d.key] = DeviceState(
                site=d.site,
                name=d.name,
                role=d.role,
                manufacturer=d.manufacturer,
                model=d.model,
                platform=d.platform,
                serial=d.serial,
                status=d.status,
            )
            for i in d.interfaces:
                st = InterfaceState(device_key=d.key, name=i.name, type=i.type, enabled=i.enabled)
                interfaces[st.key] = st
                if i.ip:
                    ips[str(i.ip)] = IPState(address=str(i.ip), interface_key=st.key)
        return cls(devices=devices, interfaces=interfaces, ips=ips)
