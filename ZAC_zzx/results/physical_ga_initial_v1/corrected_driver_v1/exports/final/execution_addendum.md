# Actual execution and diagnostic addendum

The frozen matrix finished all 108 jobs: 101 successes and 7 timeouts; model OOD count is 0. All seven timeouts are Ising42, leaving 8/9 circuits in the all-four-arm, all-three-seed common valid cohort.

## Scheduling and timing

The prespecified run began serially. At 2026-09-12T14:18:47.795294+00:00, the user authorized parallel scheduling: 46 jobs had already completed, 1 was already running and continued unchanged, and 61 unstarted jobs entered the external queue. The observed peak was 14 overlapping task execution intervals (13 external workers plus the original serial worker). Algorithms, inputs, seeds, arm settings and each job's 600-second/3-GiB limits were unchanged.

The original frozen report/result retain the planned serial timing wording for reproducibility. This addendum records the actual mixed scheduling and supersedes that wording when interpreting runtime. All wall-clock times are descriptive only; do not present any ratio as a serial speed effect or as fixed-seed timing repetitions.

| Arm | Success / timeout | Common N | Median initialization (s) | Median full compile (s) |
|---|---:|---:|---:|---:|
| old_sa4_h2 | 26 / 1 | 8 | 3.681595 | 11.710013 |
| ga_h2 | 24 / 3 | 8 | 3.458198 | 14.102742 |
| ga_h0 | 27 / 0 | 8 | 2.711741 | 12.505900 |
| random_h2 | 24 / 3 | 8 | 3.462508 | 13.744901 |

Each displayed time is the median across the eight circuit-level medians; each circuit-level median uses the three trial seeds. Initialization includes the entire initial-placement stage, including SA in the old arm; selection preview time is a separate field.

## Search budget and diagnostic sample

Among completed search reports, GA H2 and random H2 both reached 32 unique physical mapping evaluations in every successful run (24 each). GA H0 used 29–32 evaluations across 27 successful runs: 25 terminated at the unique-evaluation budget and two at the prespecified patience limit. Timeout jobs had no completed selection report, so no completed budget count is imputed for them.

Ising42 seed1 random H2 received one read-only macOS sample: requested duration 1 second, interval 1 ms, command elapsed 1.233440 s. All 843 sampled main-thread stacks were inside solve_rich_h0 (including color_phase and ghost_hit_atoms), before the selected mapping was saved. This is a sampled initialization-prefix location, not a fraction of its full 600-second run. Its timing includes that diagnostic perturbation.

Source hashes, scheduling job membership, completion records, and the sample metadata/output are pinned in execution_context.json and execution_context.provenance.json. No historical result substitutes for a failed run; the independent pilot does not replace default_initial_v1 or accepted main results.
