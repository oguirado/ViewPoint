# Candidate test impact and sharding tools

`suites.txt` uses the surface IDs and order in the night integration receipt `INT_SUITES.tsv`. Each entry means unittest discovery of `test_*.py` in that surface's `tests/` directory. The vendored package suites and this tool's own tests have separate entry points.

Run from the repository root. Each run takes an explicit Git revision (default `HEAD`), creates a disposable snapshot below its fresh output directory, and runs the schedule and its workers in one capped scope. The archive and receipt serialization sit outside the schedule timer. The read-only dependency inputs must already exist locally. The MemoryWeb tools directory must have the upstream `standards/schemas/mw.v1.schema.json` alongside it in the checkout. Use `--suite` repeatedly to run a subset. Sharding requires a complete, successful timing receipt for the tested revision by default. To replay frozen weights from another revision, pass its exact commit to `--weights-rev`. Every scheduled suite still needs a positive measured weight.

```bash
impact=components/vp-direct/qa/impact
deps=(--rust "$local_rust_toolchain" --cargo-registry "$local_cargo_registry"
      --go "$local_go_toolchain" --go-modules "$local_go_module_cache"
      --memoryweb-tools "$local_memoryweb_tools")
scope=(systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G
       -p AllowedCPUs=12-15 --)

"${scope[@]}" python3 -IB "$impact/runner.py" collect \
  "${deps[@]}" --output "$impact/.validation-serial"
"${scope[@]}" python3 -IB "$impact/runner.py" shard \
  "${deps[@]}" --cpuset 12-15 --workers 4 --weights "$impact/.validation-serial" \
  --output "$impact/.validation-lpt"
"${scope[@]}" nice -n 19 python3 -IB "$impact/runner.py" compare \
  "$impact/.validation-serial" "$impact/.validation-lpt"
```

`collect` and `shard` default to `--cpuset 12-15` and nice 19 for validation. The runner accepts Linux CPU lists such as `12,14`, can narrow the caller's affinity, and verifies the effective mask in both the coordinator and each worker. Launch validation in a scope with matching `AllowedCPUs` to confine descendants too. CPUs 16–19 require an explicitly pinned caller and are reserved for timed Rust measurements. The historical guards described below can fail under background validation.

The ledger's `duration_s` measures elapsed time from unittest's start to stop callbacks. `cpu_s` sums user and system CPU deltas in the suite process and children it reaped in that interval. Class and module setup are included in the suite elapsed time used for scheduling. Fixture errors without an executed case carry null timings. The runner compares exact surface and test membership and every result. Shards keep a suite intact and place the longest remaining suite on the lightest shard with deterministic ties.

Build a separate base input receipt to select between two revisions. The selector follows tracked Python imports transitively at both revisions and consults audited Python reads and code loads from the base. The comparison with supplied paths uses Git's NUL-separated names. Changes under `qa/impact/`, shared fixtures, schemas, toolchain changes and missing dependency information select the full inventory.

```bash
"${scope[@]}" nice -n 19 taskset -c 12-15 python3 -IB "$impact/runner.py" audit \
  "${deps[@]}" --workers 4 --rev "$base" --output "$impact/.history-base"
"${scope[@]}" nice -n 19 taskset -c 12-15 python3 -IB "$impact/selector.py" select \
  --base "$base" --head "$head" --observed "$impact/.history-base"
"${scope[@]}" nice -n 19 taskset -c 12-15 python3 -IB "$impact/selector.py" verify-history "$head" \
  --before "$impact/.history-base" --after "$impact/.history-head"
```

Historic commits' replay and mutation guards can assert CPUs 16–19 themselves. Use a separate `taskset -c 16-19` capped `audit --guard-cpuset --suite qa/replay --suite qa/mutation` run after the timed schedule experiment as needed. Pass the rerun receipt with `--observed-retry`, `--before-retry` or `--after-retry`. An audit supplement must be green at the original revision and can replace only failed suites.

Runtime observations cover the Python suite process and only the exercised paths. Subprocess-only or unexercised dynamic inputs need a Wave C manifest or stronger tracing. Keep the full merge gate and do a fresh full audit when dependency inputs are uncertain. Wave C must pin suite discovery, dependency and input hashes, receipt fields, timing boundaries, affinity, competitor controls and scoring before promotion.

T-INT2 suite membership, outcome comparisons and receipt digests are in `FULL_SUITE_PARITY.json`. The timing deviations are in `AMEND-impact.md` and `AMEND-shards.md`.
