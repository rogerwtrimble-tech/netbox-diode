"""Review-before-apply: a plan file a human approves or rejects per group.

OSS Diode has no review UI for change sets, so the review happens before data is
sent: ``recon plan --out review.json`` -> human edits decisions -> ``recon apply
--plan review.json``. Apply recomputes the plan and refuses if anything a reviewer
saw has changed in the meantime.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .diff import Action, Change, ObjectType, Plan, device_key_of

Decision = Literal["approve", "reject"]
REVIEWABLE = (Action.CREATE, Action.UPDATE, Action.STALE, Action.BLOCKED)


class ReviewItem(BaseModel):
    """One reviewable group (a device with its interfaces/IPs, or one VLAN/prefix/cable)."""

    model_config = ConfigDict(extra="forbid")

    group: str
    changes: list[str] = Field(description="Human-readable change lines")
    fingerprint: str = Field(description="sha256 of the group's changes; apply refuses if it differs")
    decision: Decision = "approve"
    note: str = ""


class ReviewPlan(BaseModel):
    """The approval artifact for one snapshot against one NetBox state."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    source: str
    snapshot_sha256: str
    created_at: datetime
    items: list[ReviewItem]

    def rejected(self) -> set[str]:
        """Groups a reviewer rejected."""
        return {i.group for i in self.items if i.decision == "reject"}

    def save(self, path: Path) -> None:
        """Write as pretty JSON (meant to be edited by a human)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> ReviewPlan:
        """Load a reviewed plan file."""
        return cls.model_validate_json(path.read_text())


class StalePlanError(RuntimeError):
    """The reviewed plan no longer matches reality; re-plan and review again."""


class GuardError(RuntimeError):
    """An unreviewed apply would change more existing inventory than allowed."""


def _line(c: Change) -> str:
    if c.action == Action.UPDATE:
        return f"{c.action} {c.object_type} {c.key}: {c.field} {c.before!r} -> {c.after!r}"
    if c.action == Action.BLOCKED:
        return f"{c.action} {c.object_type} {c.key}: endpoint held by {c.before}"
    return f"{c.action} {c.object_type} {c.key}"


def _grouped(plan: Plan) -> dict[str, list[Change]]:
    groups: dict[str, list[Change]] = defaultdict(list)
    for c in plan.changes:
        if c.action in REVIEWABLE:
            groups[c.group].append(c)
    return groups


def _fingerprint(changes: list[Change]) -> str:
    payload = json.dumps(sorted(_line(c) for c in changes))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def snapshot_digest(path: Path) -> str:
    """sha256 of the snapshot file as reviewed."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_review(plan: Plan, source: str, snapshot_sha256: str, previous: ReviewPlan | None = None) -> ReviewPlan:
    """Build a review file; decisions from ``previous`` carry over for identical groups."""
    carried = {(i.group, i.fingerprint): i for i in previous.items} if previous else {}
    items = []
    for group, changes in sorted(_grouped(plan).items()):
        fp = _fingerprint(changes)
        prior = carried.get((group, fp))
        items.append(
            ReviewItem(
                group=group,
                changes=sorted(_line(c) for c in changes),
                fingerprint=fp,
                decision=prior.decision if prior else "approve",
                note=prior.note if prior else "",
            )
        )
    return ReviewPlan(source=source, snapshot_sha256=snapshot_sha256, created_at=datetime.now(UTC), items=items)


def check_review(review: ReviewPlan, plan: Plan, source: str, snapshot_sha256: str) -> None:
    """Refuse to apply unless the reviewed plan is exactly what would be applied now."""
    if review.source != source or review.snapshot_sha256 != snapshot_sha256:
        raise StalePlanError("snapshot differs from the one that was reviewed")
    now = {g: _fingerprint(c) for g, c in _grouped(plan).items()}
    seen = {i.group: i.fingerprint for i in review.items}
    changed = sorted(g for g in now.keys() | seen.keys() if now.get(g) != seen.get(g))
    if changed:
        shown = ", ".join(changed[:10]) + (f" (+{len(changed) - 10} more)" if len(changed) > 10 else "")
        raise StalePlanError(f"NetBox changed since review for {len(changed)} group(s): {shown}")


def cascade_rejections(rejected: set[str], plan: Plan) -> set[str]:
    """Rejecting a device also rejects cables that touch it (a cable would create its ends)."""
    devices = {g.split(":", 1)[1] for g in rejected if g.startswith("device:")}
    extra = {
        c.group
        for c in plan.changes
        if c.object_type == ObjectType.CABLE and any(device_key_of(end) in devices for end in c.key.split(" <> "))
    }
    return rejected | extra


def guard(plan: Plan, existing_objects: int, max_fraction: float) -> None:
    """Blast-radius guard for unreviewed applies: updates + stale vs existing inventory."""
    risky = len({(c.object_type, c.key) for c in plan.changes if c.action in (Action.UPDATE, Action.STALE)})
    limit = max(10, int(existing_objects * max_fraction))
    if risky > limit:
        raise GuardError(
            f"{risky} existing objects would be updated or flagged stale (limit {limit}, "
            f"{max_fraction:.0%} of {existing_objects}); review with `recon plan --out` and apply --plan, "
            "or pass --force"
        )
