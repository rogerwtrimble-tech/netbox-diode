"""Compare desired state (snapshot) with actual state (NetBox).

Pure functions, no I/O, so the reconciliation rules are fully unit-tested.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .models import STALE, DeviceState, InventoryState

FIELDS: dict[str, tuple[str, ...]] = {
    "device": ("role", "manufacturer", "model", "platform", "serial", "status", "recon_state"),
    "interface": ("type", "enabled"),
    "ip_address": ("interface_key",),
    "vlan": ("name", "status"),
    "prefix": ("site", "vlan", "status"),
    "cable": ("status",),
}


class Action(StrEnum):
    """What reconciliation does with an object."""

    CREATE = "create"  # in source, not in NetBox -> Diode creates it
    UPDATE = "update"  # in both, attribute drift -> Diode updates it
    UNCHANGED = "unchanged"  # in both, identical
    STALE = "stale"  # in NetBox (in scope), not in source -> flagged/reported, retired only on approval
    BLOCKED = "blocked"  # cannot be applied until a conflicting stale object is retired


class ObjectType(StrEnum):
    """Reconciled NetBox object types."""

    DEVICE = "device"
    INTERFACE = "interface"
    IP_ADDRESS = "ip_address"
    VLAN = "vlan"
    PREFIX = "prefix"
    CABLE = "cable"


@dataclass(frozen=True, slots=True)
class Change:
    """One planned (or observed) reconciliation item; UPDATE has one row per field.

    ``group`` is the unit of review and of apply: a device with its interfaces and
    IPs, or a single VLAN, prefix or cable.
    """

    object_type: ObjectType
    key: str
    action: Action
    field: str = ""
    before: str = ""
    after: str = ""
    group: str = ""


@dataclass(frozen=True, slots=True)
class Plan:
    """Result of comparing desired vs actual state."""

    changes: tuple[Change, ...]

    def of(self, action: Action, object_type: ObjectType | None = None) -> list[Change]:
        """Changes filtered by action (and optionally object type)."""
        return [c for c in self.changes if c.action == action and (object_type is None or c.object_type == object_type)]

    def counts(self) -> dict[str, dict[str, int]]:
        """Object counts per type and action (an UPDATE counts once per object)."""
        seen = {(c.object_type, c.key, c.action) for c in self.changes}
        tally = Counter((t, a) for t, _, a in seen)
        return {t.value: {a.value: tally.get((t, a), 0) for a in Action} for t in ObjectType}

    @property
    def pending(self) -> list[Change]:
        """Changes that still require a write (CREATE/UPDATE)."""
        return [c for c in self.changes if c.action in (Action.CREATE, Action.UPDATE)]

    def groups(self, *actions: Action) -> set[str]:
        """Groups owning at least one change with one of ``actions``."""
        return {c.group for c in self.changes if c.action in actions}


def device_key_of(interface_key: str) -> str:
    """'site/device/Ethernet1/1' -> 'site/device' (site and device names never contain '/')."""
    return "/".join(interface_key.split("/", 2)[:2])


def group_of(object_type: ObjectType, key: str, interface_key: str | None = None) -> str:
    """Review/apply unit for an object."""
    if object_type == ObjectType.DEVICE:
        return f"device:{key}"
    if object_type == ObjectType.INTERFACE:
        return f"device:{device_key_of(key)}"
    if object_type == ObjectType.IP_ADDRESS:
        return f"device:{device_key_of(interface_key)}" if interface_key else f"ip_address:{key}"
    return f"{object_type}:{key}"


def _fmt(value: object) -> str:
    return "" if value is None else str(value).lower() if isinstance(value, bool) else str(value)


def _compare(
    object_type: ObjectType,
    desired: Mapping[str, Any],
    actual: Mapping[str, Any],
    stale_if: Callable[[Any], bool],
    group: Callable[[str, Any], str],
) -> list[Change]:
    """Generic keyed comparison: create / update (per field) / unchanged / stale."""
    out: list[Change] = []
    fields = FIELDS[object_type]
    for key, want in sorted(desired.items()):
        have = actual.get(key)
        g = group(key, want)
        if have is None:
            out.append(Change(object_type, key, Action.CREATE, group=g))
            continue
        drift = [
            Change(object_type, key, Action.UPDATE, f, _fmt(getattr(have, f)), _fmt(getattr(want, f)), g)
            for f in fields
            if getattr(want, f) is not None and getattr(have, f) != getattr(want, f)
        ]
        out.extend(drift or [Change(object_type, key, Action.UNCHANGED, group=g)])
    out.extend(
        Change(object_type, key, Action.STALE, group=group(key, have))
        for key, have in sorted(actual.items())
        if key not in desired and stale_if(have)
    )
    return out


def compute_plan(desired: InventoryState, actual: InventoryState, scope_sites: frozenset[str]) -> Plan:
    """Compute the reconciliation plan.

    Scope: only NetBox objects in ``scope_sites`` can be STALE, so one source never
    flags inventory owned by another site/source. Interfaces and IPs can only be
    stale on devices the source still reports.
    """
    owned = set(desired.devices)
    in_scope = {k for k, d in actual.devices.items() if d.site in scope_sites}

    changes = _compare(
        ObjectType.DEVICE,
        desired.devices,
        actual.devices,
        lambda d: d.key in in_scope,
        lambda k, _: group_of(ObjectType.DEVICE, k),
    )
    # Stale devices: show the current status (what flagging will change).
    changes = [
        Change(c.object_type, c.key, c.action, "status", actual.devices[c.key].status, group=c.group)
        if c.action == Action.STALE
        else c
        for c in changes
    ]
    changes += _compare(
        ObjectType.INTERFACE,
        desired.interfaces,
        actual.interfaces,
        lambda i: i.device_key in owned,
        lambda k, _: group_of(ObjectType.INTERFACE, k),
    )
    for c in _compare(
        ObjectType.IP_ADDRESS,
        desired.ips,
        actual.ips,
        lambda ip: bool(ip.interface_key) and device_key_of(ip.interface_key) in owned,
        lambda k, ip: group_of(ObjectType.IP_ADDRESS, k, ip.interface_key),
    ):
        if c.action in (Action.UPDATE, Action.STALE):  # show where the address is assigned today
            before = c.before if c.action == Action.UPDATE else _fmt(actual.ips[c.key].interface_key)
            c = Change(c.object_type, c.key, c.action, "assigned_to", before, c.after, c.group)  # noqa: PLW2901
        changes.append(c)
    changes += _compare(
        ObjectType.VLAN,
        desired.vlans,
        actual.vlans,
        lambda v: v.site in scope_sites,
        lambda k, _: group_of(ObjectType.VLAN, k),
    )
    changes += _compare(
        ObjectType.PREFIX,
        desired.prefixes,
        actual.prefixes,
        lambda p: p.site in scope_sites,
        lambda k, _: group_of(ObjectType.PREFIX, k),
    )

    # Cables: NetBox allows one cable per interface, so a desired cable whose end is
    # held by another (stale) cable is BLOCKED until that cable is retired.
    occupied = {end: key for key, cab in actual.cables.items() for end in cab.ends}
    for c in _compare(
        ObjectType.CABLE,
        desired.cables,
        actual.cables,
        lambda cab: any(device_key_of(e).split("/", 1)[0] in scope_sites for e in cab.ends),
        lambda k, _: group_of(ObjectType.CABLE, k),
    ):
        conflicts = (
            sorted({occupied[e] for e in desired.cables[c.key].ends if e in occupied})
            if (c.action == Action.CREATE)
            else []
        )
        blocked = Change(c.object_type, c.key, Action.BLOCKED, "conflicts_with", "; ".join(conflicts), group=c.group)
        changes.append(blocked if conflicts else c)
    return Plan(tuple(changes))


def is_flagged(device: DeviceState, stale_status: str) -> bool:
    """True when a stale device already carries the stale marker."""
    return device.status == stale_status and device.recon_state == STALE
