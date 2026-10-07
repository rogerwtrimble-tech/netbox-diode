"""Compare desired state (snapshot) with actual state (NetBox).

Pure functions, no I/O, so the reconciliation rules are fully unit-tested.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from .models import DeviceState, InventoryState

DEVICE_FIELDS = ("role", "manufacturer", "model", "platform", "serial", "status")
INTERFACE_FIELDS = ("type", "enabled")


class Action(StrEnum):
    """What reconciliation does with an object."""

    CREATE = "create"  # in source, not in NetBox -> Diode creates it
    UPDATE = "update"  # in both, attribute drift -> Diode updates it
    UNCHANGED = "unchanged"  # in both, identical
    STALE = "stale"  # in NetBox (in scope), not in source -> flagged/reported, never deleted


class ObjectType(StrEnum):
    """Reconciled NetBox object types."""

    DEVICE = "device"
    INTERFACE = "interface"
    IP_ADDRESS = "ip_address"


@dataclass(frozen=True, slots=True)
class Change:
    """One planned (or observed) reconciliation item; UPDATE has one row per field."""

    object_type: ObjectType
    key: str
    action: Action
    field: str = ""
    before: str = ""
    after: str = ""


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

    def pending_device_keys(self) -> set[str]:
        """Device keys owning any pending change (used to re-ingest stragglers)."""
        keys: set[str] = set()
        for c in self.pending:
            if c.object_type == ObjectType.DEVICE:
                keys.add(c.key)
            elif c.object_type == ObjectType.INTERFACE:
                keys.add(device_key_of(c.key))
            elif c.after:
                keys.add(device_key_of(c.after))
        return keys


def device_key_of(interface_key: str) -> str:
    """'site/device/Ethernet1/1' -> 'site/device' (site and device names never contain '/')."""
    return "/".join(interface_key.split("/", 2)[:2])


def _fmt(value: object) -> str:
    return "" if value is None else str(value).lower() if isinstance(value, bool) else str(value)


def compute_plan(desired: InventoryState, actual: InventoryState, scope_sites: frozenset[str]) -> Plan:
    """Compute the reconciliation plan.

    Scope: only NetBox objects in ``scope_sites`` can be STALE, so one source
    never flags inventory owned by another site/source.
    """
    changes: list[Change] = []
    in_scope = {k: d for k, d in actual.devices.items() if d.site in scope_sites}

    # Devices
    for key, want in sorted(desired.devices.items()):
        have = actual.devices.get(key)
        if have is None:
            changes.append(Change(ObjectType.DEVICE, key, Action.CREATE, after=want.model))
            continue
        drift = [
            Change(ObjectType.DEVICE, key, Action.UPDATE, f, _fmt(getattr(have, f)), _fmt(getattr(want, f)))
            for f in DEVICE_FIELDS
            if getattr(want, f) is not None and getattr(have, f) != getattr(want, f)
        ]
        changes.extend(drift or [Change(ObjectType.DEVICE, key, Action.UNCHANGED)])
    stale_devices = sorted(set(in_scope) - set(desired.devices))
    changes.extend(Change(ObjectType.DEVICE, k, Action.STALE, "status", in_scope[k].status) for k in stale_devices)

    # Interfaces (stale only reported on devices the source still owns)
    for key, want_if in sorted(desired.interfaces.items()):
        have_if = actual.interfaces.get(key)
        if have_if is None:
            changes.append(Change(ObjectType.INTERFACE, key, Action.CREATE, after=want_if.type))
            continue
        drift = [
            Change(
                ObjectType.INTERFACE,
                key,
                Action.UPDATE,
                f,
                _fmt(getattr(have_if, f)),
                _fmt(getattr(want_if, f)),
            )
            for f in INTERFACE_FIELDS
            if getattr(have_if, f) != getattr(want_if, f)
        ]
        changes.extend(drift or [Change(ObjectType.INTERFACE, key, Action.UNCHANGED)])
    changes.extend(
        Change(ObjectType.INTERFACE, k, Action.STALE)
        for k, i in sorted(actual.interfaces.items())
        if i.device_key in desired.devices and k not in desired.interfaces
    )

    # IP addresses
    for addr, want_ip in sorted(desired.ips.items()):
        have_ip = actual.ips.get(addr)
        if have_ip is None:
            changes.append(
                Change(ObjectType.IP_ADDRESS, addr, Action.CREATE, "assigned_to", "", _fmt(want_ip.interface_key))
            )
        elif have_ip.interface_key != want_ip.interface_key:
            changes.append(
                Change(
                    ObjectType.IP_ADDRESS,
                    addr,
                    Action.UPDATE,
                    "assigned_to",
                    _fmt(have_ip.interface_key),
                    _fmt(want_ip.interface_key),
                )
            )
        else:
            changes.append(Change(ObjectType.IP_ADDRESS, addr, Action.UNCHANGED))
    owned_prefixes = tuple(f"{k}/" for k in desired.devices)
    changes.extend(
        Change(ObjectType.IP_ADDRESS, a, Action.STALE, "assigned_to", _fmt(ip.interface_key))
        for a, ip in sorted(actual.ips.items())
        if a not in desired.ips and ip.interface_key and ip.interface_key.startswith(owned_prefixes)
    )
    return Plan(tuple(changes))


def is_flagged(device: DeviceState, stale_status: str, stale_tag: str) -> bool:
    """True when a stale device already carries the stale marker."""
    return device.status == stale_status and stale_tag in device.tags
