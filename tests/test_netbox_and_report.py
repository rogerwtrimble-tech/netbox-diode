import csv
import json
from datetime import date

import httpx
import pytest

from conftest import DATA
from fake_diode import FakeDiode
from inventory_recon import cli
from inventory_recon.config import Settings
from inventory_recon.models import InventorySnapshot
from inventory_recon.netbox import NetBoxError, NetBoxReader
from inventory_recon.reconcile import run_phase
from inventory_recon.report import write_reports
from inventory_recon.retire import execute_retirement, plan_retirement

DEVICE = {
    "id": 1,
    "name": "leaf1",
    "site": {"name": "S1"},
    "role": {"name": "leaf"},
    "device_type": {"model": "7050", "manufacturer": {"name": "Arista"}},
    "platform": None,
    "serial": "X",
    "status": {"value": "offline"},
    "custom_fields": {"recon_state": "stale", "recon_stale_since": "2026-10-02"},
}
IFACE = {"id": 7, "device": {"id": 1}, "name": "Ethernet1/1", "type": {"value": "1000base-t"}, "enabled": True}
IFACE2 = {**IFACE, "id": 8, "name": "Ethernet2/1"}
IP = {"id": 3, "address": "10.0.0.1/24", "assigned_object_type": "dcim.interface", "assigned_object_id": 7}
VLAN = {"id": 4, "vid": 10, "name": "mgmt", "status": {"value": "active"}, "site": {"name": "S1"}}
PREFIX = {
    "id": 5,
    "prefix": "10.0.0.0/24",
    "status": {"value": "active"},
    "scope_type": "dcim.site",
    "scope": {"name": "S1"},
    "vlan": {"id": 4},
}
CABLE = {
    "id": 6,
    "status": {"value": "connected"},
    "a_terminations": [{"object_type": "dcim.interface", "object_id": 8, "object": {}}],
    "b_terminations": [
        {"object_type": "dcim.interface", "object_id": 99, "object": {"name": "e9", "device": {"name": "far"}}}
    ],
}
SEEN: list[httpx.Request] = []


def transport(flaky: dict[str, int]) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer nbt_x.y"
        SEEN.append(req)
        path = req.url.path
        if flaky.get(path, 0) > 0:
            flaky[path] -= 1
            return httpx.Response(503)
        if path == "/api/dcim/devices/":
            if req.url.params.get("offset") == "1":
                return httpx.Response(200, json={"results": [{**DEVICE, "id": 2, "name": None}], "next": None})
            nxt = "http://netbox.test/api/dcim/devices/?limit=500&offset=1"
            return httpx.Response(200, json={"results": [DEVICE], "next": nxt})
        rows = {
            "/api/dcim/sites/": [{"id": 1}],
            "/api/extras/custom-fields/": [{"name": "recon_state"}, {"name": "recon_stale_since"}],
            "/api/dcim/interfaces/": [IFACE, IFACE2],
            "/api/ipam/ip-addresses/": [IP],
            "/api/ipam/vlans/": [VLAN],
            "/api/ipam/prefixes/": [PREFIX],
            "/api/dcim/cables/": [CABLE],
        }
        if path in rows:
            return httpx.Response(200, json={"results": rows[path], "next": None})
        return httpx.Response(404, text="nope")

    return httpx.MockTransport(handler)


