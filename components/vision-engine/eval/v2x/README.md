# DEV post-pass variants

Implement any API in a new importable module. Each variant is evaluated independently against the same S2 baseline; no harness changes or registration are needed.

| Prefix | Exact API | Return contract |
|---|---|---|
| `gm` | `estimate_global_motion(prev_luma, cur_luma, max_d)` and `consensus_tiles(prev_luma, cur_luma, s, dx, dy)` | `(dx, dy, score)` or `None`, with `score` a Python int (engine law: integers only; meaning is variant-defined, e.g. SAD or ppm support); consensus returns a boolean NumPy mask of shape `(gh, gw)` in row-major order |
| `fl` | `class FlickerTracker(n_tiles, max_half=64)` with `update(hashes)` | `(states, half_periods)`, integer arrays of shape `(n_tiles,)` in Morton order |
| `st` | `subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d)` | `(dx, dy, n_blocks)` or `None` |

Inputs are read-only RAM arrays. Luma is uint8 `(H, W)`; hashes are S2 uint64. Coordinates and displacements are integers, positive dx/dy moves right/down. Global score is finite and nonnegative; subtile claims need positive integer block counts and a nonzero vector. Tracker states are class IDs in the existing range; only state FLICKER (`4`) claims a tile. Half periods are integers from zero through max_half, positive for a FLICKER claim.

Claim-only semantics: global consensus promotes claimed tiles to MOVED for a nonzero vector; subtile can promote only baseline CHANGED tiles to MOVED; tracker declarations promote to FLICKER. All fields of unclaimed tiles stay identical to baseline. Spatial APIs skip identical frames and need a previous frame; tracker updates every frame. Radius is `max(W,H)-1` for global and `s-1` for subtile. Each scene starts fresh tracker state. The original prototypes and baseline are always reference rows. References execute the original Python implementations, without native substitution or content caching.

```sh
taskset -c 2-4 python3 -B -m eval.v2eval.run postpasses --variant gm=eval.v2x.gm_foo --variant fl=eval.v2x.fl_bar --quick 12 --frame-budget-ms 1000
python3 -B -m eval.v2eval.run leaderboard
```

`--quick N` chooses exactly N sealed DEV scenes deterministically, round-robin by sorted kind, preferring small resolutions within each kind. For N smaller than the kind count, only the first N kinds are covered; the exact scene IDs are recorded. With `--variant` and without `--quick`, all sealed DEV scenes are selected. The original no-option `postpasses` command retains its accelerated legacy cache/report path; use `--quick` or `--variant` for the competitive leaderboard. No GATE or TEST scenes are ever generated. Source seals are checked before and after evaluation. An exception, import/constructor failure, malformed output or deadline failure marks that variant FAILED and suppresses its partial metrics; other variants continue. `--frame-budget-ms` bounds the complete frame API work (including all subtile calls) and initialization separately. Workers are terminated and joined at scene exit.

Outputs live in `eval/v2eval/`: `variant_results.json`, deterministic `LEADERBOARD.json`/`LEADERBOARD.md`, and separate `variant_diagnostics.json`. Undefined metrics remain null/undefined, never zero evidence. FAILED entries are not competitive scores. Cost is counted executed Python bytecodes across frame API calls and their Python dependencies, including contract validation; NumPy/C internals are opaque, and uninstrumented baseline cost is null. It is not total machine work or a cross-language ranking. Wall time is DIAGNOSTIC only; wall time in a VM is not evidence. Timeouts depend on the configured host/budget; determinism means identical recorded evidence serializes identically, not that deadline outcomes are host-independent.
