"""Retirement: the only path that deletes, and only with an approved plan.

Diode cannot delete. Stale objects are flagged first (devices get recon_state=stale and
recon_stale_since), then retired in two explicit steps:

    recon retire plan SNAPSHOT --out retire.json   # candidates past the grace period
    recon retire apply retire.json --approve       # human-approved deletes, re-checked first

Every item is re-validated just before deletion (same NetBox id, unchanged state, still
stale), so a plan that has gone out of date can never delete the wrong thing.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from .config import Settings
from .diff import Action, Change, ObjectType, Plan, compute_plan, device_key_of
from .models import STALE, InventorySnapshot, InventoryState
from .reconcile import Check, PhaseResult

log = logging.getLogger(__name__)

# Dependents first: a device delete cascades to its interfaces in NetBox.
ORDER = (
    ObjectType.CABLE,
    ObjectType.IP_ADDRESS,
    ObjectType.PREFIX,
    ObjectType.VLAN,
    ObjectType.INTERFACE,
    ObjectType.DEVICE,
)
ENDPOINT = {
    ObjectType.CABLE: "/api/dcim/cables/",
    ObjectType.IP_ADDRESS: "/api/ipam/ip-addresses/",
    ObjectType.PREFIX: "/api/ipam/prefixes/",
    ObjectType.VLAN: "/api/ipam/vlans/",
    ObjectType.INTERFACE: "/api/dcim/interfaces/",
    ObjectType.DEVICE: "/api/dcim/devices/",
}
_STATE_ATTR = {
    ObjectType.CABLE: "cables",
    ObjectType.IP_ADDRESS: "ips",
    ObjectType.PREFIX: "prefixes",
    ObjectType.VLAN: "vlans",
    ObjectType.INTERFACE: "interfaces",
    ObjectType.DEVICE: "devices",
}


class RetireItem(BaseModel):
    """One object to delete."""

    model_config = ConfigDict(extra="forbid")

    object_type: ObjectType
    key: str
    netbox_id: int
    reason: str
    fingerprint: str
    decision: Literal["approve", "reject"] = "approve"


class RetirePlan(BaseModel):
    """Approval artifact for deletions."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    source: str
    sites: list[str]
    grace_days: int
    created_at: datetime
    items: list[RetireItem]
    waiting: list[str] = []  # stale devices still inside the grace period

    def save(self, path: Path) -> None:
        """Write as pretty JSON (meant to be reviewed by a human)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> RetirePlan:
        """Load a retirement plan."""
        return cls.model_validate_json(path.read_text())


def _obj(state: InventoryState, t: ObjectType, key: str) -> Any:
    return getattr(state, _STATE_ATTR[t]).get(key)


def fingerprint(state: InventoryState, t: ObjectType, key: str) -> str:
    """Short hash of an object's current state (detects edits between plan and delete)."""
    obj = _obj(state, t, key)
    payload = obj.model_dump_json() if obj is not None else "absent"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def plan_retirement(snapshot: InventorySnapshot, state: InventoryState, grace_days: int, today: date) -> RetirePlan:
    """Select stale objects eligible for deletion; devices must be flagged and past the grace period."""
    plan = compute_plan(InventoryState.from_snapshot(snapshot), state, snapshot.sites)
    chosen: dict[tuple[ObjectType, str], str] = {}
    waiting: list[str] = []
    for c in plan.of(Action.STALE):
        if c.object_type != ObjectType.DEVICE:
            chosen.setdefault((c.object_type, c.key), f"absent from {snapshot.source}")
            continue
        dev = state.devices[c.key]
        age = (today - dev.stale_since).days if dev.stale_since else -1
        if dev.recon_state != STALE or age < grace_days:
            waiting.append(f"{c.key} (state={dev.recon_state}, stale {max(age, 0)}d < {grace_days}d)")
            continue
        chosen[(ObjectType.DEVICE, c.key)] = f"stale since {dev.stale_since} ({age}d >= {grace_days}d)"
        # Dependents that would otherwise be orphaned (IPs) or block re-cabling (cables).
        for ck, cab in state.cables.items():
            if any(device_key_of(e) == c.key for e in cab.ends):
                chosen.setdefault((ObjectType.CABLE, ck), f"attached to retired {c.key}")
        for addr, ip in state.ips.items():
            if ip.interface_key and device_key_of(ip.interface_key) == c.key:
                chosen.setdefault((ObjectType.IP_ADDRESS, addr), f"assigned to retired {c.key}")
    items = [
        RetireItem(
            object_type=t,
            key=k,
            netbox_id=state.ids[f"{t}:{k}"],
            reason=reason,
            fingerprint=fingerprint(state, t, k),
        )
        for (t, k), reason in sorted(chosen.items(), key=lambda kv: (ORDER.index(kv[0][0]), kv[0][1]))
        if f"{t}:{k}" in state.ids
    ]
    return RetirePlan(
        source=snapshot.source,
        sites=sorted(snapshot.sites),
        grace_days=grace_days,
        created_at=datetime.now(UTC),
        items=items,
        waiting=sorted(waiting),
    )


