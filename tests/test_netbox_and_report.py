import csv
import json

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

DEVICE = {
    "id": 1,
    "name": "leaf1",
    "site": {"name": "S1"},
    "role": {"name": "leaf"},
    "device_type": {"model": "7050", "manufacturer": {"name": "Arista"}},
    "platform": None,
    "serial": "X",
    "status": {"value": "active"},
    "tags": [{"name": "src-cmdb"}],
}
IFACE = {"id": 7, "device": {"id": 1}, "name": "Ethernet1/1", "type": {"value": "1000base-t"}, "enabled": True}
IP = {"address": "10.0.0.1/24", "assigned_object_type": "dcim.interface", "assigned_object_id": 7}


def transport(flaky: dict[str, int]) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer nbt_x.y"
        path = req.url.path
        if flaky.get(path, 0) > 0:
            flaky[path] -= 1
            return httpx.Response(503)
        if path == "/api/dcim/devices/":
            if req.url.params.get("offset") == "1":
                return httpx.Response(200, json={"results": [{**DEVICE, "id": 2, "name": None}], "next": None})
            nxt = "http://netbox.test/api/dcim/devices/?limit=500&offset=1"
            return httpx.Response(200, json={"results": [DEVICE], "next": nxt})
        if path == "/api/dcim/interfaces/":
            return httpx.Response(200, json={"results": [IFACE], "next": None})
        if path == "/api/ipam/ip-addresses/":
            return httpx.Response(200, json={"results": [IP], "next": None})
        return httpx.Response(404, text="nope")

    return httpx.MockTransport(handler)


def test_reader_paginates_normalizes_and_retries(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("inventory_recon.netbox.time.sleep", lambda _: None)
    with NetBoxReader(settings, transport=transport({"/api/dcim/interfaces/": 2})) as nb:
        st = nb.fetch_state()
    assert list(st.devices) == ["S1/leaf1"]
    assert st.devices["S1/leaf1"].tags == {"src-cmdb"}
    assert st.ips["10.0.0.1/24"].interface_key == "S1/leaf1/Ethernet1/1"


def test_reader_raises_on_client_error(settings: Settings) -> None:
    with NetBoxReader(settings, transport=transport({})) as nb, pytest.raises(NetBoxError, match="404"):
        nb.status()


def test_reports_written(settings: Settings) -> None:
    fake = FakeDiode()
    disc = InventorySnapshot.load(DATA / "discovery_day1.json")
    base = InventorySnapshot.load(DATA / "cmdb_baseline.json")
    phases = [
        run_phase(n, n, s, settings, read_state=fake.read_state, client=fake, sleep=lambda _: None)
        for n, s in (("1-baseline", base), ("3-discovery", disc))
    ]
    summary = write_reports(phases, settings.reports_dir, {"NetBox": "test"})
    text = summary.read_text()
    assert "**Result: PASS** (2/2 phases passed)" in text
    assert "New devices (3)" in text and "Stale devices, absent from `discovery` (3)" in text
    assert "`10.30.0.18/24` on nyc1-wlc-01/GigabitEthernet0" in text
    rows = list(csv.DictReader((settings.reports_dir / "detail/3-discovery-verification.csv").open()))
    assert rows and all(r["result"] in {"PASS", "REPORTED"} for r in rows)
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
