# Inventory Reconciliation Report

**Result: PASS** (3/3 phases passed) · NetBox: 4.7.2 · Diode plugin: 1.18.0 · recon: 0.1.0

| Phase | Source | Create | Update | Unchanged | Stale | Entities sent | NetBox writes | Checks passed | Time | Result |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|---|
| 1-baseline | `cmdb` (50 dev) | 245 | 0 | 0 | 0 | 245 | 278 | 246/246 | 17s | **PASS** |
| 2-rerun | `cmdb` (50 dev) | 0 | 0 | 245 | 0 | 245 | 0 | 1/1 | 1s | **PASS** |
| 3-discovery | `discovery` (50 dev) | 16 | 7 | 223 | 5 | 249 | 118 | 27/27 | 24s | **PASS** |

Create/Update/Unchanged/Stale count objects (devices + interfaces + IP addresses) in the plan.
*Entities sent*: everything in the source goes to Diode, which decides what to write.
*NetBox writes*: changelog entries made by Diode (independent proof of idempotency).
*Checks*: NetBox is re-read after apply; every planned item is verified, plus a residual-drift sweep.
Stale objects are never deleted: stale devices are flagged (status/tag) for human review.

## 1-baseline: initial load of the CMDB export into an empty NetBox

| Object | Create | Update | Unchanged | Stale |
|---|--:|--:|--:|--:|
| device | 50 | 0 | 0 | 0 |
| interface | 141 | 0 | 0 | 0 |
| ip_address | 54 | 0 | 0 | 0 |

- **New devices (50):** `nyc1-acc-01`, `nyc1-acc-02`, `nyc1-acc-03`, `nyc1-acc-04`, `nyc1-acc-05`, `nyc1-acc-06`, `nyc1-bfw-01`, `nyc1-rtr-01`, `nyc1-wlc-01`, `nyc1-wlc-02`, `atl1-core-01`, `atl1-core-02`, +38 more

Detail: [`detail/1-baseline-plan.csv`](detail/1-baseline-plan.csv), [`detail/1-baseline-verification.csv`](detail/1-baseline-verification.csv)

## 2-rerun: same export again: proves idempotency (expect zero changes)

| Object | Create | Update | Unchanged | Stale |
|---|--:|--:|--:|--:|
| device | 0 | 0 | 50 | 0 |
| interface | 0 | 0 | 141 | 0 |
| ip_address | 0 | 0 | 54 | 0 |

Detail: [`detail/2-rerun-plan.csv`](detail/2-rerun-plan.csv), [`detail/2-rerun-verification.csv`](detail/2-rerun-verification.csv)

## 3-discovery: network discovery finds drift: new, changed and missing assets

| Object | Create | Update | Unchanged | Stale |
|---|--:|--:|--:|--:|
| device | 3 | 7 | 40 | 3 |
| interface | 8 | 0 | 134 | 0 |
| ip_address | 5 | 0 | 49 | 2 |

- **Device serial changed (2):** `nyc1-acc-03` FOCQTKERWFY -> FOC1DUFURQD, `dal1-leaf-04` JPE5TY4D2YY -> JPEFJPFWD10
- **New devices (3):** `nyc1-acc-07`, `atl1-lab-sw01`, `dal1-leaf-13`
- **Device status changed (2):** `atl1-leaf-11` planned -> active, `atl1-leaf-12` planned -> active
- **Device platform changed (3):** `atl1-spine-01` eos-4.31 -> eos-4.33, `dal1-spine-01` eos-4.31 -> eos-4.33, `dal1-spine-02` eos-4.31 -> eos-4.33
- **Stale devices, absent from `discovery` (3):** `nyc1-acc-06`, `atl1-leaf-07`, `dal1-oob-02`
- **Stale IP addresses, absent from `discovery` (2):** `10.20.0.27/24` on atl1-fw-02/mgmt, `10.30.0.18/24` on nyc1-wlc-01/GigabitEthernet0

Detail: [`detail/3-discovery-plan.csv`](detail/3-discovery-plan.csv), [`detail/3-discovery-verification.csv`](detail/3-discovery-verification.csv)
