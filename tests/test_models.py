from ipaddress import IPv4Interface

import pytest
from pydantic import ValidationError

from conftest import DATA, device, snapshot
from inventory_recon.models import InterfaceRecord, InventorySnapshot, InventoryState


@pytest.mark.parametrize("name", ["cmdb_baseline.json", "discovery_day1.json"])
def test_shipped_snapshots_are_valid(name: str) -> None:
    snap = InventorySnapshot.load(DATA / name)
    assert len(snap.devices) == 50
    assert snap.sites == {"DC-DAL1", "DC-ATL1", "BR-NYC1"}


def test_duplicate_device_serial_and_ip_rejected() -> None:
    ip = InterfaceRecord(name="mgmt", type="1000base-t", ip=IPv4Interface("10.0.0.1/24"))
    a = device("a", serial="X", interfaces=(ip,))
    b = device("a", serial="X", interfaces=(ip,))
    with pytest.raises(ValidationError) as exc:
        snapshot(a, b)
    msg = str(exc.value)
    assert "duplicate device: S1/a" in msg
    assert "duplicate serial: X" in msg
    assert "duplicate ip: 10.0.0.1/24" in msg


def test_duplicate_interface_rejected() -> None:
    i = InterfaceRecord(name="eth0", type="1000base-t")
    with pytest.raises(ValidationError, match="duplicate interfaces"):
        device("a", interfaces=(i, i))


@pytest.mark.parametrize("bad", [{"name": "a/b"}, {"status": "broken"}, {"serial": ""}, {"color": "red"}])
def test_invalid_device_fields_rejected(bad: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        device(**{"name": "a", **bad})


def test_interface_names_may_contain_slash() -> None:
    assert InterfaceRecord(name="et-0/0/1", type="100gbase-x-qsfp28").name == "et-0/0/1"


def test_state_from_snapshot_keys() -> None:
    ip = InterfaceRecord(name="Ethernet1/1", type="1000base-t", ip=IPv4Interface("10.0.0.1/24"))
    st = InventoryState.from_snapshot(snapshot(device("leaf1", interfaces=(ip,))))
    assert set(st.devices) == {"S1/leaf1"}
    assert set(st.interfaces) == {"S1/leaf1/Ethernet1/1"}
    assert st.ips["10.0.0.1/24"].interface_key == "S1/leaf1/Ethernet1/1"
