from __future__ import annotations

from pathlib import Path

import pytest

from inventory_recon.config import Settings
from inventory_recon.models import DeviceRecord, InterfaceRecord, InventorySnapshot

DATA = Path(__file__).resolve().parent.parent / "data"


def device(name: str, site: str = "S1", **kw: object) -> DeviceRecord:
    fields: dict[str, object] = {
        "name": name,
        "site": site,
        "role": "leaf",
        "manufacturer": "Arista",
        "model": "7050",
        "platform": "eos-4.31",
        "serial": f"SN-{site}-{name}",
        "interfaces": (InterfaceRecord(name="Ethernet1/1", type="100gbase-x-qsfp28", ip=None),),
    }
    fields.update(kw)
    return DeviceRecord.model_validate(fields)


def snapshot(*devices: DeviceRecord, source: str = "test") -> InventorySnapshot:
    return InventorySnapshot(source=source, collected_at="2026-10-01T00:00:00Z", devices=devices)  # type: ignore[arg-type]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        netbox_url="http://netbox.test",  # type: ignore[arg-type]
        netbox_token="nbt_x.y",  # type: ignore[arg-type]
        diode_client_secret="s",  # type: ignore[arg-type]
        reports_dir=tmp_path / "reports",
        converge_timeout_s=0.3,
        poll_interval_s=1,
    )
