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
        [(f"Ethernet{n}/1", "100gbase-x-qsfp28") for n in range(1, 17)],
    ),
    "leaf": (
        "leaf",
        "Arista",
        "DCS-7050SX3-48YC8",
        "eos-4.31",
        "JPE",
        "Management1",
        [
            ("Ethernet1", "25gbase-x-sfp28"),
            ("Ethernet49/1", "100gbase-x-qsfp28"),
            ("Ethernet50/1", "100gbase-x-qsfp28"),
        ],
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
        [(f"port{n}", "1000base-t") for n in range(1, 9)],
    ),
    "acc": (
        "access-switch",
        "Cisco",
        "C9300-48P",
        "ios-xe-17.12",
        "FOC",
        "GigabitEthernet0/0",
        [("TenGigabitEthernet1/1/1", "10gbase-x-sfpp"), ("GigabitEthernet1/0/48", "1000base-t")],
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


def cable(site: str, a: str, a_if: str, b: str, b_if: str) -> dict:
    return {"a": {"site": site, "device": a, "interface": a_if}, "b": {"site": site, "device": b, "interface": b_if}}


def dc_cables(site: str, p: str) -> list[dict]:
    out = []
    for n in range(1, 13):  # every leaf dual-homed to both spines
        out.append(cable(site, f"{p}-leaf-{n:02d}", "Ethernet49/1", f"{p}-spine-01", f"Ethernet{n}/1"))
        out.append(cable(site, f"{p}-leaf-{n:02d}", "Ethernet50/1", f"{p}-spine-02", f"Ethernet{n}/1"))
    for n, other in ((1, 2), (2, 1)):  # cores cross-connected to both spines
        out.append(cable(site, f"{p}-core-0{n}", "et-0/0/0", f"{p}-spine-0{n}", "Ethernet15/1"))
        out.append(cable(site, f"{p}-core-0{n}", "et-0/0/1", f"{p}-spine-0{other}", "Ethernet16/1"))
    for n in (1, 2):  # firewalls hang off the border leafs
        out.append(cable(site, f"{p}-fw-0{n}", "ethernet1/1", f"{p}-leaf-0{n}", "Ethernet1"))
    return out


def branch_cables(site: str, p: str) -> list[dict]:
    out = [cable(site, f"{p}-rtr-01", "TenGigabitEthernet0/1/0", f"{p}-bfw-01", "port1")]
    out += [cable(site, f"{p}-acc-0{n}", "TenGigabitEthernet1/1/1", f"{p}-bfw-01", f"port{n + 1}") for n in range(1, 7)]
    out += [
        cable(site, f"{p}-wlc-0{n}", "TenGigabitEthernet0/1/0", f"{p}-acc-0{n}", "GigabitEthernet1/0/48")
        for n in (1, 2)
    ]
    return out


def vlan(site: str, vid: int, name: str, status: str = "active") -> dict:
    return {"site": site, "vid": vid, "name": name, "status": status}


def prefix(pfx: str, site: str, vid: int | None, status: str = "active") -> dict:
    return {"prefix": pfx, "site": site, "vlan": vid, "status": status}


def ipam() -> tuple[list[dict], list[dict]]:
    vlans, prefixes = [], []
    for idx, (site, _, subnet, _) in enumerate(SITES, start=1):
        o = subnet.split(".")[1]  # 10.<o>.0
        if site.startswith("DC-"):
            vlans += [vlan(site, 10, "mgmt"), vlan(site, 100, "servers"), vlan(site, 200, "storage")]
            prefixes += [
                prefix(f"10.{o}.0.0/24", site, 10),
                prefix(f"10.{int(o) + 1}.0.0/22", site, 100),
                prefix(f"10.{int(o) + 2}.0.0/24", site, 200, "reserved" if site == "DC-DAL1" else "active"),
            ]
        else:
            vlans += [vlan(site, 10, "mgmt"), vlan(site, 20, "users"), vlan(site, 30, "voice"), vlan(site, 40, "guest")]
            prefixes += [prefix(f"10.{o}.0.0/24", site, 10)]
            prefixes += [prefix(f"10.{int(o) + 1}.{v}.0/24", site, v) for v in (20, 30, 40)]
        prefixes.append(prefix(f"10.255.{idx}.0/24", site, None, "container"))
    vlans.append(vlan("DC-DAL1", 999, "legacy-voice"))
    prefixes.append(prefix("10.19.99.0/24", "DC-DAL1", 999))
    return vlans, prefixes


def baseline() -> dict:
    devices = []
    for site_idx, (site, p, subnet, plan) in enumerate(SITES, start=1):
        host = 10
        for profile, count in plan:
            for n in range(1, count + 1):
                lo = f"10.255.{site_idx}.{n}" if profile == "core" else None
                devices.append(make_device(site, f"{p}-{profile}-{n:02d}", profile, f"{subnet}.{host}", lo))
                host += 1
    # Two leafs are racked but not yet in service.
    for d in devices:
        if d["name"] in ("atl1-leaf-11", "atl1-leaf-12"):
            d["status"] = "planned"
    cables = dc_cables("DC-DAL1", "dal1") + dc_cables("DC-ATL1", "atl1") + branch_cables("BR-NYC1", "nyc1")
    vlans, prefixes = ipam()
    return {"devices": devices, "vlans": vlans, "prefixes": prefixes, "cables": cables}


# The day-1 drift, by category. This is the ground truth the demo must detect.
DRIFT = {
    "new_devices": [
        ("DC-DAL1", "dal1-leaf-13", "leaf", "10.10.0.40"),
        ("BR-NYC1", "nyc1-acc-07", "acc", "10.30.0.40"),
        ("DC-ATL1", "atl1-lab-sw01", "acc", "10.20.0.40"),  # unauthorised: the demo reviewer rejects it
    ],
    "serial_rma": ["dal1-leaf-04", "nyc1-acc-03"],
    "os_upgrade": {"dal1-spine-01": "eos-4.33", "dal1-spine-02": "eos-4.33", "atl1-spine-01": "eos-4.33"},
    "status_live": ["atl1-leaf-11", "atl1-leaf-12"],
    "mgmt_readdress": {"nyc1-wlc-01": "10.30.1.10", "atl1-fw-02": "10.20.1.10"},
    "new_interface": {"dal1-core-01": ("et-0/0/2", "100gbase-x-qsfp28")},
    "missing": ["dal1-oob-02", "atl1-leaf-07", "nyc1-acc-06"],
    "new_cables": [
        ("DC-DAL1", "dal1-leaf-13", "Ethernet49/1", "dal1-spine-01", "Ethernet13/1"),
        ("DC-DAL1", "dal1-leaf-13", "Ethernet50/1", "dal1-spine-02", "Ethernet13/1"),
        ("BR-NYC1", "nyc1-acc-07", "TenGigabitEthernet1/1/1", "nyc1-bfw-01", "port8"),
        ("DC-ATL1", "atl1-lab-sw01", "TenGigabitEthernet1/1/1", "atl1-leaf-12", "Ethernet1"),
    ],
    # Re-patched: leaf-02 uplink moved from spine-01 Ethernet2/1 to Ethernet14/1. Blocked until the
    # old cable is retired, because NetBox allows one cable per interface.
    "repatch": ("DC-DAL1", "dal1-leaf-02", "Ethernet49/1", "dal1-spine-01", "Ethernet2/1", "Ethernet14/1"),
    "new_vlans": [("BR-NYC1", 50, "iot")],
    "renamed_vlans": {("DC-ATL1", 200): "storage-nvme"},
    "missing_vlans": [("DC-DAL1", 999)],
    "new_prefixes": [
        ("10.31.50.0/24", "BR-NYC1", 50),
        ("10.30.1.0/24", "BR-NYC1", 10),
        ("10.20.1.0/24", "DC-ATL1", 10),
    ],
    "prefix_status": {"10.12.0.0/24": "active"},
    "missing_prefixes": ["10.19.99.0/24"],
}


def discovery(base: dict) -> dict:
    devices = {d["name"]: d for d in copy.deepcopy(base["devices"])}
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

    # Discovery only sees cables whose both ends it can see.
    cables = [c for c in copy.deepcopy(base["cables"]) if c["a"]["device"] in devices and c["b"]["device"] in devices]
    _site, dev, ifn, _peer, old_port, new_port = DRIFT["repatch"]
    for c in cables:
        if (c["a"]["device"], c["a"]["interface"], c["b"]["interface"]) == (dev, ifn, old_port):
            c["b"]["interface"] = new_port
    cables += [cable(*c) for c in DRIFT["new_cables"]]

    vlans = [v for v in copy.deepcopy(base["vlans"]) if (v["site"], v["vid"]) not in DRIFT["missing_vlans"]]
    for v in vlans:
        v["name"] = DRIFT["renamed_vlans"].get((v["site"], v["vid"]), v["name"])
    vlans += [vlan(*v) for v in DRIFT["new_vlans"]]
    prefixes = [p for p in copy.deepcopy(base["prefixes"]) if p["prefix"] not in DRIFT["missing_prefixes"]]
    for p in prefixes:
        p["status"] = DRIFT["prefix_status"].get(p["prefix"], p["status"])
    prefixes += [prefix(*p) for p in DRIFT["new_prefixes"]]
    return {
        "devices": sorted(devices.values(), key=lambda d: (d["site"], d["name"])),
        "vlans": vlans,
        "prefixes": prefixes,
        "cables": cables,
    }


def write(name: str, source: str, ts: str, inv: dict) -> None:
    doc = {"source": source, "collected_at": ts, **inv}
    (DATA / name).write_text(json.dumps(doc, indent=1) + "\n")
    print(f"wrote data/{name}: " + ", ".join(f"{len(v)} {k}" for k, v in inv.items()))


if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    base = baseline()
    write("cmdb_baseline.json", "cmdb", "2026-10-01T06:00:00Z", base)
    write("discovery_day1.json", "discovery", "2026-10-02T06:00:00Z", discovery(base))
