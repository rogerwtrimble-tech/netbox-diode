#!/usr/bin/env python3
"""Scale benchmark: load a large synthetic site, then time scoped vs full NetBox reads.

Runs inside the recon container (`make bench`), using the same settings as the demo.
Writes reports/scale.md. Load once, measure many: re-running skips the ingest if the
bench site is already converged.

    python scripts/bench_scale.py [--devices 1000]
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime
from ipaddress import IPv4Address
from pathlib import Path

from inventory_recon.config import Settings
from inventory_recon.diff import compute_plan
from inventory_recon.diode import open_client
from inventory_recon.models import InventorySnapshot, InventoryState
from inventory_recon.netbox import NetBoxReader
from inventory_recon.reconcile import run_phase

DEMO_SITES = frozenset({"DC-DAL1", "DC-ATL1", "BR-NYC1"})


def bench_snapshot(n: int) -> InventorySnapshot:
    """n leaf switches in one site, 4 interfaces each, mgmt IPs, pairwise uplink cables."""
    devices, cables = [], []
    base = int(IPv4Address("10.200.0.0"))
    for i in range(n):
        name = f"bench-leaf-{i:05d}"
        devices.append(
            {
                "name": name,
                "site": "BENCH-1",
                "role": "leaf",
                "manufacturer": "Arista",
                "model": "DCS-7050SX3-48YC8",
                "platform": "eos-4.33",
                "serial": f"BENCH{i:07d}",
                "interfaces": [
                    {"name": "Management1", "type": "1000base-t", "ip": f"{IPv4Address(base + i + 1)}/16"},
                    {"name": "Ethernet1", "type": "25gbase-x-sfp28"},
                    {"name": "Ethernet49/1", "type": "100gbase-x-qsfp28"},
                    {"name": "Ethernet50/1", "type": "100gbase-x-qsfp28"},
                ],
            }
        )
        if i % 2:
            cables.append(
                {
                    "a": {"site": "BENCH-1", "device": f"bench-leaf-{i - 1:05d}", "interface": "Ethernet50/1"},
                    "b": {"site": "BENCH-1", "device": name, "interface": "Ethernet50/1"},
                }
            )
    return InventorySnapshot.model_validate(
        {"source": "bench", "collected_at": datetime.now(UTC), "devices": devices, "cables": cables}
    )


def timed(fn, repeat: int = 3) -> tuple[float, object]:  # type: ignore[no-untyped-def]
    best, result = float("inf"), None
    for _ in range(repeat):
        t = time.monotonic()
        result = fn()
        best = min(best, time.monotonic() - t)
    return best, result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--devices", type=int, default=1000)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings().model_copy(update={"force": True, "converge_timeout_s": 3600})  # type: ignore[call-arg]
    snap = bench_snapshot(args.devices)
    objects = len(snap.devices) * 6 + len(snap.cables)  # device + 4 interfaces + 1 IP each, + cables

    with NetBoxReader(settings) as nb:
        desired = InventoryState.from_snapshot(snap)
        pending = compute_plan(desired, nb.fetch_state(snap.sites), snap.sites).pending
        ingest_s = 0.0
        if pending:
            with open_client(settings) as client:
                t = time.monotonic()
                r = run_phase("bench-load", "load", snap, settings, read_state=nb.fetch_state, client=client)
                ingest_s = time.monotonic() - t
            print(f"loaded: passed={r.passed} converged={r.converged} in {ingest_s:.0f}s")

        demo_scoped_s, demo_state = timed(lambda: nb.fetch_state(DEMO_SITES))
        bench_scoped_s, bench_state = timed(lambda: nb.fetch_state(snap.sites))
        full_s, full_state = timed(lambda: nb.fetch_state(None))
        plan_s, plan = timed(lambda: compute_plan(desired, bench_state, snap.sites))

    def n(st: InventoryState) -> int:
        return sum(len(getattr(st, a)) for a in ("devices", "interfaces", "ips", "vlans", "prefixes", "cables"))

    rows = [
        (
            "Load bench site through Diode (create + verify)",
            f"{ingest_s:.0f}s" if ingest_s else "already loaded",
            objects,
        ),
        ("Read demo sites only (scoped)", f"{demo_scoped_s:.1f}s", n(demo_state)),  # type: ignore[arg-type]
        ("Read bench site only (scoped)", f"{bench_scoped_s:.1f}s", n(bench_state)),  # type: ignore[arg-type]
        ("Read everything (unscoped)", f"{full_s:.1f}s", n(full_state)),  # type: ignore[arg-type]
        ("Plan (diff) bench site", f"{plan_s:.2f}s", len(plan.changes)),  # type: ignore[attr-defined]
    ]
    md = [
        "# Scale benchmark",
        "",
        f"`scripts/bench_scale.py --devices {args.devices}` on {datetime.now(UTC):%Y-%m-%d} "
        "(single-host Docker, NetBox 4.7.2, Diode 2.3.1; best of 3 for reads).",
        "",
        "| Operation | Time | Objects |",
        "|---|--:|--:|",
        *[f"| {a} | {b} | {c} |" for a, b, c in rows],
        "",
        "Scoped reads cost scales with the sites being reconciled, not with the size of NetBox.",
    ]
    out = Path(settings.reports_dir) / "scale.md"
    out.write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
