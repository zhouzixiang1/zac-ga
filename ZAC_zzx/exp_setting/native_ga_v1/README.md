# Native GA v1 tuning configuration

`ours_nl_shared.json` and `ours_lk_shared.json` are the causal-comparison pair:
they differ only in `method_id`, runner-owned `dir`, and `lookahead_horizon`.
Both methods use the registered geometric-decay
`physical_terminal_decay_v1` spec (`rho=0.6`, `epsilon=0.05`) and differ only in
`max_horizon`: M3 is strict zero and M4 is bounded at eight (six offsets are
effective under the default cutoff).  The current physical NLL is exact and
undiscounted; only future residency/re-entry/terminal heuristics receive
`alpha_lookahead*rho^(offset-1)`.  Both configs require the ABI3 native backend,
the registered wheel hash, and forbid a Python fallback.

The four method configs currently contain the registered default candidate.
Their `selection_status` remains `provisional_default_pending_validation` until
all 270 validation trials are sealed.  The tuning finalizer replaces them and
`selected_config_manifest.json` together; a pending file must never be cited as
a tuned result.

The execution sequence starts only after the sibling
`initial-placement/selected_engine.json` gate is complete.  `prepare` and every
mutating phase revalidate the clean commit, plan, suite, canonical inputs,
configs, selected initializer and native identity:

```text
python -m experiments_v2.tuning_cli prepare ...
# Run four processes with worker-index 0,1,2,3 respectively.
python -m experiments_v2.tuning_cli run-phase ... --phase screen --resume --worker-count 4 --worker-index 0
python -m experiments_v2.tuning_cli promote ... --phase screen
python -m experiments_v2.tuning_cli run-phase ... --phase successive_halving --resume --worker-count 4 --worker-index 0
python -m experiments_v2.tuning_cli promote ... --phase successive_halving
# Validation is deliberately serial and has no worker arguments.
python -m experiments_v2.tuning_cli run-phase ... --phase validation --resume
python -m experiments_v2.tuning_cli finalize ...
```

Resume reads only the deterministic sealed receipt for each scheduled trial. It
does not scan old compiler directories. Failed trials are retried only with the
explicit `--retry-failed` option, while every compiler launch remains preserved
in a unique UUID-bearing attempt directory.

Screen and successive-halving promotion is forbidden until all four static
`ordinal % 4` shards have sealed the complete ledger.  A stale claim is never
stolen automatically; after confirming its worker PID has exited, archive it
explicitly with `tuning_cli recover-claim --root ... --phase ... --trial-id ...`.
Transition times observed under parallel load are diagnostic/nonclaim and do
not break promotion ties; only the serial validation and formal Runtime tracks
may use implementation time for selection or paper conclusions.
