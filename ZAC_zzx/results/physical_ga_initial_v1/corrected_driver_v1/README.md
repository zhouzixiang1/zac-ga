# Physical GA initialization pilot — corrected run

**Finished: 108 jobs = 101 successes + 7 timeouts; common valid circuit N = 8/9; model OOD = 0.** All timeouts are Ising42. Statistics use the median of each field across all three seeds per circuit, then the same complete four-arm cohort.

| GA H2 compared with | Common N | Paired geometric fidelity ratio | Gain | Circuit wins / ties / losses |
|---|---:|---:|---:|---:|
| Old SA + four candidates, H2 | 8 | 1.0114955396662024 | 1.149554% | 3 / 0 / 5 |
| GA H0 | 8 | 1.0009546783722754 | 0.095468% | 4 / 0 / 4 |
| Same-domain random H2 | 8 | 1.0121668409907156 | 1.216684% | 4 / 1 / 3 |

Ratios are formed from paired differences between circuit-level median log fidelities and then geometrically aggregated. They do not imply that every circuit improves; the GA H2 comparison against the old initializer has more losses than wins.

| Arm | Planned | Success | Timeout |
|---|---:|---:|---:|
| Old SA + four candidates, H2 | 27 | 26 | 1 |
| GA H2 | 27 | 24 | 3 |
| GA H0 | 27 | 27 | 0 |
| Same-domain random H2 | 27 | 24 | 3 |

**Runtime is descriptive only.** The run changed from serial execution to at most 14 concurrent workers under later user authorization: 46 jobs had finished, one continued running, and 61 unstarted jobs entered the parallel queue. The frozen report's phrase “Descriptive serial times” records the original protocol and **does not describe the complete actual run**. Read the [actual scheduling and diagnostic addendum](exports/final/execution_addendum.md) before interpreting any wall-clock ratio. One timed-out job also received a single one-second diagnostic sample.

This opt-in initialization pilot does not replace default_initial_v1 or accepted main results. No failed run was retried or substituted. The original driver-aborted records remain in the parent directory; this complete corrected matrix has a separate protocol/source identity.

- [Quality results and original frozen report](exports/final/report.md)
- [Full structured summary](exports/final/result.json)
- [All 108 run records](exports/final/runs.csv) and [per-circuit medians](exports/final/circuits.csv)
- [Actual scheduling, full initialization times, budgets, and sampling](exports/final/execution_addendum.md)
- [Execution context and source hashes](exports/final/execution_context.json)
- [Frozen analysis provenance](exports/final/provenance.json) and [addendum provenance](exports/final/execution_context.provenance.json)
