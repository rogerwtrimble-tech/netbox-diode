"""Inventory snapshot schema (input) and normalized state (for comparison)."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from ipaddress import IPv4Interface, IPv4Network
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

DeviceStatus = Literal["active", "planned", "staged", "failed", "offline", "inventory", "decommissioning"]
IPAMStatus = Literal["active", "reserved", "deprecated"]
CableStatus = Literal["connected", "planned", "decommissioning"]
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


class VLANRecord(_Strict):
    """A site-scoped VLAN."""

    site: Name
    vid: int = Field(ge=1, le=4094)
    name: str = Field(min_length=1, max_length=64)
    status: IPAMStatus = "active"

    @property
    def key(self) -> str:
        """site/vid identity."""
        return f"{self.site}/{self.vid}"


class PrefixRecord(_Strict):
    """A prefix scoped to a site, optionally bound to one of that site's VLANs."""

    prefix: IPv4Network
    site: Name
    vlan: int | None = Field(default=None, ge=1, le=4094, description="VID of a VLAN at the same site")
    status: Literal["active", "reserved", "deprecated", "container"] = "active"

    @property
    def key(self) -> str:
        """The prefix itself (global VRF)."""
        return str(self.prefix)


class CableEnd(_Strict):
    """One end of a cable: an interface on a device."""

    site: Name
    device: Name
    interface: IfName

    @property
    def key(self) -> str:
        """site/device/interface (same form as InterfaceState.key)."""
        return f"{self.site}/{self.device}/{self.interface}"


class CableRecord(_Strict):
    """A point-to-point cable between two interfaces."""

    a: CableEnd
    b: CableEnd
    status: CableStatus = "connected"

    @property
    def ends(self) -> tuple[str, str]:
        """Endpoints in canonical (sorted) order; a cable has no direction."""
        x, y = sorted((self.a.key, self.b.key))
        return x, y

    @property
    def key(self) -> str:
        """Order-independent identity."""
        return cable_key(*self.ends)


def cable_key(a: str, b: str) -> str:
    """Canonical cable identity from two interface keys."""
    x, y = sorted((a, b))
    return f"{x} <> {y}"


class InventorySnapshot(_Strict):
    """A complete inventory export from one source at one point in time."""

    source: str = Field(pattern=r"^[a-z0-9-]+$", description="Producer id, e.g. cmdb or discovery")
    collected_at: datetime
    devices: tuple[DeviceRecord, ...] = Field(min_length=1)
    vlans: tuple[VLANRecord, ...] = ()
    prefixes: tuple[PrefixRecord, ...] = ()
    cables: tuple[CableRecord, ...] = ()

    @model_validator(mode="after")
    def _integrity(self) -> InventorySnapshot:
        errors: list[str] = []
        for label, values in (
            ("device", [d.key for d in self.devices]),
            ("serial", [d.serial for d in self.devices]),
            ("ip", [str(i.ip) for d in self.devices for i in d.interfaces if i.ip]),
            ("vlan", [v.key for v in self.vlans]),
            ("prefix", [p.key for p in self.prefixes]),
            ("cable endpoint", [e for c in self.cables for e in c.ends]),
        ):
            dupes = sorted(v for v, c in Counter(values).items() if c > 1)
            if dupes:
                errors.append(f"duplicate {label}: {', '.join(dupes)}")
        interfaces = {f"{d.key}/{i.name}" for d in self.devices for i in d.interfaces}
        if unknown := sorted({e for c in self.cables for e in c.ends} - interfaces):
            errors.append(f"cable endpoint not in snapshot: {', '.join(unknown)}")
        vlans = {v.key for v in self.vlans}
        if bad := sorted(p.key for p in self.prefixes if p.vlan and f"{p.site}/{p.vlan}" not in vlans):
            errors.append(f"prefix references unknown VLAN: {', '.join(bad)}")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    @property
    def sites(self) -> frozenset[str]:
        """Sites covered by this snapshot; reconciliation scope."""
        return frozenset(
            [d.site for d in self.devices] + [v.site for v in self.vlans] + [p.site for p in self.prefixes]
        )

    @classmethod
    def load(cls, path: Path) -> InventorySnapshot:
        """Load and validate a snapshot JSON file."""
        return cls.model_validate_json(path.read_text())


# ---------------------------------------------------------------- normalized state
PRESENT, STALE = "present", "stale"  # values of the recon_state custom field


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
    recon_state: str | None = None  # custom field: present | stale (None = never reconciled)
    stale_since: date | None = None  # custom field recon_stale_since

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


class VLANState(_Strict):
    """Comparable view of a VLAN."""

    site: str
    vid: int
    name: str
    status: str

    @property
    def key(self) -> str:
        """site/vid identity."""
        return f"{self.site}/{self.vid}"


class PrefixState(_Strict):
    """Comparable view of a prefix."""

    prefix: str
    site: str | None
    vlan: str | None  # VLAN key site/vid
    status: str


class CableState(_Strict):
    """Comparable view of a cable between two interfaces."""

    ends: tuple[str, str]
    status: str

    @property
    def key(self) -> str:
        """Order-independent identity."""
        return cable_key(*self.ends)


class InventoryState(_Strict):
    """All reconciled objects keyed for comparison, plus NetBox ids (for retirement)."""

    devices: dict[str, DeviceState] = {}
    interfaces: dict[str, InterfaceState] = {}
    ips: dict[str, IPState] = {}
    vlans: dict[str, VLANState] = {}
    prefixes: dict[str, PrefixState] = {}
    cables: dict[str, CableState] = {}
    ids: dict[str, int] = Field(default={}, description="'<object_type>:<key>' -> NetBox id")
    custom_fields: frozenset[str] = frozenset()  # names of device custom fields that exist in NetBox

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
                recon_state=PRESENT,
            )
            for i in d.interfaces:
                st = InterfaceState(device_key=d.key, name=i.name, type=i.type, enabled=i.enabled)
                interfaces[st.key] = st
                if i.ip:
                    ips[str(i.ip)] = IPState(address=str(i.ip), interface_key=st.key)
        vlans = {v.key: VLANState(site=v.site, vid=v.vid, name=v.name, status=v.status) for v in snap.vlans}
        prefixes = {
            p.key: PrefixState(
                prefix=p.key, site=p.site, vlan=f"{p.site}/{p.vlan}" if p.vlan else None, status=p.status
            )
            for p in snap.prefixes
        }
        cables = {c.key: CableState(ends=c.ends, status=c.status) for c in snap.cables}
        return cls(devices=devices, interfaces=interfaces, ips=ips, vlans=vlans, prefixes=prefixes, cables=cables)