def test_reader_paginates_normalizes_and_retries(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("inventory_recon.netbox.time.sleep", lambda _: None)
    SEEN.clear()
    with NetBoxReader(settings, transport=transport({"/api/dcim/interfaces/": 2})) as nb:
        st = nb.fetch_state(frozenset({"S1"}))
    assert list(st.devices) == ["S1/leaf1"]
    assert st.devices["S1/leaf1"].recon_state == "stale"
    assert str(st.devices["S1/leaf1"].stale_since) == "2026-10-02"
    assert st.ips["10.0.0.1/24"].interface_key == "S1/leaf1/Ethernet1/1"
    assert st.vlans["S1/10"].name == "mgmt" and st.prefixes["10.0.0.0/24"].vlan == "S1/10"
    assert list(st.cables) == ["?/far/e9 <> S1/leaf1/Ethernet2/1"]
    assert st.ids["cable:?/far/e9 <> S1/leaf1/Ethernet2/1"] == 6
    # Scoped, slim reads: every list call filters by site (IPs by device) and asks only for needed fields.
    lists = [
        r
        for r in SEEN
        if r.url.path not in {"/api/dcim/sites/", "/api/extras/custom-fields/"} and "offset" not in r.url.params
    ]
    assert st.custom_fields == {"recon_state", "recon_stale_since"}
    assert all("fields" in r.url.params for r in lists)
    assert all(r.url.params.get("site_id") == "1" for r in lists if "ip-addresses" not in r.url.path)
    assert next(r for r in lists if "ip-addresses" in r.url.path).url.params.get_list("device_id") == ["1"]


def test_reader_raises_on_client_error(settings: Settings) -> None:
    with NetBoxReader(settings, transport=transport({})) as nb, pytest.raises(NetBoxError, match="404"):
        nb.status()


def test_reader_empty_scope_reads_nothing(settings: Settings) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path in {"/api/dcim/sites/", "/api/extras/custom-fields/"}
        return httpx.Response(200, json={"results": [], "next": None})

    with NetBoxReader(settings, transport=httpx.MockTransport(handler)) as nb:
        assert nb.fetch_state(frozenset({"nowhere"})).devices == {}


def test_reports_written(settings: Settings) -> None:
    fake = FakeDiode()
    disc = InventorySnapshot.load(DATA / "discovery_day1.json")
    base = InventorySnapshot.load(DATA / "cmdb_baseline.json")
    phases = [
        run_phase(n, n, s, settings, read_state=fake.read_state, client=fake, sleep=lambda _: None)
        for n, s in (("1-baseline", base), ("3-discovery", disc))
    ]
    retire_plan = plan_retirement(disc, fake.read_state(), 0, date(2026, 10, 3))
    phases.append(execute_retirement(retire_plan, fake.read_state, fake.delete, name="4-retire"))
    summary = write_reports(phases, settings.reports_dir, {"NetBox": "test"})
    text = summary.read_text()
    assert "**Result: PASS** (3/3 phases passed)" in text
    assert "New devices (3)" in text and "Stale devices, absent from `discovery` (3)" in text
    assert "`10.30.0.18/24` on nyc1-wlc-01/GigabitEthernet0" in text
    assert "Blocked cables" in text and "VLAN name changed (1):** `DC-ATL1/200` storage -> storage-nvme" in text
    assert "Retired devices (3)" in text
    rows = list(csv.DictReader((settings.reports_dir / "detail/3-discovery-verification.csv").open()))
    assert rows and all(r["result"] in {"PASS", "REPORTED", "BLOCKED"} for r in rows)
    run_json = json.loads((settings.reports_dir / "run.json").read_text())
    assert run_json["result"] == "PASS" and run_json["phases"][1]["plan"]["device"]["stale"] == 3


def test_cli_validate_and_bad_input(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    assert cli.main(["validate", str(DATA / "cmdb_baseline.json")]) == 0
    assert "devices=50" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text('{"source": "x", "collected_at": "2026-01-01T00:00:00Z", "devices": []}')
    with pytest.raises(SystemExit, match="invalid snapshot"):
        cli.main(["validate", str(bad)])


def test_cli_reports_missing_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECON_NETBOX_TOKEN", raising=False)
    assert cli.main(["plan", str(DATA / "cmdb_baseline.json")]) == 2


def test_cli_retire_apply_requires_approve(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("RECON_NETBOX_TOKEN", "x")
    monkeypatch.setenv("RECON_DIODE_CLIENT_SECRET", "y")
    monkeypatch.setattr(cli, "Session", lambda *a, **k: object())
    assert cli.main(["retire", "apply", str(tmp_path / "plan.json")]) == 3
