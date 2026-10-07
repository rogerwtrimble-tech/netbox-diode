from conftest import DATA, device, snapshot
from fake_diode import FakeDiode
from inventory_recon.config import Settings
from inventory_recon.models import InventorySnapshot
from inventory_recon.reconcile import run_phase

BASE = InventorySnapshot.load(DATA / "cmdb_baseline.json")
DISC = InventorySnapshot.load(DATA / "discovery_day1.json")


def run(settings: Settings, fake: FakeDiode, snap: InventorySnapshot, apply: bool = True):  # type: ignore[no-untyped-def]
    return run_phase(
        "p",
        "test",
        snap,
        settings,
        read_state=fake.read_state,
        client=fake if apply else None,
        count_changes=fake.change_count,
        sleep=lambda _: None,
    )


def test_full_scenario_converges_and_rerun_is_idempotent(settings: Settings) -> None:
    fake = FakeDiode()
    baseline = run(settings, fake, BASE)
    assert baseline.passed and baseline.converged
    assert baseline.plan.counts()["device"]["create"] == 50
    assert baseline.ingest.requests == 50

    rerun = run(settings, fake, BASE)
    assert rerun.passed
    assert rerun.plan.pending == []
    assert rerun.netbox_writes == 0  # idempotency proven by zero writes

    drift = run(settings, fake, DISC)
    assert drift.passed, drift.failures
    flagged = [c for c in drift.checks if c.action == "stale" and c.object_type == "device"]
    assert len(flagged) == 3 and all(c.result == "PASS" for c in flagged)
    assert fake.state.devices["DC-ATL1/atl1-leaf-07"].status == "offline"


def test_lost_ingest_is_recovered_by_one_retry(settings: Settings) -> None:
    fake = FakeDiode(drop_calls=50)  # the whole first send is lost
    result = run(settings, fake, BASE)
    assert result.retries == 1
    assert result.passed and result.converged


def test_never_converging_fails_with_details(settings: Settings) -> None:
    fake = FakeDiode(drop_calls=10_000)
    result = run(settings, fake, snapshot(device("a")))
    assert not result.converged and not result.passed
    assert {c.key for c in result.failures} >= {"S1/a"}


def test_diode_errors_fail_the_phase(settings: Settings) -> None:
    result = run(settings, FakeDiode(errors=["boom"]), snapshot(device("a")))
    assert result.converged and not result.passed
    assert "boom" in result.ingest.errors


def test_report_only_stale_mode_does_not_write(settings: Settings) -> None:
    fake = FakeDiode()
    run(settings, fake, snapshot(device("a"), device("b")))
    report_only = settings.model_copy(update={"stale_action": "report"})
    result = run(report_only, fake, snapshot(device("a")))
    assert result.passed
    assert fake.state.devices["S1/b"].status == "active"
    assert [c.result for c in result.checks if c.action == "stale"] == ["REPORTED"]


def test_dry_run_never_ingests(settings: Settings) -> None:
    fake = FakeDiode()
    result = run(settings, fake, BASE, apply=False)
    assert fake.calls == 0 and not result.applied and result.passed
