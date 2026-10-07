# Architecture and process

## 1. Problem and approach

Inventory data comes from several places that do not agree: the CMDB says what *should* exist,
network discovery says what *does* exist. NetBox is the source of truth, so it has to be reconciled
against both without corrupting it.

**Diode** (NetBox Labs, open source) is the write path. Producers send *what they observed*.
Diode matches each object against NetBox and decides whether to create it, update it or leave it
alone. The producer never has to work out NetBox IDs or ordering, and sending the same data twice is
safe. OSS Diode only creates and updates, though; it never deletes and never reports what is
*missing*. The **inventory-recon** tool in this repo fills that gap and makes every run auditable:

| Concern | Who handles it |
|---|---|
| Authenticated ingestion, matching, create/update, dependency ordering | Diode (ingester + reconciler + NetBox plugin) |
| Input validation (schema, duplicate serials/IPs/names) | inventory-recon (pydantic) |
| Plan before write: create / update / unchanged / stale | inventory-recon (`diff.py`) |
| Stale assets (in NetBox, absent from the source) | inventory-recon flags them through Diode: `status=offline` + tag `recon-stale`. Nothing is ever deleted |
| Proof the write landed: wait for convergence, verify each item, sweep for residual drift | inventory-recon (`reconcile.py`) |
| Audit: summary + detail reports, NetBox changelog write count | inventory-recon (`report.py`) + NetBox changelog (user `diode`) |

## 2. Components

```mermaid
flowchart LR
  subgraph Sources
    CMDB[CMDB export<br/>cmdb_baseline.json]
    DISC[Network discovery<br/>discovery_day1.json<br/><i>prod: Orb agent</i>]
  end

  subgraph Recon["inventory-recon (CLI container)"]
    VAL[validate<br/>pydantic]
    PLAN[plan<br/>diff vs NetBox]
    VER[verify + report]
  end

  subgraph Diode["Diode server 2.3.1"]
    NGX[diode-nginx<br/>gRPC ingress :8080]
    AUTH[diode-auth + Hydra<br/>OAuth2 client-credentials]
    ING[diode-ingester]
    RDS[(diode-redis<br/>stream)]
    REC[diode-reconciler<br/>auto-apply]
    DPG[(diode-postgres<br/>ingestion log)]
  end

  subgraph NetBox["NetBox 4.7.2"]
    PLG[Diode plugin 1.18.0<br/>/api/plugins/diode]
    NB[NetBox API / UI :8000]
    NPG[(netbox-postgres)]
  end

  CMDB & DISC --> VAL --> PLAN
  PLAN -- "SDK ingest (gRPC)" --> NGX
  NGX -- token introspection --> AUTH
  NGX --> ING --> RDS --> REC
  REC --- DPG
  REC -- "bulk plan+apply (OAuth2)" --> PLG --> NPG
  PLAN -- "read state (REST)" --> NB
  VER -- "re-read state + changelog" --> NB
  NB --- NPG
  VER --> RPT[[reports/<br/>summary.md, detail/*.csv, run.json]]
```

Only NetBox (`:8000`) and the Diode gRPC ingress (`:8080`) are published. Everything else stays
on the internal compose network. Three OAuth2 clients, each with its own scope, are generated per
install (`make init`):

| Client | Scope | Used by |
|---|---|---|
| `diode-ingest` | `diode:ingest` | inventory-recon, Orb agents, any SDK producer |
| `diode-to-netbox` | `netbox:read netbox:write` | Diode reconciler calling the NetBox plugin |
| `netbox-to-diode` | `diode:read diode:write` | NetBox plugin calling Diode |

## 3. Reconciliation process (one phase)

```mermaid
sequenceDiagram
  autonumber
  participant R as inventory-recon
  participant NB as NetBox
  participant D as Diode
  R->>R: load + validate snapshot (fail fast, exit 2)
  R->>NB: read devices / interfaces / IPs, changelog count
  R->>R: plan = diff(snapshot, NetBox, scope = snapshot sites)
  R->>D: ingest every device (+ interfaces, IPs), one request per device
  R->>D: partial update for stale devices (status=offline, tag recon-stale)
  D->>NB: match + create / update / no-op (async)
  loop every 5s until converged or timeout (600s)
    R->>NB: re-read state, recompute plan
    Note over R,D: at timeout/2: re-send only still-drifting devices once (idempotent)
  end
  R->>R: verify each planned item + residual-drift sweep
  R->>NB: changelog count (writes made by Diode)
  R->>R: summary.md, detail CSVs, run.json, exit 0 / 1
```

