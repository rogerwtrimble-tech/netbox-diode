# Inventory Reconciliation Report

**Result: PASS** (5/5 phases passed) · NetBox: 4.7.2 · Diode plugin: 1.18.0 · recon: 0.2.0

| Phase | Source | Reviewed | Create | Update | Unchanged | Stale | Blocked | Rejected | Entities sent | NetBox writes | Checks passed | Time | Result |
|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| 1-baseline | `cmdb` | no | 431 | 0 | 0 | 0 | 0 | 0 | 433 | 813 | 432/432 | 46s | **PASS** |
| 2-rerun | `cmdb` | no | 0 | 0 | 431 | 0 | 0 | 0 | 431 | 0 | 1/1 | 2s | **PASS** |
| 3-discovery | `discovery` | yes | 27 | 9 | 399 | 11 | 1 | 2 | 432 | 225 | 34/34 | 46s | **PASS** |
| 4-retire | `discovery` | yes | 0 | 0 | 0 | 14 | 0 | 0 | - | 39 | 14/14 | 4s | **PASS** |
| 5-converge | `discovery` | yes | 7 | 0 | 429 | 0 | 0 | 2 | 430 | 6 | 2/2 | 15s | **PASS** |

Counts are planned objects (devices, interfaces, IPs, VLANs, prefixes, cables), including those in
groups a reviewer rejected; *Rejected* counts review groups, which are not applied.
*Entities sent*: the whole source minus rejected/blocked groups; Diode decides what to write.
*NetBox writes*: NetBox changelog entries during the phase (independent proof of idempotency).
*Checks*: NetBox is re-read after apply; every approved item is verified, plus a residual-drift sweep.
Stale objects are flagged, never deleted, until a human approves a retirement plan (phase *retire*).

## 1-baseline: initial load of the CMDB export into an empty NetBox

| Object | Create | Update | Unchanged | Stale | Blocked |
|---|--:|--:|--:|--:|--:|
| device | 50 | 0 | 0 | 0 | 0 |
| interface | 233 | 0 | 0 | 0 | 0 |
| ip_address | 54 | 0 | 0 | 0 | 0 |
| vlan | 11 | 0 | 0 | 0 | 0 |
| prefix | 14 | 0 | 0 | 0 | 0 |
| cable | 69 | 0 | 0 | 0 | 0 |

- **New devices (50):** `nyc1-acc-01`, `nyc1-acc-02`, `nyc1-acc-03`, `nyc1-acc-04`, `nyc1-acc-05`, `nyc1-acc-06`, `nyc1-bfw-01`, `nyc1-rtr-01`, `nyc1-wlc-01`, `nyc1-wlc-02`, `atl1-core-01`, `atl1-core-02`, +38 more
- **New VLANs (11):** `BR-NYC1/10`, `BR-NYC1/20`, `BR-NYC1/30`, `BR-NYC1/40`, `DC-ATL1/10`, `DC-ATL1/100`, `DC-ATL1/200`, `DC-DAL1/10`, `DC-DAL1/100`, `DC-DAL1/200`, `DC-DAL1/999`
- **New prefixes (14):** `10.10.0.0/24`, `10.11.0.0/22`, `10.12.0.0/24`, `10.19.99.0/24`, `10.20.0.0/24`, `10.21.0.0/22`, `10.22.0.0/24`, `10.255.1.0/24`, `10.255.2.0/24`, `10.255.3.0/24`, `10.30.0.0/24`, `10.31.20.0/24`, +2 more
- **New cables (69):** `nyc1-acc-01/GigabitEthernet1/0/48 <> nyc1-wlc-01/TenGigabitEthernet0/1/0`, `nyc1-acc-01/TenGigabitEthernet1/1/1 <> nyc1-bfw-01/port2`, `nyc1-acc-02/GigabitEthernet1/0/48 <> nyc1-wlc-02/TenGigabitEthernet0/1/0`, `nyc1-acc-02/TenGigabitEthernet1/1/1 <> nyc1-bfw-01/port3`, `nyc1-acc-03/TenGigabitEthernet1/1/1 <> nyc1-bfw-01/port4`, `nyc1-acc-04/TenGigabitEthernet1/1/1 <> nyc1-bfw-01/port5`, `nyc1-acc-05/TenGigabitEthernet1/1/1 <> nyc1-bfw-01/port6`, `nyc1-acc-06/TenGigabitEthernet1/1/1 <> nyc1-bfw-01/port7`, `nyc1-bfw-01/port1 <> nyc1-rtr-01/TenGigabitEthernet0/1/0`, `atl1-core-01/et-0/0/0 <> atl1-spine-01/Ethernet15/1`, `atl1-core-01/et-0/0/1 <> atl1-spine-02/Ethernet16/1`, `atl1-core-02/et-0/0/0 <> atl1-spine-02/Ethernet15/1`, +57 more

