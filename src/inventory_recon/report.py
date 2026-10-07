"""Summary (Markdown), detail (CSV) and machine-readable (JSON) reports."""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from .diff import Action, Change, ObjectType
from .reconcile import PhaseResult

HIGHLIGHT_LIMIT = 12


def _verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def _phase_row(p: PhaseResult) -> str:
    counts = p.plan.counts()

    def total(a: Action) -> int:
        return sum(counts[t][a] for t in counts)

    passed = sum(c.result == "PASS" for c in p.checks)
    applied = "-" if p.kind == "retire" else f"{p.ingest.entities}" if p.applied else "dry run"
    writes = "-" if p.netbox_writes is None else str(p.netbox_writes)
    review = "yes" if p.reviewed else "no"
    return (
        f"| {p.name} | `{p.source}` | {review} | {total(Action.CREATE)} | {total(Action.UPDATE)} | "
        f"{total(Action.UNCHANGED)} | {total(Action.STALE)} | {total(Action.BLOCKED)} | {len(p.rejected)} | "
        f"{applied} | {writes} | {passed}/{passed + len(p.failures)} | {p.duration_s:.0f}s | **{_verdict(p.passed)}** |"
    )


def _breakdown(p: PhaseResult) -> list[str]:
    counts = p.plan.counts()
    if p.kind == "retire":
        return ["| Object | Retire |", "|---|--:|"] + [
            f"| {t} | {c['stale']} |" for t, c in counts.items() if c["stale"]
        ]
    lines = ["| Object | Create | Update | Unchanged | Stale | Blocked |", "|---|--:|--:|--:|--:|--:|"]
    lines += [
        f"| {t} | {c['create']} | {c['update']} | {c['unchanged']} | {c['stale']} | {c['blocked']} |"
        for t, c in counts.items()
        if any(c.values())
    ]
    return lines


def _short(key: str) -> str:
    """Drop the site from site/device[/interface] keys for readability (cables: both ends)."""
    if " <> " in key:
        return " <> ".join(_short(k) for k in key.split(" <> "))
    return key.split("/", 1)[1] if "/" in key and not key[0].isdigit() else key


_SINGULAR = {
    ObjectType.DEVICE: "Device",
    ObjectType.INTERFACE: "Interface",
    ObjectType.IP_ADDRESS: "IP address",
    ObjectType.VLAN: "VLAN",
    ObjectType.PREFIX: "Prefix",
    ObjectType.CABLE: "Cable",
}
_PLURAL = {
    ObjectType.DEVICE: "devices",
    ObjectType.INTERFACE: "interfaces",
    ObjectType.IP_ADDRESS: "IP addresses",
    ObjectType.VLAN: "VLANs",
    ObjectType.PREFIX: "prefixes",
    ObjectType.CABLE: "cables",
}


def _label(p: PhaseResult, c: Change) -> tuple[str, str] | None:
    """Category label + item text for one change, or None if not highlighted."""
    plural = _PLURAL[c.object_type]
    key = f"`{c.key}`" if c.object_type in {ObjectType.VLAN, ObjectType.PREFIX} else f"`{_short(c.key)}`"
    if p.kind == "retire":
        return f"Retired {plural}", key
    if c.action == Action.CREATE and c.object_type not in {ObjectType.INTERFACE, ObjectType.IP_ADDRESS}:
        return f"New {plural}", key
    if c.action == Action.UPDATE and c.object_type == ObjectType.IP_ADDRESS:
        return "IP addresses moved", f"{key} {_short(c.before)} -> {_short(c.after)}"
    if c.action == Action.UPDATE:
        return f"{_SINGULAR[c.object_type]} {c.field} changed", f"{key} {c.before} -> {c.after}"
    if c.action == Action.STALE:
        where = f" on {_short(c.before)}" if c.before and c.object_type == ObjectType.IP_ADDRESS else ""
        return f"Stale {plural}, absent from `{p.source}`", f"{key}{where}"
    if c.action == Action.BLOCKED:
        return f"Blocked {plural} (endpoint held by a stale object; retire it first)", key
    return None


def _highlights(p: PhaseResult) -> list[str]:
    """One compact line per drift category (directors read this, analysts read the CSV)."""
    groups: dict[str, list[str]] = {}
    for c in p.plan.changes:
        if c.group in p.rejected and p.kind != "retire":
            continue  # listed once under "Rejected by reviewer"
        if (c.object_type == ObjectType.IP_ADDRESS and c.action == Action.CREATE) or not (lc := _label(p, c)):
            continue
        groups.setdefault(lc[0], []).append(lc[1])
    if p.rejected:
        groups["Rejected by reviewer (not applied)"] = [f"`{g}`" for g in sorted(p.rejected)]
    out = []
    for label, raw in groups.items():
        items = list(dict.fromkeys(raw))
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
        "| Phase | Source | Reviewed | Create | Update | Unchanged | Stale | Blocked | Rejected | Entities sent "
        "| NetBox writes | Checks passed | Time | Result |",
        "|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|",
        *[_phase_row(p) for p in phases],
        "",
        "Counts are planned objects (devices, interfaces, IPs, VLANs, prefixes, cables), including those in",
        "groups a reviewer rejected; *Rejected* counts review groups, which are not applied.",
        "*Entities sent*: the whole source minus rejected/blocked groups; Diode decides what to write.",
        "*NetBox writes*: NetBox changelog entries during the phase (independent proof of idempotency).",
        "*Checks*: NetBox is re-read after apply; every approved item is verified, plus a residual-drift sweep.",
        "Stale objects are flagged, never deleted, until a human approves a retirement plan (phase *retire*).",
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