## 4. Decision rules

```mermaid
flowchart TD
  A[Object in snapshot?] -->|yes| B{Exists in NetBox?<br/>key = site/device}
  B -->|no| C[CREATE]
  B -->|yes| D{Any compared field differs?}
  D -->|yes| E[UPDATE<br/>one row per field]
  D -->|no| F[UNCHANGED]
  A -->|no| G{In NetBox, inside snapshot's sites?}
  G -->|no| H[Out of scope: ignored]
  G -->|yes| I[STALE]
  I -->|device| J[flag: status=offline + tag recon-stale<br/>never deleted]
  I -->|interface / IP| K[reported for review]
```

| Object | Identity | Compared fields |
|---|---|---|
| Device | site + name | role, manufacturer, model, platform, serial, status |
| Interface | site + device + name | type, enabled |
| IP address | address (with prefix length) | assigned interface |

Scope rule: a source can mark objects stale only in the sites it reports on, so a branch-only
discovery job can never flag data-center inventory.

## 5. Demo scenario (ground truth)

`scripts/generate_mock_data.py` builds 50 devices across 3 sites (2 data centers, 1 branch; Juniper,
Arista, Palo Alto, Opengear, Cisco, Fortinet), 141 interfaces and 54 IPs. It always produces the same
output. The day-1 discovery file has exactly this drift, and `tests/test_diff.py` fails if the
expected counts ever change:

| Drift | Devices | Expected outcome |
|---|---|---|
| Unknown device on the network | dal1-leaf-13, nyc1-acc-07, atl1-lab-sw01 | created, with interfaces + mgmt IP |
| Hardware RMA (serial changed) | dal1-leaf-04, nyc1-acc-03 | serial updated |
| OS upgrade | dal1-spine-01/02, atl1-spine-01 | platform eos-4.31 → eos-4.33 |
| Went live | atl1-leaf-11/12 | status planned → active |
| Mgmt re-addressed | nyc1-wlc-01, atl1-fw-02 | new IP created; old IP reported stale |
| Line card added | dal1-core-01 | interface et-0/0/2 created |
| Not seen by discovery | dal1-oob-02, atl1-leaf-07, nyc1-acc-06 | flagged offline + `recon-stale` |

## 6. Reliability measures

- **Idempotent by construction.** Every run sends the full snapshot and Diode decides what to write.
  Phase 2 of the demo proves it with *0 NetBox changelog writes*.
- **Verification is independent of Diode.** Success means NetBox actually holds the expected
  state, re-read through the REST API. Diode accepting the request is not enough.
- **Bounded waiting with one self-heal.** Diode applies changes asynchronously. The tool polls until
  NetBox converges. Halfway through the timeout it re-sends only the objects that still drift, once.
- **Fail closed.** An invalid snapshot is rejected before any write. Diode errors, missing items or
  unexpected drift fail the phase (exit 1) and are listed in the report.
- **Read-only to NetBox.** All writes go through Diode, so they are authenticated, logged under the
  `diode` user and visible in NetBox's changelog.
- **Pinned versions** for every image and Python dependency. Health-gated startup (`up --wait`).

## 7. Known limitations (MVP)

- Reconciles devices, interfaces and IPs. Cables, VLANs, prefixes and VMs are supported by Diode but
  not wired into the diff yet.
- Diode tags are additive. A device flagged `recon-stale` that reappears goes back to its source
  status, but the tag must be removed by hand.
- Diode has no delete operation. Retiring stale assets is a deliberate human step.
- Review-before-apply (`AUTO_APPLY_CHANGESETS=false`) needs a review UI that the OSS plugin does not
  include. This MVP plans with `recon plan` (dry run) instead.
- NetBox state is read in full (paged, 500 per page). That is fine for thousands of objects; filter
  by site before going to six figures.
