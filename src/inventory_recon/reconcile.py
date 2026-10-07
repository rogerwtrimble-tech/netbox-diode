"""One reconciliation phase: plan -> (review) -> apply via Diode -> converge -> verify."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from netboxlabs.diode.sdk.diode.v1.ingester_pb2 import Entity as EntityPB

from .config import Settings
from .diff import Action, Change, ObjectType, Plan, compute_plan, is_flagged
from .diode import (
    Ingester,
    IngestResult,
    bootstrap_entities,
    cable_entity,
    device_entities,
    ingest,
    prefix_entity,
    stale_entity,
    vlan_entity,
)
from .models import InventorySnapshot, InventoryState
from .netbox import CF_STALE_SINCE, CF_STATE
from .review import ReviewPlan, cascade_rejections, check_review, guard

log = logging.getLogger(__name__)
NON_DEVICE_BATCH = 25  # VLAN/prefix/cable entities per Diode request
REQUIRED_CUSTOM_FIELDS = frozenset({CF_STATE, CF_STALE_SINCE})
_CABLE_DEPENDENCIES = (ObjectType.DEVICE, ObjectType.INTERFACE)

StateReader = Callable[[frozenset[str]], InventoryState]


@dataclass(frozen=True, slots=True)
class Check:
    """Verification of one planned item against NetBox after apply."""

    object_type: str
    key: str
    action: str
    field: str
    expected: str
    actual: str
    result: str  # PASS | FAIL | REPORTED | REJECTED | BLOCKED


@dataclass
class PhaseResult:
    """Everything the reports need about one phase."""

    name: str
    description: str
    source: str
    snapshot_devices: int
    started_at: datetime
    plan: Plan
    kind: str = "reconcile"  # or "retire"
    applied: bool = False
    reviewed: bool = False
    rejected: set[str] = field(default_factory=set)
    ingest: IngestResult = field(default_factory=IngestResult)
    converged: bool = False
    retries: int = 0
    netbox_writes: int | None = None  # changelog entries written during the phase
    duration_s: float = 0.0
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        """Checks that did not pass."""
        return [c for c in self.checks if c.result == "FAIL"]

    @property
    def passed(self) -> bool:
        """Phase verdict."""
        return not self.applied or (self.converged and not self.failures and not self.ingest.errors)


def _site_name(device_key: str) -> tuple[str, str]:
    site, name = device_key.split("/", 1)
    return site, name


def _outstanding(plan: Plan, actual: InventoryState, settings: Settings, rejected: set[str]) -> list[Change]:
    out = [c for c in plan.pending if c.group not in rejected]
    if settings.stale_action == "flag":
        out += [
            c
            for c in plan.of(Action.STALE, ObjectType.DEVICE)
            if c.group not in rejected and not is_flagged(actual.devices[c.key], settings.stale_status)
        ]
    return out


class _Sender:
    """Builds Diode entities for review groups and sends them, accumulating results."""

    def __init__(
        self,
        snapshot: InventorySnapshot,
        settings: Settings,
        client: Ingester,
        result: PhaseResult,
        skip: set[str],
    ) -> None:
        self.snap, self.settings, self.client, self.result, self.skip = snapshot, settings, client, result, skip
        self.devices = {d.key: d for d in snapshot.devices}
        self.vlans = {v.key: v for v in snapshot.vlans}
        self.prefixes = {p.key: p for p in snapshot.prefixes}
        self.cables = {c.key: c for c in snapshot.cables}
        self.all_groups = (
            {f"device:{k}" for k in self.devices}
            | {f"vlan:{k}" for k in self.vlans}
            | {f"prefix:{k}" for k in self.prefixes}
            | {f"cable:{k}" for k in self.cables}
        )

    def _entities(self, group: str) -> list[EntityPB]:
        kind, key = group.split(":", 1)
        src = self.snap.source
        if kind == "device" and key in self.devices:
            return device_entities(self.devices[key], src)
        if kind == "vlan":
            return [vlan_entity(self.vlans[key], src)]
        if kind == "prefix":
            return [prefix_entity(self.prefixes[key], self.vlans, src)]
        if kind == "cable":
            return [cable_entity(self.cables[key], self.devices, src)]
        return []

    def send(self, groups: set[str], stale: list[Change]) -> None:
        """One request per device group; VLANs/prefixes/cables batched; stale flags last."""
        wanted = sorted(groups - self.skip)
        batches = [self._entities(g) for g in wanted if g.startswith("device:")]
        rest = [e for g in wanted if not g.startswith("device:") for e in self._entities(g)]
        batches += [rest[n : n + NON_DEVICE_BATCH] for n in range(0, len(rest), NON_DEVICE_BATCH)]
        if self.settings.stale_action == "flag" and stale:
            since = self.snap.collected_at.date()
            batches.append([stale_entity(*_site_name(c.key), self.settings.stale_status, since) for c in stale])
        r = ingest(self.client, batches)
        self.result.ingest.requests += r.requests
        self.result.ingest.entities += r.entities
        self.result.ingest.errors += r.errors


def _ensure_custom_fields(
    before: InventoryState,
    *,
    client: Ingester,
    read_state: StateReader,
    settings: Settings,
    sleep: Callable[[float], None],
    result: PhaseResult,
) -> bool:
    """Create the reconciliation custom fields through Diode and wait until NetBox has them.

    Devices carry these fields and NetBox rejects data for unknown ones, so they must
    exist before any device is sent (otherwise the first apply races them).
    """
    if before.custom_fields >= REQUIRED_CUSTOM_FIELDS:
        return True
    start = time.monotonic()
    sends = 0
    while True:
        elapsed = time.monotonic() - start
        if sends == 0 or (sends == 1 and elapsed >= settings.converge_timeout_s / 2):
            r = ingest(client, [bootstrap_entities()])  # sent once, re-sent once at half-time
            result.ingest.requests += r.requests
            result.ingest.entities += r.entities
            result.ingest.errors += r.errors
            sends += 1
        if read_state(frozenset()).custom_fields >= REQUIRED_CUSTOM_FIELDS:
            return True
        if elapsed > settings.converge_timeout_s:
            result.ingest.errors.append(f"custom fields {sorted(REQUIRED_CUSTOM_FIELDS)} were not created")
            return False
        sleep(settings.poll_interval_s)


def _converge(
    name: str,
    desired: InventoryState,
    scope: frozenset[str],
    *,
    sender: _Sender,
    cables: set[str],
    rejected: set[str],
    read_state: StateReader,
    settings: Settings,
    sleep: Callable[[float], None],
    result: PhaseResult,
    t0: float,
) -> InventoryState:
    """Poll NetBox until the approved plan has landed; send cables once their devices exist."""
    cables_sent = not cables
    deadline = t0 + settings.converge_timeout_s
    retry_at = t0 + settings.converge_timeout_s / 2
    after = read_state(scope)
    while True:
        post = compute_plan(desired, after, scope)
        outstanding = _outstanding(post, after, settings, rejected)
        now = time.monotonic()
        if not cables_sent:
            waiting = [c for c in outstanding if c.object_type in _CABLE_DEPENDENCIES and c.action != Action.STALE]
            if not waiting or now >= retry_at:
                sender.send(cables, [])
                cables_sent = True
                continue
        elif not outstanding:
            result.converged = True
            return after
        if now >= deadline:
            log.error("[%s] not converged; %d items outstanding", name, len(outstanding))
            return after
        if now >= retry_at and result.retries == 0:
            # Idempotent re-send of only the groups still drifting.
            result.retries = 1
            groups = {c.group for c in outstanding if c.action != Action.STALE}
            log.warning("[%s] re-sending %d outstanding groups", name, len(groups))
            sender.send(groups, [c for c in outstanding if c.action == Action.STALE])
        sleep(settings.poll_interval_s)
        after = read_state(scope)


def run_phase(
    name: str,
    description: str,
    snapshot: InventorySnapshot,
    settings: Settings,
    *,
    read_state: StateReader,
    client: Ingester | None,
    count_changes: Callable[[], int] | None = None,
    review: ReviewPlan | None = None,
    snapshot_sha256: str = "",
    sleep: Callable[[float], None] = time.sleep,
) -> PhaseResult:
    """Run one phase. ``client=None`` is a dry run (plan only, no writes).

    With ``review`` the plan is first checked to be exactly what was reviewed
    (StalePlanError otherwise) and only approved groups are sent. Without it, the
    blast-radius guard applies unless forced (GuardError).
    """
    t0 = time.monotonic()
    scope = snapshot.sites
    writes_before = count_changes() if count_changes else None
    desired = InventoryState.from_snapshot(snapshot)
    before = read_state(scope)
    plan = compute_plan(desired, before, scope)
    result = PhaseResult(
        name=name,
        description=description,
        source=snapshot.source,
        snapshot_devices=len(snapshot.devices),
        started_at=datetime.now(UTC),
        plan=plan,
        reviewed=review is not None,
    )
    log.info("[%s] plan: %s", name, plan.counts())
    if client is None:
        result.duration_s = time.monotonic() - t0
        return result
    if review is not None:
        check_review(review, plan, snapshot.source, snapshot_sha256)
    elif not settings.force:
        existing = sum(len(getattr(before, t)) for t in ("devices", "interfaces", "ips", "vlans", "prefixes", "cables"))
        guard(plan, existing, settings.guard_max_fraction)
    rejected = cascade_rejections(review.rejected(), plan) if review else set()
    result.rejected = rejected
    result.applied = True  # from here on the phase must converge to pass

    if not _ensure_custom_fields(
        before, client=client, read_state=read_state, settings=settings, sleep=sleep, result=result
    ):
        result.duration_s = time.monotonic() - t0
        return result
    sender = _Sender(snapshot, settings, client, result, rejected | plan.groups(Action.BLOCKED))
    # Send the full snapshot (minus rejected/blocked groups): Diode itself decides
    # create/update/no-change, which is what makes re-runs idempotent. Cables go in a
    # second stage, once their devices exist: Diode does not order work across requests.
    cables = {g for g in sender.all_groups if g.startswith("cable:")}
    sender.send(
        sender.all_groups - cables,
        [c for c in _outstanding(plan, before, settings, rejected) if c.action == Action.STALE],
    )
    after = _converge(
        name,
        desired,
        scope,
        sender=sender,
        cables=cables,
        rejected=rejected,
        read_state=read_state,
        settings=settings,
        sleep=sleep,
        result=result,
        t0=t0,
    )

    result.checks = verify(plan, compute_plan(desired, after, scope), after, settings, rejected)
    if count_changes and writes_before is not None:
        result.netbox_writes = count_changes() - writes_before
    result.duration_s = time.monotonic() - t0
    log.info("[%s] converged=%s failures=%d", name, result.converged, len(result.failures))
    return result


def verify(plan: Plan, post: Plan, after: InventoryState, settings: Settings, rejected: set[str]) -> list[Check]:
    """Check every approved planned change landed in NetBox, and nothing else drifted."""
    residual = {(c.object_type, c.key, c.field): c for c in post.pending if c.group not in rejected}
    residual_objects = {(c.object_type, c.key) for c in post.pending if c.group not in rejected}
    checks: list[Check] = []
    for c in plan.changes:
        if c.action == Action.UNCHANGED:
            continue
        if c.group in rejected:
            checks.append(Check(c.object_type, c.key, c.action, c.field, "not applied", "rejected", "REJECTED"))
            continue
        if c.action == Action.BLOCKED:
            checks.append(Check(c.object_type, c.key, c.action, c.field, "retire conflict first", c.before, "BLOCKED"))
            continue
        if c.action == Action.STALE:
            if c.object_type == ObjectType.DEVICE and settings.stale_action == "flag":
                dev = after.devices.get(c.key)
                ok = dev is not None and is_flagged(dev, settings.stale_status)
                actual = f"{dev.status} state={dev.recon_state} since={dev.stale_since}" if dev else "missing"
                expected = f"{settings.stale_status} state=stale"
                checks.append(
                    Check(c.object_type, c.key, c.action, "status", expected, actual, "PASS" if ok else "FAIL")
                )
            else:
                checks.append(Check(c.object_type, c.key, c.action, c.field, "review/retire", c.before, "REPORTED"))
            continue
        left = residual.get((c.object_type, c.key, c.field))
        if c.action == Action.CREATE:
            ok = (c.object_type, c.key) not in residual_objects
            actual = "present" if ok else "missing"
        else:
            ok = left is None
            actual = c.after if ok else left.before  # type: ignore[union-attr]
        checks.append(
            Check(c.object_type, c.key, c.action, c.field, c.after or "present", actual, "PASS" if ok else "FAIL")
        )

    planned = {(c.object_type, c.key) for c in plan.changes if c.action != Action.UNCHANGED}
    regressions = sorted({(t, k) for t, k in residual_objects if (t, k) not in planned})
    checks.extend(Check(t, k, "unchanged", "", "no drift", "drift detected", "FAIL") for t, k in regressions)
    drifting = len(residual_objects)
    checks.append(
        Check(
            "all",
            "*",
            "sweep",
            "",
            "0 approved objects drifting",
            f"{drifting} objects drifting",
            "PASS" if drifting == 0 else "FAIL",
        )
    )
    return checks
