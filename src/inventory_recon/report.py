"""Summary (Markdown), detail (CSV) and machine-readable (JSON) reports."""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from .diff import Action, ObjectType
from .reconcile import PhaseResult

HIGHLIGHT_LIMIT = 12


def _verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def _phase_row(p: PhaseResult) -> str:
    counts = p.plan.counts()

    def total(a: Action) -> int:
        return sum(counts[t][a] for t in counts)

    passed = sum(c.result == "PASS" for c in p.checks)
    applied = f"{p.ingest.entities}" if p.applied else "dry run"
    writes = "-" if p.netbox_writes is None else str(p.netbox_writes)
    return (
        f"| {p.name} | `{p.source}` ({p.snapshot_devices} dev) | {total(Action.CREATE)} | "
        f"{total(Action.UPDATE)} | {total(Action.UNCHANGED)} | {total(Action.STALE)} | {applied} | {writes} | "
        f"{passed}/{passed + len(p.failures)} | {p.duration_s:.0f}s | **{_verdict(p.passed)}** |"
    )


def _breakdown(p: PhaseResult) -> list[str]:
    counts = p.plan.counts()
    lines = ["| Object | Create | Update | Unchanged | Stale |", "|---|--:|--:|--:|--:|"]
    lines += [f"| {t} | {c['create']} | {c['update']} | {c['unchanged']} | {c['stale']} |" for t, c in counts.items()]
    return lines


def _short(key: str) -> str:
    """Drop the site from site/device[/interface] keys for readability."""
    return key.split("/", 1)[1] if "/" in key else key


_LABEL = {ObjectType.DEVICE: "devices", ObjectType.IP_ADDRESS: "IP addresses", ObjectType.INTERFACE: "interfaces"}


def _highlights(p: PhaseResult) -> list[str]:
    """One compact line per drift category (directors read this, analysts read the CSV)."""
    groups: dict[str, list[str]] = {}
    for c in p.plan.changes:
        if c.action == Action.CREATE and c.object_type == ObjectType.DEVICE:
            label, item = "New devices", f"`{_short(c.key)}`"
        elif c.action == Action.UPDATE and c.object_type == ObjectType.IP_ADDRESS:
            label, item = "IP addresses moved", f"`{c.key}` {_short(c.before)} -> {_short(c.after)}"
        elif c.action == Action.UPDATE:
            label, item = f"Device {c.field} changed", f"`{_short(c.key)}` {c.before} -> {c.after}"
        elif c.action == Action.STALE and c.object_type == ObjectType.DEVICE:
            label, item = f"Stale devices, absent from `{p.source}`", f"`{_short(c.key)}`"
        elif c.action == Action.STALE:
            where = f" on {_short(c.before)}" if c.before else ""
            key = c.key if c.object_type == ObjectType.IP_ADDRESS else _short(c.key)
            label, item = f"Stale {_LABEL[c.object_type]}, absent from `{p.source}`", f"`{key}`{where}"
        else:
            continue
        groups.setdefault(label, []).append(item)
    out = []
    for label, items in groups.items():
        shown = ", ".join(items[:HIGHLIGHT_LIMIT])
        more = f", +{len(items) - HIGHLIGHT_LIMIT} more" if len(items) > HIGHLIGHT_LIMIT else ""
        out.append(f"- **{label} ({len(items)}):** {shown}{more}")
    return out


def write_reports(phases: Sequence[PhaseResult], out_dir: Path, meta: dict[str, str]) -> Path:
    """Write summary.md, detail/*.csv and run.json; return the summary path."""
    detail = out_dir / "detail"
    detail.mkdir(parents=True, exist_ok=True)
    ok = all(p.passed for p in phases)

    for p in phases:
        with (detail / f"{p.name}-plan.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["object_type", "key", "action", "field", "netbox_before", "source_value"])
            w.writerows(
                [c.object_type, c.key, c.action, c.field, c.before, c.after]
                for c in p.plan.changes
                if c.action != Action.UNCHANGED
            )
        with (detail / f"{p.name}-verification.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["object_type", "key", "action", "field", "expected", "actual", "result"])
            w.writerows(list(asdict(c).values()) for c in p.checks)

    md = [
        "# Inventory Reconciliation Report",
        "",
        f"**Result: {_verdict(ok)}** ({sum(p.passed for p in phases)}/{len(phases)} phases passed) · "
        + " · ".join(f"{k}: {v}" for k, v in meta.items()),
        "",
        "| Phase | Source | Create | Update | Unchanged | Stale | Entities sent | NetBox writes | Checks passed "
        "| Time | Result |",
        "|---|---|--:|--:|--:|--:|--:|--:|--:|--:|---|",
        *[_phase_row(p) for p in phases],
        "",
        "Create/Update/Unchanged/Stale count objects (devices + interfaces + IP addresses) in the plan.",
        "*Entities sent*: everything in the source goes to Diode, which decides what to write.",
        "*NetBox writes*: changelog entries made by Diode (independent proof of idempotency).",
        "*Checks*: NetBox is re-read after apply; every planned item is verified, plus a residual-drift sweep.",
        "Stale objects are never deleted: stale devices are flagged (status/tag) for human review.",
    ]
    for p in phases:
        md += ["", f"## {p.name}: {p.description}", "", *_breakdown(p)]
        if hl := _highlights(p):
            md += ["", *hl]
        if p.failures or p.ingest.errors:
            md += ["", f"**Failures ({len(p.failures)}), Diode errors ({len(p.ingest.errors)}):**"]
            md += [
                f"- {c.object_type} `{c.key}` {c.field}: expected `{c.expected}`, got `{c.actual}`"
                for c in p.failures[:HIGHLIGHT_LIMIT]
            ]
            md += [f"- diode: {e}" for e in p.ingest.errors[:HIGHLIGHT_LIMIT]]
        md += [
            "",
            f"Detail: [`detail/{p.name}-plan.csv`](detail/{p.name}-plan.csv), "
            f"[`detail/{p.name}-verification.csv`](detail/{p.name}-verification.csv)",
        ]
    summary = out_dir / "summary.md"
    summary.write_text("\n".join(md) + "\n")

    (out_dir / "run.json").write_text(
        json.dumps(
            {
                "result": _verdict(ok),
                **meta,
                "phases": [
                    {
                        "name": p.name,
                        "source": p.source,
                        "passed": p.passed,
                        "applied": p.applied,
                        "converged": p.converged,
                        "netbox_writes": p.netbox_writes,
                        "retries": p.retries,
                        "duration_s": round(p.duration_s, 1),
                        "plan": p.plan.counts(),
                        "ingest": asdict(p.ingest),
                        "checks": {"total": len(p.checks), "failed": len(p.failures)},
                    }
                    for p in phases
                ],
            },
            indent=2,
        )
        + "\n"
    )
    return summary
