# Scale benchmark

`scripts/bench_scale.py --devices 1000` on 2026-10-07 (single-host Docker, NetBox 4.7.2, Diode 2.3.1; best of 3 for reads).

| Operation | Time | Objects |
|---|--:|--:|
| Load bench site through Diode (create + verify) | 477s | 6500 |
| Read demo sites only (scoped) | 0.9s | 430 |
| Read bench site only (scoped) | 7.7s | 6500 |
| Read everything (unscoped) | 7.4s | 6930 |
| Plan (diff) bench site | 0.02s | 6500 |

Scoped reads cost scales with the sites being reconciled, not with the size of NetBox.
