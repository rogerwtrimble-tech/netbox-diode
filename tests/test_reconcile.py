from datetime import date

import pytest

from conftest import DATA, device, snapshot
from fake_diode import FakeDiode
from inventory_recon.config import Settings
from inventory_recon.diff import Action
from inventory_recon.models import InventorySnapshot, InventoryState
from inventory_recon.reconcile import run_phase
from inventory_recon.retire import execute_retirement, plan_retirement
from inventory_recon.review import GuardError, ReviewPlan, StalePlanError, make_review

BASE = InventorySnapshot.load(DATA / "cmdb_baseline.json")
DISC = InventorySnapshot.load(DATA / "discovery_day1.json")
LAB = "device:DC-ATL1/atl1-lab-sw01"


def run(
    settings: Settings, fake: FakeDiode, snap: InventorySnapshot, apply: bool = True, review: ReviewPlan | None = None
):  # type: ignore[no-untyped-def]
    return run_phase(
        "p",
        "test",
        snap,
        settings,
        read_state=fake.read_state,
        client=fake if apply else None,
        count_changes=fake.change_count,
        review=review,
        snapshot_sha256="sha",
        sleep=lambda _: None,
    )


def reviewed(
    settings: Settings, fake: FakeDiode, snap: InventorySnapshot, previous: ReviewPlan | None = None
) -> ReviewPlan:
    return make_review(run(settings, fake, snap, apply=False).plan, snap.source, "sha", previous)


def test_full_lifecycle(settings: Settings) -> None:
    """baseline -> idempotent rerun -> reviewed drift -> approved retirement -> converge."""
    fake = FakeDiode()
    baseline = run(settings, fake, BASE)
    assert baseline.passed and baseline.plan.counts()["cable"]["create"] == 69

    rerun = run(settings, fake, BASE)
    assert rerun.passed and rerun.plan.pending == [] and rerun.netbox_writes == 0

    review = reviewed(settings, fake, DISC)
    next(i for i in review.items if i.group == LAB).decision = "reject"
    drift = run(settings, fake, DISC, review=review)
    assert drift.passed, drift.failures
    assert "DC-ATL1/atl1-lab-sw01" not in fake.state.devices  # rejected, and its cable did not sneak it in
    assert any(g.startswith("cable:") for g in drift.rejected)
    assert [c.result for c in drift.checks if c.action == "blocked"] == ["BLOCKED"]
    assert fake.state.devices["DC-ATL1/atl1-leaf-07"].recon_state == "stale"
    assert fake.state.devices["DC-ATL1/atl1-leaf-07"].stale_since == date(2026, 10, 2)
    assert not fake.failed_applies  # blocked cable was never sent

    retire_plan = plan_retirement(DISC, fake.read_state(), grace_days=0, today=date(2026, 10, 3))
    kinds = sorted({i.object_type.value for i in retire_plan.items})
    assert kinds == ["cable", "device", "ip_address", "prefix", "vlan"]
    retired = execute_retirement(retire_plan, fake.read_state, fake.delete)
    assert retired.passed and len(retired.checks) == len(retire_plan.items)
    assert "DC-ATL1/atl1-leaf-07" not in fake.state.devices

    review5 = reviewed(settings, fake, DISC, previous=review)
    assert next(i for i in review5.items if i.group == LAB).decision == "reject"  # carried over
    converge = run(settings, fake, DISC, review=review5)
    assert converge.passed
    assert "DC-DAL1/dal1-leaf-02/Ethernet49/1 <> DC-DAL1/dal1-spine-01/Ethernet14/1" in fake.state.cables


def test_cables_wait_for_their_devices_even_if_diode_reorders(settings: Settings) -> None:
    """Regression: Diode does not order work across requests; cables must not race devices."""
    fake = FakeDiode(async_lifo=True)
    result = run(settings, fake, BASE)
    assert result.passed and result.retries == 0, fake.failed_applies
    assert not fake.failed_applies and len(fake.state.cables) == 69