Detail: [`detail/1-baseline-plan.csv`](detail/1-baseline-plan.csv), [`detail/1-baseline-verification.csv`](detail/1-baseline-verification.csv)

## 2-rerun: same export again: proves idempotency (expect zero NetBox writes)

| Object | Create | Update | Unchanged | Stale | Blocked |
|---|--:|--:|--:|--:|--:|
| device | 0 | 0 | 50 | 0 | 0 |
| interface | 0 | 0 | 233 | 0 | 0 |
| ip_address | 0 | 0 | 54 | 0 | 0 |
| vlan | 0 | 0 | 11 | 0 | 0 |
| prefix | 0 | 0 | 14 | 0 | 0 |
| cable | 0 | 0 | 69 | 0 | 0 |

Detail: [`detail/2-rerun-plan.csv`](detail/2-rerun-plan.csv), [`detail/2-rerun-verification.csv`](detail/2-rerun-verification.csv)

## 3-discovery: discovery drift, reviewed: 1 group rejected, re-patch blocked

| Object | Create | Update | Unchanged | Stale | Blocked |
|---|--:|--:|--:|--:|--:|
| device | 3 | 7 | 40 | 3 | 0 |
| interface | 11 | 0 | 224 | 0 | 0 |
| ip_address | 5 | 0 | 49 | 2 | 0 |
| vlan | 1 | 1 | 9 | 1 | 0 |
| prefix | 3 | 1 | 12 | 1 | 0 |
| cable | 4 | 0 | 65 | 4 | 1 |

- **Device serial changed (2):** `nyc1-acc-03` FOCQTKERWFY -> FOC1DUFURQD, `dal1-leaf-04` JPE5TY4D2YY -> JPEFJPFWD10
- **New devices (2):** `nyc1-acc-07`, `dal1-leaf-13`
- **Device status changed (2):** `atl1-leaf-11` planned -> active, `atl1-leaf-12` planned -> active
- **Device platform changed (3):** `atl1-spine-01` eos-4.31 -> eos-4.33, `dal1-spine-01` eos-4.31 -> eos-4.33, `dal1-spine-02` eos-4.31 -> eos-4.33
- **Stale devices, absent from `discovery` (3):** `nyc1-acc-06`, `atl1-leaf-07`, `dal1-oob-02`
- **Stale IP addresses, absent from `discovery` (2):** `10.20.0.27/24` on atl1-fw-02/mgmt, `10.30.0.18/24` on nyc1-wlc-01/GigabitEthernet0
- **New VLANs (1):** `BR-NYC1/50`
- **VLAN name changed (1):** `DC-ATL1/200` storage -> storage-nvme
- **Stale VLANs, absent from `discovery` (1):** `DC-DAL1/999`
- **Prefix status changed (1):** `10.12.0.0/24` reserved -> active
- **New prefixes (3):** `10.20.1.0/24`, `10.30.1.0/24`, `10.31.50.0/24`
- **Stale prefixes, absent from `discovery` (1):** `10.19.99.0/24`
- **New cables (3):** `nyc1-acc-07/TenGigabitEthernet1/1/1 <> nyc1-bfw-01/port8`, `dal1-leaf-13/Ethernet49/1 <> dal1-spine-01/Ethernet13/1`, `dal1-leaf-13/Ethernet50/1 <> dal1-spine-02/Ethernet13/1`
- **Blocked cables (endpoint held by a stale object; retire it first) (1):** `dal1-leaf-02/Ethernet49/1 <> dal1-spine-01/Ethernet14/1`
- **Stale cables, absent from `discovery` (4):** `nyc1-acc-06/TenGigabitEthernet1/1/1 <> nyc1-bfw-01/port7`, `atl1-leaf-07/Ethernet49/1 <> atl1-spine-01/Ethernet7/1`, `atl1-leaf-07/Ethernet50/1 <> atl1-spine-02/Ethernet7/1`, `dal1-leaf-02/Ethernet49/1 <> dal1-spine-01/Ethernet2/1`
- **Rejected by reviewer (not applied) (2):** `cable:DC-ATL1/atl1-lab-sw01/TenGigabitEthernet1/1/1 <> DC-ATL1/atl1-leaf-12/Ethernet1`, `device:DC-ATL1/atl1-lab-sw01`

