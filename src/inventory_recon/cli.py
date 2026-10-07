"""Command line for inventory reconciliation.

  recon validate SNAPSHOT...                      schema/integrity check, no network
  recon plan SNAPSHOT [--out review.json]         dry run; optional review file to approve
  recon apply SNAPSHOT [--plan review.json]       apply via Diode, converge, verify
  recon retire plan SNAPSHOT [--out retire.json]  stale objects past the grace period
  recon retire apply retire.json --approve        approved deletions, re-validated first
  recon demo                                      the 5-phase scenario

Exit codes: 0 success, 1 reconciliation/verification failure, 2 bad input/config,
3 refused (plan out of date, blast-radius guard, or missing --approve).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import __version__
from .config import Settings
from .diode import Ingester, open_client
from .models import InventorySnapshot
from .netbox import NetBoxError, NetBoxReader
from .reconcile import PhaseResult, run_phase
from .report import write_reports
from .retire import NetBoxDeleter, RetirePlan, execute_retirement, plan_retirement
from .review import GuardError, ReviewPlan, StalePlanError, make_review, snapshot_digest

log = logging.getLogger("inventory_recon")
REJECT_IN_DEMO = "device:DC-ATL1/atl1-lab-sw01"  # the demo reviewer rejects the unauthorised switch


def _snapshot(path: Path) -> InventorySnapshot:
    try:
        return InventorySnapshot.load(path)
    except (OSError, ValidationError) as exc:
        raise SystemExit(f"invalid snapshot {path}:\n{exc}") from exc


class Session:
    """NetBox reader + (optional) Diode client + report metadata for one command."""

    def __init__(self, settings: Settings, stack: contextlib.ExitStack, apply: bool) -> None:
        self.settings = settings
        self.nb = stack.enter_context(NetBoxReader(settings))
        status = self.nb.status()
        self.meta = {
            "NetBox": str(status.get("netbox-version")),
            "Diode plugin": str(status.get("plugins", {}).get("netbox_diode_plugin", "?")),
            "recon": __version__,
        }
        self.client: Ingester | None = stack.enter_context(open_client(settings)) if apply else None

    def phase(
        self, name: str, desc: str, path: Path, review: ReviewPlan | None = None, apply: bool = True
    ) -> PhaseResult:
        """Run one reconciliation phase for the snapshot at ``path``."""
        return run_phase(
            name,
            desc,
            _snapshot(path),
            self.settings,
            read_state=self.nb.fetch_state,
            client=self.client if apply else None,
            count_changes=self.nb.change_count,
            review=review,
            snapshot_sha256=snapshot_digest(path),
        )

    def review_file(self, path: Path, out: Path, previous: ReviewPlan | None = None) -> ReviewPlan:
        """Dry-run plan written as an editable review file."""
        dry = self.phase("plan", "dry run", path, apply=False)
        review = make_review(dry.plan, dry.source, snapshot_digest(path), previous)
        review.save(out)
        log.info("review file written: %s (%d groups to review)", out, len(review.items))
        return review

    def retire_plan(self, path: Path, grace_days: int, out: Path) -> RetirePlan:
        """Retirement candidates for the snapshot's sites."""
        snap = _snapshot(path)
        plan = plan_retirement(snap, self.nb.fetch_state(snap.sites), grace_days, datetime.now(UTC).date())
        plan.save(out)
        log.info("retire plan written: %s (%d items, %d waiting)", out, len(plan.items), len(plan.waiting))
        return plan

    def retire(self, plan: RetirePlan, name: str = "retire") -> PhaseResult:
        """Execute an approved retirement plan."""
        with NetBoxDeleter(self.settings) as deleter:
            return execute_retirement(
                plan, self.nb.fetch_state, deleter.delete, lambda: self.nb.change_count(None), name=name
            )

    def report(self, results: list[PhaseResult]) -> int:
        """Write reports, print the summary, return the exit code."""
        summary = write_reports(results, self.settings.reports_dir, self.meta)
        print(summary.read_text())
        return 0 if all(r.passed for r in results) else 1