class NetBoxDeleter:
    """Minimal NetBox writer used only for approved retirements."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        token = (settings.netbox_write_token or settings.netbox_token).get_secret_value()
        self._http = httpx.Client(
            base_url=str(settings.netbox_url).rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=settings.netbox_timeout_s,
            transport=transport,
        )

    def __enter__(self) -> NetBoxDeleter:
        return self

    def __exit__(self, *_: object) -> None:
        self._http.close()

    def delete(self, t: ObjectType, netbox_id: int, attempts: int = 3) -> str:
        """Delete one object; returns 'deleted', 'gone' (404) or an error string."""
        for attempt in range(1, attempts + 1):
            try:
                resp = self._http.delete(f"{ENDPOINT[t]}{netbox_id}/")
            except httpx.TransportError as exc:
                if attempt == attempts:
                    return f"error: {exc}"
                time.sleep(attempt)
                continue
            if resp.status_code == 204:
                return "deleted"
            if resp.status_code == 404:
                return "gone"
            if resp.status_code not in (429, 502, 503, 504) or attempt == attempts:
                return f"error: HTTP {resp.status_code} {resp.text[:200]}"
            time.sleep(attempt)
        return "error: retries exhausted"


def execute_retirement(
    plan: RetirePlan,
    read_state: Callable[[frozenset[str]], InventoryState],
    delete: Callable[[ObjectType, int], str],
    count_changes: Callable[[], int] | None = None,
    name: str = "retire",
) -> PhaseResult:
    """Delete approved items after re-validating each one; verify they are gone."""
    t0 = time.monotonic()
    scope = frozenset(plan.sites)
    writes_before = count_changes() if count_changes else None
    approved = [i for i in plan.items if i.decision == "approve"]
    result = PhaseResult(
        name=name,
        description=f"approved retirement of {len(approved)} stale objects (grace {plan.grace_days}d)",
        source=plan.source,
        snapshot_devices=0,
        started_at=datetime.now(UTC),
        plan=Plan(tuple(Change(i.object_type, i.key, Action.STALE, group=i.reason) for i in plan.items)),
        kind="retire",
        applied=True,
        reviewed=True,
        rejected={f"{i.object_type}:{i.key}" for i in plan.items if i.decision == "reject"},
    )
    state = read_state(scope)
    outcome: dict[tuple[ObjectType, str], str] = {}
    for item in sorted(approved, key=lambda i: ORDER.index(i.object_type)):
        current_id = state.ids.get(f"{item.object_type}:{item.key}")
        if current_id is None:
            outcome[(item.object_type, item.key)] = "gone"
        elif current_id != item.netbox_id or fingerprint(state, item.object_type, item.key) != item.fingerprint:
            outcome[(item.object_type, item.key)] = "skipped: changed since plan"
        else:
            outcome[(item.object_type, item.key)] = delete(item.object_type, item.netbox_id)
            if outcome[(item.object_type, item.key)].startswith("error"):
                result.ingest.errors.append(f"{item.object_type} {item.key}: {outcome[(item.object_type, item.key)]}")
    after = read_state(scope)
    for item in plan.items:
        if item.decision == "reject":
            result.checks.append(Check(item.object_type, item.key, "retire", "", "kept", "rejected", "REJECTED"))
            continue
        status = outcome[(item.object_type, item.key)]
        still_there = after.ids.get(f"{item.object_type}:{item.key}") is not None
        ok = not still_there and status in ("deleted", "gone")
        actual = status if not still_there else f"{status}; still present"
        result.checks.append(
            Check(item.object_type, item.key, "retire", "", "deleted", actual, "PASS" if ok else "FAIL")
        )
    result.converged = True
    if count_changes and writes_before is not None:
        result.netbox_writes = count_changes() - writes_before
    result.duration_s = time.monotonic() - t0
    return result
