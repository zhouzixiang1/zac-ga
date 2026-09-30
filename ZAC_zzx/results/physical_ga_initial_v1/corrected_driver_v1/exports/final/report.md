# Physical GA initialization pilot

independent initialization pilot; not a replacement for accepted main results

Median of each field across all three seeds within a canonical circuit. Primary comparisons use the same all-four-arm complete in-domain circuit cohort. Fidelity ratios use log differences; other ratios are paired by circuit before geometric averaging.

Planned runs: 108; statuses: {'success': 101, 'timeout': 7}; OOD runs: 0.
Common complete circuit cohort: 8/9.

| Comparison | N | Geometric fidelity ratio | Win/tie/loss |
|---|---:|---:|---|
| ga_h2_vs_old_sa4_h2 | 8 | 1.0114955 | {'wins': 3, 'ties': 0, 'losses': 5} |
| ga_h2_vs_ga_h0 | 8 | 1.0009547 | {'wins': 4, 'ties': 0, 'losses': 4} |
| ga_h2_vs_random_h2 | 8 | 1.0121668 | {'wins': 4, 'ties': 1, 'losses': 3} |

Descriptive serial times across three stochastic configurations, not fixed-seed timing repetitions or a claim of runtime speedup.

No historical outcomes replace new missing/failed runs. Per-circuit and all-run CSVs retain the full planned inventory.
