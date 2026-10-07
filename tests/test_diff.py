from ipaddress import IPv4Interface

from conftest import DATA, device, snapshot
from inventory_recon.diff import Action, ObjectType, compute_plan, device_key_of, is_flagged
from inventory_recon.models import InterfaceRecord, InventorySnapshot, InventoryState


def plan_for(desired_snap, actual_snap):  # type: ignore[no-untyped-def]
    return compute_plan(
        InventoryState.from_snapshot(desired_snap), InventoryState.from_snapshot(actual_snap), desired_snap.sites
    )


def test_create_update_unchanged_stale() -> None:
    have = snapshot(device("a"), device("b"), device("gone"))
    want = snapshot(device("a"), device("b", serial="NEW", platform="eos-4.33"), device("new"))
    plan = plan_for(want, have)

    assert [c.key for c in plan.of(Action.CREATE, ObjectType.DEVICE)] == ["S1/new"]
    updates = {(c.key, c.field, c.after) for c in plan.of(Action.UPDATE)}
    assert updates == {("S1/b", "serial", "NEW"), ("S1/b", "platform", "eos-4.33")}
    assert [c.key for c in plan.of(Action.STALE, ObjectType.DEVICE)] == ["S1/gone"]
    assert plan.counts()["device"] == {"create": 1, "update": 1, "unchanged": 1, "stale": 1}


def test_identical_state_has_no_pending_changes() -> None:
    snap = InventorySnapshot.load(DATA / "cmdb_baseline.json")
    plan = plan_for(snap, snap)
    assert plan.pending == []
    assert not plan.of(Action.STALE)


def test_stale_is_scoped_to_snapshot_sites() -> None:
    have = snapshot(device("a"), device("other", site="S2"))
    plan = plan_for(snapshot(device("a")), have)
    assert plan.of(Action.STALE) == []


def test_ip_reassignment_and_stale_ip() -> None:
    old = InterfaceRecord(name="mgmt", type="1000base-t", ip=IPv4Interface("10.0.0.1/24"))
    new = InterfaceRecord(name="mgmt", type="1000base-t", ip=IPv4Interface("10.0.1.1/24"))
    moved = InterfaceRecord(name="mgmt", type="1000base-t", ip=IPv4Interface("10.0.0.9/24"))
    have = snapshot(device("a", interfaces=(old,)), device("b", interfaces=(moved,)))
    want = snapshot(
        device("a", interfaces=(new,)),
        device("b", interfaces=(InterfaceRecord(name="mgmt", type="1000base-t"),)),
        device("c", interfaces=(moved,)),
    )
    plan = plan_for(want, have)
    assert {c.key for c in plan.of(Action.CREATE, ObjectType.IP_ADDRESS)} == {"10.0.1.1/24"}
    (upd,) = plan.of(Action.UPDATE, ObjectType.IP_ADDRESS)
    assert (upd.key, upd.before, upd.after) == ("10.0.0.9/24", "S1/b/mgmt", "S1/c/mgmt")
    assert {c.key for c in plan.of(Action.STALE, ObjectType.IP_ADDRESS)} == {"10.0.0.1/24"}


def test_interface_create_update_stale() -> None:
    e1 = InterfaceRecord(name="Ethernet1/1", type="100gbase-x-qsfp28")
    have = snapshot(device("a", interfaces=(e1, InterfaceRecord(name="old", type="1000base-t"))))
    want = snapshot(
        device(
            "a",
            interfaces=(
                InterfaceRecord(name="Ethernet1/1", type="100gbase-x-qsfp28", enabled=False),
                InterfaceRecord(name="new", type="1000base-t"),
            ),
        )
    )
    plan = plan_for(want, have)
    assert [(c.key, c.field, c.before, c.after) for c in plan.of(Action.UPDATE)] == [
        ("S1/a/Ethernet1/1", "enabled", "true", "false")
    ]
    assert [c.key for c in plan.of(Action.CREATE)] == ["S1/a/new"]
    assert [c.key for c in plan.of(Action.STALE)] == ["S1/a/old"]
    assert plan.pending_device_keys() == {"S1/a"}


def test_device_key_of_handles_slashes_in_interface_names() -> None:
    assert device_key_of("DC-DAL1/dal1-core-01/et-0/0/2") == "DC-DAL1/dal1-core-01"


def test_is_flagged() -> None:
    st = InventoryState.from_snapshot(snapshot(device("a", status="offline"))).devices["S1/a"]
    assert not is_flagged(st, "offline", "recon-stale")
    assert is_flagged(st.model_copy(update={"tags": frozenset({"recon-stale"})}), "offline", "recon-stale")


def test_demo_scenario_ground_truth() -> None:
    """The shipped discovery snapshot must produce exactly the documented drift."""
    base = InventorySnapshot.load(DATA / "cmdb_baseline.json")
    disc = InventorySnapshot.load(DATA / "discovery_day1.json")
    plan = plan_for(disc, base)
    assert plan.counts() == {
        "device": {"create": 3, "update": 7, "unchanged": 40, "stale": 3},
        "interface": {"create": 8, "update": 0, "unchanged": 134, "stale": 0},
        "ip_address": {"create": 5, "update": 0, "unchanged": 49, "stale": 2},
    }
    fields = sorted(c.field for c in plan.of(Action.UPDATE))
    assert fields == ["platform"] * 3 + ["serial"] * 2 + ["status"] * 2
