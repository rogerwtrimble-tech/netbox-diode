"""Command line: validate | plan | apply | demo.

Exit codes: 0 success, 1 reconciliation/verification failure, 2 bad input/config.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from . import __version__
from .config import Settings
from .diode import open_client
from .models import InventorySnapshot
from .netbox import NetBoxError, NetBoxReader
from .reconcile import PhaseResult, run_phase
from .report import write_reports

log = logging.getLogger("inventory_recon")

# The demo scenario: (phase name, snapshot file, description)
DEMO_PHASES = (
    ("1-baseline", "cmdb_baseline.json", "initial load of the CMDB export into an empty NetBox"),
    ("2-rerun", "cmdb_baseline.json", "same export again: proves idempotency (expect zero changes)"),
    ("3-discovery", "discovery_day1.json", "network discovery finds drift: new, changed and missing assets"),
)


def _snapshot(path: Path) -> InventorySnapshot:
    try:
        return InventorySnapshot.load(path)
    except (OSError, ValidationError) as exc:
        raise SystemExit(f"invalid snapshot {path}:\n{exc}") from exc


def _run(settings: Settings, phases: Sequence[tuple[str, Path, str]], apply: bool) -> int:
    snapshots = [(n, _snapshot(p), d) for n, p, d in phases]  # validate everything first
    results: list[PhaseResult] = []
    with NetBoxReader(settings) as nb:
        status = nb.status()
        meta = {
            "NetBox": str(status.get("netbox-version")),
            "Diode plugin": str(status.get("plugins", {}).get("netbox_diode_plugin", "?")),
            "recon": __version__,
        }
        with contextlib.ExitStack() as stack:
            client = stack.enter_context(open_client(settings)) if apply else None
            for name, snap, desc in snapshots:
                results.append(
                    run_phase(
                        name,
                        desc,
                        snap,
                        settings,
                        read_state=nb.fetch_state,
                        client=client,
                        count_changes=nb.change_count,
                    )
                )
    summary = write_reports(results, settings.reports_dir, meta)
    print(summary.read_text())
    return 0 if all(r.passed for r in results) else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(prog="recon", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_val = sub.add_parser("validate", help="validate snapshot files (no network)")
    p_val.add_argument("snapshots", nargs="+", type=Path)
    for cmd, helptext in (
        ("plan", "dry run: compare a snapshot with NetBox"),
        ("apply", "reconcile a snapshot into NetBox via Diode and verify"),
    ):
        p = sub.add_parser(cmd, help=helptext)
        p.add_argument("snapshot", type=Path)
    sub.add_parser("demo", help="run the 3-phase demo scenario and write reports")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.cmd == "validate":
        for path in args.snapshots:
            s = _snapshot(path)
            print(f"OK {path}: source={s.source} devices={len(s.devices)} sites={sorted(s.sites)}")
        return 0
    try:
        settings = Settings()  # type: ignore[call-arg]  # secrets come from the environment
    except ValidationError as exc:
        print(f"configuration error:\n{exc}", file=sys.stderr)
        return 2
    try:
        if args.cmd == "demo":
            phases = [(n, settings.data_dir / f, d) for n, f, d in DEMO_PHASES]
            return _run(settings, phases, apply=True)
        return _run(
            settings, [(args.cmd, args.snapshot, f"{args.cmd} {args.snapshot.name}")], apply=args.cmd == "apply"
        )
    except NetBoxError as exc:
        print(f"NetBox error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