def test_stale_review_is_refused(settings: Settings) -> None:
    fake = FakeDiode()
    run(settings, fake, BASE)
    review = reviewed(settings, fake, DISC)
    run(settings, fake, snapshot(device("intruder", site="DC-DAL1")).model_copy(update={"source": "x"}), review=None)
    with pytest.raises(StalePlanError, match="changed since review"):
        run(settings, fake, DISC, review=review)
    with pytest.raises(StalePlanError, match="snapshot differs"):
        run_phase(
            "p", "t", DISC, settings, read_state=fake.read_state, client=fake, review=review, snapshot_sha256="other"
        )


def test_guard_blocks_large_unreviewed_change(settings: Settings) -> None:
    fake = FakeDiode()
    run(settings, fake, BASE)
    shrunk = BASE.model_copy(update={"devices": BASE.devices[:5], "cables": (), "prefixes": (), "vlans": ()})
    strict = settings.model_copy(update={"guard_max_fraction": 0.05})
    with pytest.raises(GuardError, match="would be updated or flagged stale"):
        run(strict, fake, shrunk)
    assert run(settings, fake, BASE, apply=False).plan.pending == []  # refused apply wrote nothing
    forced = run(strict.model_copy(update={"force": True}), fake, shrunk)
    assert forced.passed


def test_retirement_respects_grace_and_revalidates(settings: Settings) -> None:
    fake = FakeDiode()
    run(settings, fake, BASE)
    run(settings, fake, DISC, review=reviewed(settings, fake, DISC))
    waiting = plan_retirement(DISC, fake.read_state(), grace_days=30, today=date(2026, 10, 3))
    assert len(waiting.waiting) == 3 and not any(i.object_type == "device" for i in waiting.items)

    plan = plan_retirement(DISC, fake.read_state(), grace_days=0, today=date(2026, 10, 3))
    target = next(i for i in plan.items if i.object_type == "vlan")
    target.decision = "reject"
    # Someone edits a stale device after the plan was made -> it must not be deleted.
    dev = next(i for i in plan.items if i.object_type == "device")
    edited = fake.state.devices[dev.key].model_copy(update={"serial": "CHANGED"})
    fake.state = fake.state.model_copy(update={"devices": {**fake.state.devices, dev.key: edited}})
    result = execute_retirement(plan, fake.read_state, fake.delete)
    by_key = {c.key: c for c in result.checks}
    assert by_key[target.key].result == "REJECTED" and "DC-DAL1/999" in fake.state.vlans
    assert by_key[dev.key].result == "FAIL" and "changed since plan" in by_key[dev.key].actual
    assert dev.key in fake.state.devices


def test_lost_ingest_is_recovered_by_one_retry(settings: Settings) -> None:
    never = run(settings, FakeDiode(drop_calls=10_000), snapshot(device("a")))
    assert never.applied and not never.converged and not never.passed

    lost_bootstrap = FakeDiode(drop_calls=1)  # the custom-field bootstrap request is lost once
    assert run(settings, lost_bootstrap, snapshot(device("a"))).passed

    ready = FakeDiode(state=InventoryState(custom_fields=frozenset({"recon_state", "recon_stale_since"})), drop_calls=1)
    result = run(settings, ready, snapshot(device("a")))  # the device request is lost once
    assert result.retries == 1 and result.passed


def test_diode_errors_fail_the_phase(settings: Settings) -> None:
    result = run(settings, FakeDiode(errors=["boom"]), snapshot(device("a")))
    assert result.converged and not result.passed and "boom" in result.ingest.errors


def test_report_only_stale_mode_does_not_write(settings: Settings) -> None:
    fake = FakeDiode()
    run(settings, fake, snapshot(device("a"), device("b")))
    result = run(settings.model_copy(update={"stale_action": "report"}), fake, snapshot(device("a")))
    assert result.passed and fake.state.devices["S1/b"].status == "active"
    assert [c.result for c in result.checks if c.action == Action.STALE] == ["REPORTED"]


def test_dry_run_never_ingests(settings: Settings) -> None:
    fake = FakeDiode()
    result = run(settings, fake, BASE, apply=False)
    assert fake.calls == 0 and not result.applied and result.passed