def _demo(s: Session) -> int:
    data, out = s.settings.data_dir, s.settings.reports_dir
    base, disc = data / "cmdb_baseline.json", data / "discovery_day1.json"
    results = [
        s.phase("1-baseline", "initial load of the CMDB export into an empty NetBox", base),
        s.phase("2-rerun", "same export again: proves idempotency (expect zero NetBox writes)", base),
    ]
    review = s.review_file(disc, out / "review-3-discovery.json")
    for item in review.items:
        if item.group == REJECT_IN_DEMO:
            item.decision, item.note = "reject", "unauthorised lab switch - rejected by demo reviewer"
    review.save(out / "review-3-discovery.json")
    results.append(
        s.phase("3-discovery", "discovery drift, reviewed: 1 group rejected, re-patch blocked", disc, review)
    )
    # Demo uses a 0-day grace period so retirement can be shown in one run (default: 30 days).
    retire_plan = s.retire_plan(disc, 0, out / "retire-plan.json")
    results.append(s.retire(retire_plan, "4-retire"))
    review5 = s.review_file(disc, out / "review-5-converge.json", previous=review)
    results.append(s.phase("5-converge", "re-plan with carried decisions: unblocked re-patch applied", disc, review5))
    return s.report(results)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recon", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("validate", help="validate snapshot files (no network)")
    p.add_argument("snapshots", nargs="+", type=Path)
    p = sub.add_parser("plan", help="dry run: compare a snapshot with NetBox")
    p.add_argument("snapshot", type=Path)
    p.add_argument("--out", type=Path, help="write an editable review file")
    p.add_argument("--decisions", type=Path, help="carry decisions from an earlier review file")
    p = sub.add_parser("apply", help="reconcile a snapshot into NetBox via Diode and verify")
    p.add_argument("snapshot", type=Path)
    p.add_argument("--plan", type=Path, help="reviewed plan file; only approved groups are applied")
    p.add_argument("--force", action="store_true", help="skip the blast-radius guard (unreviewed applies)")
    r = sub.add_parser("retire", help="retire (delete) stale objects with approval").add_subparsers(
        dest="retire_cmd", required=True
    )
    p = r.add_parser("plan", help="list retirement candidates")
    p.add_argument("snapshot", type=Path)
    p.add_argument("--older-than", type=int, help="grace period in days (default RECON_RETIRE_GRACE_DAYS)")
    p.add_argument("--out", type=Path, default=Path("retire-plan.json"))
    p = r.add_parser("apply", help="delete the approved items of a retirement plan")
    p.add_argument("plan", type=Path)
    p.add_argument("--approve", action="store_true", help="required: confirms a human approved this plan")
    sub.add_parser("demo", help="run the 5-phase demo scenario and write reports")
    return parser


def _dispatch(args: Any, s: Session) -> int:
    if args.cmd == "demo":
        return _demo(s)
    if args.cmd == "plan":
        if args.out:
            prior = ReviewPlan.load(args.decisions) if args.decisions else None
            s.review_file(args.snapshot, args.out, prior)
        return s.report([s.phase("plan", f"dry run of {args.snapshot.name}", args.snapshot, apply=False)])
    if args.cmd == "apply":
        review = ReviewPlan.load(args.plan) if args.plan else None
        return s.report([s.phase("apply", f"apply {args.snapshot.name}", args.snapshot, review)])
    if args.retire_cmd == "plan":
        grace = s.settings.retire_grace_days if args.older_than is None else args.older_than
        plan = s.retire_plan(args.snapshot, grace, args.out)
        print(
            f"{len(plan.items)} items to retire, {len(plan.waiting)} stale devices still in grace period -> {args.out}"
        )
        return 0
    if not args.approve:
        print("refusing to delete without --approve (review the plan file first)", file=sys.stderr)
        return 3
    return s.report([s.retire(RetirePlan.load(args.plan))])


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.DEBUG if args.verbose else logging.WARNING)
    if args.cmd == "validate":
        for path in args.snapshots:
            snap = _snapshot(path)
            print(
                f"OK {path}: source={snap.source} devices={len(snap.devices)} vlans={len(snap.vlans)} "
                f"prefixes={len(snap.prefixes)} cables={len(snap.cables)} sites={sorted(snap.sites)}"
            )
        return 0
    try:
        settings = Settings()  # type: ignore[call-arg]  # secrets come from the environment
    except ValidationError as exc:
        print(f"configuration error:\n{exc}", file=sys.stderr)
        return 2
    if getattr(args, "force", False):
        settings = settings.model_copy(update={"force": True})
    needs_diode: Callable[[Any], bool] = lambda a: a.cmd in ("apply", "demo")  # noqa: E731
    try:
        with contextlib.ExitStack() as stack:
            return _dispatch(args, Session(settings, stack, apply=needs_diode(args)))
    except (StalePlanError, GuardError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except NetBoxError as exc:
        print(f"NetBox error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
