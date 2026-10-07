"""One reconciliation phase: plan -> apply via Diode -> converge -> verify."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .config import Settings
from .diff import Action, Change, ObjectType, Plan, compute_plan, is_flagged
from .diode import Ingester, IngestResult, device_entities, ingest, stale_entity
from .models import InventorySnapshot, InventoryState

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Check:
    """Verification of one planned item against NetBox after apply."""

    object_type: str
    key: str
    action: str
    field: str
    expected: str
    actual: str
    result: str  # PASS | FAIL | REPORTED


@dataclass
class PhaseResult:
    """Everything the reports need about one phase."""

    name: str
    description: str
    source: str
    snapshot_devices: int
    started_at: datetime
    plan: Plan
    applied: bool = False
    ingest: IngestResult = field(default_factory=IngestResult)
    converged: bool = False
    retries: int = 0
    netbox_writes: int | None = None  # changelog entries written by Diode during the phase
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


def _outstanding(plan: Plan, actual: InventoryState, settings: Settings) -> list[Change]:
    out = list(plan.pending)
    if settings.stale_action == "flag":
        out += [
            c
            for c in plan.of(Action.STALE, ObjectType.DEVICE)
            if not is_flagged(actual.devices[c.key], settings.stale_status, settings.stale_tag)
        ]
    return out


def run_phase(
    name: str,
    description: str,
    snapshot: InventorySnapshot,
    settings: Settings,
    *,
    read_state: Callable[[], InventoryState],
    client: Ingester | None,
    count_changes: Callable[[], int] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> PhaseResult:
    """Run one phase. ``client=None`` is a dry run (plan only, no writes)."""
    t0 = time.monotonic()
    writes_before = count_changes() if count_changes else None
    desired = InventoryState.from_snapshot(snapshot)
    before = read_state()
    plan = compute_plan(desired, before, snapshot.sites)
    result = PhaseResult(
        name=name,
        description=description,
        source=snapshot.source,
        snapshot_devices=len(snapshot.devices),
        started_at=datetime.now(UTC),
        plan=plan,
    )
    log.info("[%s] plan: %s", name, plan.counts())
    if client is None:
        result.duration_s = time.monotonic() - t0
        return result

    by_key = {d.key: d for d in snapshot.devices}

    def send(device_keys: set[str], stale: list[Change]) -> None:
        batches = [device_entities(by_key[k], snapshot.source) for k in sorted(device_keys)]
        if settings.stale_action == "flag" and stale:
            batches.append([stale_entity(*_site_name(c.key), settings.stale_status, settings.stale_tag) for c in stale])
        r = ingest(client, batches)
        result.ingest.requests += r.requests
        result.ingest.entities += r.entities
        result.ingest.errors += r.errors

    # Send the full snapshot: Diode itself decides create/update/no-change, which
    # is what makes re-runs idempotent. Stale devices get a partial update.
    send(set(by_key), [c for c in _outstanding(plan, before, settings) if c.action == Action.STALE])
    result.applied = True

    deadline = t0 + settings.converge_timeout_s
    retry_at = t0 + settings.converge_timeout_s / 2
    after = read_state()
    while True:
        post = compute_plan(desired, after, snapshot.sites)
        outstanding = _outstanding(post, after, settings)
        if not outstanding:
            result.converged = True
            break
        now = time.monotonic()
        if now >= deadline:
            log.error("[%s] not converged; %d items outstanding", name, len(outstanding))
            break
        if now >= retry_at and result.retries == 0:
            # Idempotent re-send of only the objects still drifting.
            result.retries = 1
            log.warning("[%s] re-sending %d outstanding devices", name, len(post.pending_device_keys()))
            send(post.pending_device_keys() & set(by_key), [c for c in outstanding if c.action == Action.STALE])
        sleep(settings.poll_interval_s)
        after = read_state()

    result.checks = verify(plan, compute_plan(desired, after, snapshot.sites), after, settings)
    if count_changes and writes_before is not None:
        result.netbox_writes = count_changes() - writes_before
    result.duration_s = time.monotonic() - t0
    log.info("[%s] converged=%s failures=%d", name, result.converged, len(result.failures))
    return result


def verify(plan: Plan, post: Plan, after: InventoryState, settings: Settings) -> list[Check]:
    """Check every planned change landed in NetBox, and nothing else drifted."""
    residual = {(c.object_type, c.key, c.field): c for c in post.pending}
    residual_objects = {(c.object_type, c.key) for c in post.pending}
    checks: list[Check] = []
    for c in plan.changes:
        if c.action == Action.UNCHANGED:
            continue
        if c.action == Action.STALE:
            if c.object_type == ObjectType.DEVICE and settings.stale_action == "flag":
                dev = after.devices.get(c.key)
                ok = dev is not None and is_flagged(dev, settings.stale_status, settings.stale_tag)
                actual = f"{dev.status} tags={sorted(dev.tags)}" if dev else "missing"
                expected = f"{settings.stale_status} +{settings.stale_tag}"
                checks.append(
                    Check(c.object_type, c.key, c.action, "status", expected, actual, "PASS" if ok else "FAIL")
                )
            else:
                checks.append(Check(c.object_type, c.key, c.action, c.field, "review", c.before, "REPORTED"))
            continue
        left = residual.get((c.object_type, c.key, c.field))
        if c.action == Action.CREATE and c.object_type != ObjectType.IP_ADDRESS:
            ok = (c.object_type, c.key) not in residual_objects
            actual = "present" if ok else "missing"
        else:
            ok = left is None
            actual = c.after if ok else left.before  # type: ignore[union-attr]
        checks.append(Check(c.object_type, c.key, c.action, c.field, c.after, actual, "PASS" if ok else "FAIL"))

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
            "0 objects drifting",
            f"{drifting} objects drifting",
            "PASS" if drifting == 0 else "FAIL",
        )
    )
    return checks
