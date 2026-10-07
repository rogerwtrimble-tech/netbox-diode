#!/usr/bin/env python3
"""Generate the deterministic mock inventory used by the demo.

data/cmdb_baseline.json   - the CMDB export (system of record, day 0)
data/discovery_day1.json  - what network discovery sees on day 1, with the
                            drift listed in DRIFT below injected on purpose.

Re-running produces byte-identical files (fixed seed), so reports are reproducible.
"""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
rng = random.Random(20261007)


def serial(prefix: str) -> str:
    return prefix + "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789") for _ in range(8))


# role, manufacturer, model, platform, serial prefix, mgmt if, data ifs (name, type)
PROFILES = {
    "core": (
        "core-router",
        "Juniper",
        "MX204",
        "junos-23.4",
        "JN",
        "fxp0",
        [("et-0/0/0", "100gbase-x-qsfp28"), ("et-0/0/1", "100gbase-x-qsfp28")],
    ),
    "spine": (
        "spine",
        "Arista",
        "DCS-7050CX3-32S",
        "eos-4.31",
        "JPE",
        "Management1",
        [("Ethernet1/1", "100gbase-x-qsfp28"), ("Ethernet2/1", "100gbase-x-qsfp28")],
    ),
    "leaf": (
        "leaf",
        "Arista",
        "DCS-7050SX3-48YC8",
        "eos-4.31",
        "JPE",
        "Management1",
        [("Ethernet1", "25gbase-x-sfp28"), ("Ethernet49/1", "100gbase-x-qsfp28")],
    ),
    "fw": (
        "firewall",
        "Palo Alto Networks",
        "PA-3220",
        "panos-11.1",
        "PA",
        "mgmt",
        [("ethernet1/1", "10gbase-x-sfpp"), ("ethernet1/2", "10gbase-x-sfpp")],
    ),
    "oob": ("console-server", "Opengear", "CM8148", "opengear-24.07", "OG", "eth0", [("eth1", "1000base-t")]),
    "rtr": (
        "edge-router",
        "Cisco",
        "C8300-1N1S-4T2X",
        "ios-xe-17.12",
        "FDO",
        "GigabitEthernet0",
        [("TenGigabitEthernet0/1/0", "10gbase-x-sfpp")],
    ),
    "bfw": (
        "firewall",
        "Fortinet",
        "FG-100F",
        "fortios-7.4",
        "FG1",
        "mgmt",
        [("port1", "1000base-t"), ("port2", "1000base-t")],
    ),
    "acc": (
        "access-switch",
        "Cisco",
        "C9300-48P",
        "ios-xe-17.12",
        "FOC",
        "GigabitEthernet0/0",
        [("TenGigabitEthernet1/1/1", "10gbase-x-sfpp")],
    ),
    "wlc": (
        "wireless-controller",
        "Cisco",
        "C9800-L-C-K9",
        "ios-xe-17.12",
        "FCW",
        "GigabitEthernet0",
        [("TenGigabitEthernet0/1/0", "10gbase-x-sfpp")],
    ),
}

# site, device prefix, mgmt subnet, [(profile, count)]
SITES = [
    ("DC-DAL1", "dal1", "10.10.0", [("core", 2), ("spine", 2), ("leaf", 12), ("fw", 2), ("oob", 2)]),
    ("DC-ATL1", "atl1", "10.20.0", [("core", 2), ("spine", 2), ("leaf", 12), ("fw", 2), ("oob", 2)]),
    ("BR-NYC1", "nyc1", "10.30.0", [("rtr", 1), ("bfw", 1), ("acc", 6), ("wlc", 2)]),
]


def make_device(site: str, name: str, profile: str, mgmt_ip: str, loopback: str | None) -> dict:
    role, mfr, model, platform, sprefix, mgmt, data_ifs = PROFILES[profile]
    ifaces = [{"name": mgmt, "type": "1000base-t", "ip": f"{mgmt_ip}/24"}]
    ifaces += [{"name": n, "type": t} for n, t in data_ifs]
    if loopback:
        ifaces.append({"name": "lo0", "type": "virtual", "ip": f"{loopback}/32"})
    return {
        "name": name,
        "site": site,
        "role": role,
        "manufacturer": mfr,
        "model": model,
        "platform": platform,
        "serial": serial(sprefix),
        "status": "active",
        "interfaces": ifaces,
    }


def baseline() -> list[dict]:
    devices = []
    for site_idx, (site, prefix, subnet, plan) in enumerate(SITES, start=1):
        host = 10
        for profile, count in plan:
            for n in range(1, count + 1):
                lo = f"10.255.{site_idx}.{n}" if profile == "core" else None
                devices.append(make_device(site, f"{prefix}-{profile}-{n:02d}", profile, f"{subnet}.{host}", lo))
                host += 1
    # Two leafs are racked but not yet in service.
    for d in devices:
        if d["name"] in ("atl1-leaf-11", "atl1-leaf-12"):
            d["status"] = "planned"
    return devices


# The day-1 drift, by category. This is the ground truth the demo must detect.
DRIFT = {
    "new_devices": [
        ("DC-DAL1", "dal1-leaf-13", "leaf", "10.10.0.40"),
        ("BR-NYC1", "nyc1-acc-07", "acc", "10.30.0.40"),
        ("DC-ATL1", "atl1-lab-sw01", "acc", "10.20.0.40"),
    ],
    "serial_rma": ["dal1-leaf-04", "nyc1-acc-03"],
    "os_upgrade": {"dal1-spine-01": "eos-4.33", "dal1-spine-02": "eos-4.33", "atl1-spine-01": "eos-4.33"},
    "status_live": ["atl1-leaf-11", "atl1-leaf-12"],
    "mgmt_readdress": {"nyc1-wlc-01": "10.30.1.10", "atl1-fw-02": "10.20.1.10"},
    "new_interface": {"dal1-core-01": ("et-0/0/2", "100gbase-x-qsfp28")},
    "missing": ["dal1-oob-02", "atl1-leaf-07", "nyc1-acc-06"],
}


def discovery(base: list[dict]) -> list[dict]:
    devices = {d["name"]: d for d in copy.deepcopy(base)}
    for name in DRIFT["missing"]:
        del devices[name]
    for name in DRIFT["serial_rma"]:
        devices[name]["serial"] = serial(devices[name]["serial"][:3])
    for name, platform in DRIFT["os_upgrade"].items():
        devices[name]["platform"] = platform
    for name in DRIFT["status_live"]:
        devices[name]["status"] = "active"
    for name, ip in DRIFT["mgmt_readdress"].items():
        devices[name]["interfaces"][0]["ip"] = f"{ip}/24"
    for name, (ifname, iftype) in DRIFT["new_interface"].items():
        devices[name]["interfaces"].append({"name": ifname, "type": iftype})
    for site, name, profile, ip in DRIFT["new_devices"]:
        devices[name] = make_device(site, name, profile, ip, None)
    return sorted(devices.values(), key=lambda d: (d["site"], d["name"]))


def write(name: str, source: str, ts: str, devices: list[dict]) -> None:
    doc = {"source": source, "collected_at": ts, "devices": devices}
    (DATA / name).write_text(json.dumps(doc, indent=1) + "\n")
    print(f"wrote data/{name}: {len(devices)} devices")


if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    base = baseline()
    write("cmdb_baseline.json", "cmdb", "2026-10-01T06:00:00Z", base)
    write("discovery_day1.json", "discovery", "2026-10-02T06:00:00Z", discovery(base))