Detail: [`detail/3-discovery-plan.csv`](detail/3-discovery-plan.csv), [`detail/3-discovery-verification.csv`](detail/3-discovery-verification.csv)

## 4-retire: approved retirement of 14 stale objects (grace 0d)

| Object | Retire |
|---|--:|
| device | 3 |
| ip_address | 5 |
| vlan | 1 |
| prefix | 1 |
| cable | 4 |

- **Retired cables (4):** `nyc1-acc-06/TenGigabitEthernet1/1/1 <> nyc1-bfw-01/port7`, `atl1-leaf-07/Ethernet49/1 <> atl1-spine-01/Ethernet7/1`, `atl1-leaf-07/Ethernet50/1 <> atl1-spine-02/Ethernet7/1`, `dal1-leaf-02/Ethernet49/1 <> dal1-spine-01/Ethernet2/1`
- **Retired IP addresses (5):** `10.10.0.29/24`, `10.20.0.20/24`, `10.20.0.27/24`, `10.30.0.17/24`, `10.30.0.18/24`
- **Retired prefixes (1):** `10.19.99.0/24`
- **Retired VLANs (1):** `DC-DAL1/999`
- **Retired devices (3):** `nyc1-acc-06`, `atl1-leaf-07`, `dal1-oob-02`

Detail: [`detail/4-retire-plan.csv`](detail/4-retire-plan.csv), [`detail/4-retire-verification.csv`](detail/4-retire-verification.csv)

## 5-converge: re-plan with carried decisions: unblocked re-patch applied

| Object | Create | Update | Unchanged | Stale | Blocked |
|---|--:|--:|--:|--:|--:|
| device | 1 | 0 | 49 | 0 | 0 |
| interface | 3 | 0 | 232 | 0 | 0 |
| ip_address | 1 | 0 | 53 | 0 | 0 |
| vlan | 0 | 0 | 11 | 0 | 0 |
| prefix | 0 | 0 | 16 | 0 | 0 |
| cable | 2 | 0 | 68 | 0 | 0 |

- **New cables (1):** `dal1-leaf-02/Ethernet49/1 <> dal1-spine-01/Ethernet14/1`
- **Rejected by reviewer (not applied) (2):** `cable:DC-ATL1/atl1-lab-sw01/TenGigabitEthernet1/1/1 <> DC-ATL1/atl1-leaf-12/Ethernet1`, `device:DC-ATL1/atl1-lab-sw01`

Detail: [`detail/5-converge-plan.csv`](detail/5-converge-plan.csv), [`detail/5-converge-verification.csv`](detail/5-converge-verification.csv)
