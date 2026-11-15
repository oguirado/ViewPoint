# HyperBook versus code audit: HB-VC-s2-change

## Source binding and method

Repository: `oguirado/vision-engine`. Requested ref: `bridge/integration`. The ref resolved in this session to commit `9637587da02809d2600a8cef0dc99b596e36a046`; every coordinate below refers to the exact files observed on that branch at this commit, not to the task card's older recorded commit.

The task card and metadata were read first. The book, claims, anchors, symbols, manifest, implementation, matching specification, and primary tests were fetched through the GitHub connector with an explicit ref. Each primary file was fetched again at the resolved commit, and its blob identity matched the branch read. Exact Base64-decoded bytes were checked against Git's blob SHA-1 before local inspection. Supplementary imports, matching tests, and cited receipts were retrieved and pinned the same way. No HyperBook or implementation changes are proposed by this audit.

| Evidence request | Exact source | SHA-256 of exact bytes |
|---|---|---|
| E0 | `hyperbooks/HB-VC-s2-change/book.md` | `39d16af5062fe6f289e98c082689aefc539505e7bc610630c9d322f5d0a5ac9b` |
| E1 | `hyperbooks/HB-VC-s2-change/manifest.json` | `0c5939e80950197099eb27c3b8413fda153eb939e41f59aa26b9a53c786108a3` |

Every line number was checked by reading the actual file. Each mismatch statement quotes at most 15 words from either side. Repeated instances of one semantic defect share one row and list their additional coordinates. For an omission, ABSENT means the identified item has no matching entry or explicit rule in the inspected scope; the book coordinate points to the actual enclosing contract, index record, or table header, never to an invented line.

The comparisons cover authored behavior, constants, thresholds, fields, formulas, evidence bindings, existing metadata records, standalone symbol coverage, and directly resolvable imports/calls within the generated three-file source set. A layout field is not considered undocumented merely because the book incorporates it through `layout.TILE_FIELDS`. Index omissions describe missing independently addressable records or edges; they do not imply that the implementation is absent or that the extractor promised a broader resolution policy. Test callers excluded from the table explicitly labeled “tool symbols” are not counted as missing tool-call rows.

Severity means: **blocker** prevents source binding or completion of the primary audit; **major** changes identity, behavior, a consequential algorithm/input contract, or oracle independence; **minor** affects an evidence label, resource description, detail, or index coverage. These are audit classifications, not orchestrator acceptance decisions.

## Behavioral, contract, evidence, and inventory mismatches

| ID | Kind | Book coordinate | Code or test coordinate | Mismatch | Severity |
|---|---|---|---|---|---|
| S2-001 | Manifest identity | `hyperbooks/HB-VC-s2-change/manifest.json:1` | `visioncore/change.py:1` | The manifest declares `"hb_id":"HB-ROUTER-002"`, whereas the implementation declares `"book":"HB-VC-s2-change"` and its book directory is HB-VC-s2-change. | major |
| S2-002 | Reversed inequality | `hyperbooks/HB-VC-s2-change/book.md:177` | `visioncore/change.py:61` | The larger-mask claim `never exceeds the residual with a smaller mask` reverses the monotonicity of `np.maximum(0, ham - hd)`, which cannot decrease when additional flipped bits are admitted. | major |
| S2-003 | Threshold boundary | `hyperbooks/HB-VC-s2-change/book.md:185`; `hyperbooks/HB-VC-s2-change/book.md:42` | `visioncore/change.py:329` | The interval `inside [T_exit, T_enter]` includes the exit boundary, but `r1 > t_exit` deactivates a hot tile exactly at that boundary. | major |
| S2-004 | Overstated class stability | `hyperbooks/HB-VC-s2-change/book.md:185`; `hyperbooks/HB-VC-s2-change/book.md:42`; `hyperbooks/HB-VC-s2-change/book.md:177`; `hyperbooks/HB-VC-s2-change/book.md:360` | `visioncore/change.py:447` | The guarantee `no class flip` is false for the final class because `cls[drift] = C_DRIFT` can replace JITTER while every interframe residual stays strictly inside the band. | major |
| S2-005 | Wrong clipped-edge class | `hyperbooks/HB-VC-s2-change/book.md:144` | `visioncore/change.py:390` | The claimed clipped-edge promotion `from FLICKER to MOVED` is implemented only as `flicker &= ~mexp`, leaving motion-explained partial-window tiles CHANGED when no full-window candidate was accepted. | major |
| S2-006 | Clipped-motion predicate omission | `hyperbooks/HB-VC-s2-change/book.md:144` | `visioncore/change.py:389` | The description `edge tiles explained on the clipped window` leaves the precise predicate ABSENT, including `2 * nvc >= nn`, the strict area-normalized SAD ratio, and the clipped-area residual ceiling. | major |
| S2-007 | Wrong pool membership | `hyperbooks/HB-VC-s2-change/book.md:144`; `hyperbooks/HB-VC-s2-change/book.md:177`; `hyperbooks/HB-VC-s2-change/book.md:360` | `visioncore/change.py:411`; `visioncore/change.py:378` | The pool is described as reaching `every active tile`, but `offer(v, cand_base, False)` uses the original active non-FLICKER set fixed before global overrides. | major |
| S2-008 | Aperture predicate omission | `hyperbooks/HB-VC-s2-change/book.md:146`; `hyperbooks/HB-VC-s2-change/book.md:177` | `visioncore/change.py:435` | The aperture description `explains at least as well as zero motion` leaves the minimum-overlap condition `2 * nv >= s_ * s_` and its area-normalized SAD comparison ABSENT. | major |
| S2-009 | Overbroad DRIFT suppression | `hyperbooks/HB-VC-s2-change/book.md:147` | `visioncore/change.py:445` | The requirement `the tile is not aperture-ambiguous` is stronger than `~(amb_lt \| (amb & (state.pre_mov > 0)))`, which permits an equal-quality ambiguous tile with no prior motion. | major |
| S2-010 | Motion-history input omission | `hyperbooks/HB-VC-s2-change/book.md:148` | `visioncore/change.py:471` | The history description `shifts in MOVED` omits `(mexp & active)`, which also records clipped motion-explained CHANGED tiles. | major |
| S2-011 | Previous-luma retention | `hyperbooks/HB-VC-s2-change/book.md:148`; `hyperbooks/HB-VC-s2-change/book.md:82` | `visioncore/change.py:481` | The unconditional update `y_prev <- y_cur` omits `if y_cur is not None:`, so a call without luma retains an older plane rather than clearing it. | major |
| S2-012 | Wrong static-HUD narrative | `hyperbooks/HB-VC-s2-change/book.md:412` | `tests/test_change.py:122`; `visioncore/change.py:424` | The static-HUD narrative says it `becomes FROZEN`, but the fixture requires `c[45:] == C.C_UNCHANGED` because a never-active HUD lacks the prior-activity latch. | major |
| S2-013 | First-frame flag exception | `hyperbooks/HB-VC-s2-change/book.md:49` | `visioncore/change.py:492` | The flag guarantee `both bits are cleared` omits the first-frame exception: `out = _as_tiles(tiles)` in classify_first preserves GLOBAL_MV and SHADE_ONLY on UNCHANGED output. | minor |
| S2-014 | Wrong exception type | `hyperbooks/HB-VC-s2-change/book.md:303` | `visioncore/change.py:109` | The documented failure `KeyError if a tile field is missing` disagrees with `tiles[n]`, which raises ValueError for a missing field in the documented NumPy structured-array input. | minor |
| S2-015 | Overstated grid validation | `hyperbooks/HB-VC-s2-change/book.md:291` | `visioncore/change.py:311` | The promised grid-change rejection `classify_tiles then raises E_VC_GRID_MISMATCH` exceeds `len(t) != n or len(words_eq) != n`, which checks counts but does not compare grid geometry. | major |
| S2-016 | Positive-N contract omission | `hyperbooks/HB-VC-s2-change/book.md:48` | `visioncore/change.py:422` | The precondition `len(tiles) == len(words_eq) == state.n` leaves N greater than zero ABSENT, while `n_active * 1000000 // n` raises ZeroDivisionError for equal-length empty inputs. | major |
| S2-017 | Reference clamp omission | `hyperbooks/HB-VC-s2-change/book.md:147` | `visioncore/change.py:443` | The `ref_max` latch description leaves the saturation value ABSENT, although `np.minimum(rr_all, 65535)` caps the stored maximum at 65535. | minor |
| S2-018 | Frame-counter update omission | `hyperbooks/HB-VC-s2-change/book.md:285` | `visioncore/change.py:323` | The state description gives `frame = -1` and initialization but leaves subsequent advancement ABSENT, although `state.frame += 1` runs after the length check. | minor |
| S2-019 | Wrong state-memory budget | `hyperbooks/HB-VC-s2-change/book.md:289` | `visioncore/change.py:132`; `visioncore/change.py:126`; `visioncore/change.py:133`; `visioncore/change.py:134` | The estimate `about 100 B per tile plus the three 32 B records` disagrees with the allocated arrays, including `t.copy(), t.copy(), t.copy()`, which total 131 bytes per tile before luma and object overhead. | minor |
| S2-020 | First-frame allocation omission | `hyperbooks/HB-VC-s2-change/book.md:277` | `visioncore/change.py:500` | The space budget `one tile copy` omits the three confidence-mask arrays retained by `cf, cf.copy(), cf.copy()` when initial luma is supplied. | minor |
| S2-021 | Wrong computational model | `hyperbooks/HB-VC-s2-change/book.md:366` | `visioncore/change.py:234` | The model `Python loop only for island labelling` contradicts `for lo in range(0, tx.size, 128):` and the additional projection, boundary-window, and candidate loops. | minor |
| S2-022 | Obsolete temporal memory model | `hyperbooks/HB-VC-s2-change/book.md:366` | `visioncore/change.py:353` | The temporal-domain storage claim `O(N) state plus int64 SAD planes` omits the per-vector SAD/count arrays created by `sad_cache[vec] = _tile_sad_clip` and retained for the current frame. | minor |
| S2-023 | Requested-window allocation omission | `hyperbooks/HB-VC-s2-change/book.md:366` | `visioncore/change.py:246` | The motion-domain description `O(W*H) int64 per SAD pass` omits the bounded requested-window allocation and the narrow-input fast path `np.subtract(cur, src, dtype=np.int32)` followed by int64 reductions. | minor |
| S2-024 | Missing island fallback qualifier | `hyperbooks/HB-VC-s2-change/book.md:396` | `tests/test_change_fix.py:90`; `tests/test_change_fix.py:89` | The condition beginning `the explained share is below` is insufficient for CHANGED because the test obtains MOVED vectors `{(4, 0), (-4, 0)}` without a published global vector. | major |
| S2-025 | Ambiguous residual guarantee | `hyperbooks/HB-VC-s2-change/book.md:42` | `visioncore/change.py:464` | The unqualified claim `its residual is 0` omits the output-residual exception in `np.where(drift, rr_all, r1)`; exact equality zeros r1 but does not zero a still-latched DRIFT output. | minor |
| S2-026 | Initial-mask parameter omission | `hyperbooks/HB-VC-s2-change/book.md:115` | `visioncore/change.py:499` | The initial-mask description for `classify_first` leaves the parameter source ABSENT, while code uses `P.PARAMS["SIG_DEAD_Q4"]` even if later steps receive a directly supplied custom table. | minor |
| S2-027 | Inferred-grid formula omission | `hyperbooks/HB-VC-s2-change/book.md:115` | `visioncore/change.py:101` | The mask-generation description names `_conf_of` but leaves grid inference ABSENT, whereas `gw, gh = int(tx.max()) + 1, int(ty.max()) + 1` derives dimensions from Morton coordinates when grid is omitted. | minor |
| S2-028 | Wrong state-field shape | `hyperbooks/HB-VC-s2-change/book.md:287` | `visioncore/change.py:498` | The state-output list groups y_prev under `arrays of length N`, but `state.y_prev = y_cur` retains the two-dimensional padded luma plane. | minor |
| S2-029 | Overstated periodic fallback | `hyperbooks/HB-VC-s2-change/book.md:399`; `hyperbooks/HB-VC-s2-change/book.md:381` | `tests/test_rt0_d02.py:258`; `tests/test_rt0_d02.py:257`; `tests/test_rt0_d02.py:264` | The unconditional repetitive-texture fallback `falls back to CHANGED` conflicts with `self.assertTrue(moved & classes[n], n)`, which requires MOVED in the periodic_tie scenario. | major |
| S2-030 | Wrong receipt binding | `hyperbooks/HB-VC-s2-change/book.md:386` | `tests/test_e12_sig_dead.py:87`; `hyperbooks/HB-VC-s2-change/receipts/R-s2-change-unittest.json:6` | The confidence-mask question binds its verification to `R-s2-change-unittest`, although `def test_masked_residual(self):` belongs to tests.test_e12_sig_dead rather than the receipt's tests.test_change suite. | minor |
| S2-031 | Overstated determinism evidence | `hyperbooks/HB-VC-s2-change/book.md:42`; `hyperbooks/HB-VC-s2-change/book.md:177`; `hyperbooks/HB-VC-s2-change/book.md:360` | `tests/test_change.py:174` | The cited test is presented as checking `tiles, summary and state digest`, but it retains only `o.tobytes()` and never compares the summary or state digest. | minor |
| S2-032 | Incorrect oracle independence | `hyperbooks/HB-VC-s2-change/book.md:177` | `tests/test_change.py:25` | The book labels run an `independent_reference`, but the helper delegates directly to `C.classify_tiles(st, frames[i], e, params=params)` and contains no independent classifier. | major |
| S2-033 | Stale test status | `hyperbooks/HB-VC-s2-change/book.md:381` | `tests/test_rt0_d02.py:263` | The book leaves `a periodic-texture test` at TARGET even though `def test_global_vector_ties_equal_sealed(self):` already exercises periodic projection ties, without establishing external-truth recall. | minor |
| S2-034 | Roadmap evidence scope | `hyperbooks/HB-VC-s2-change/book.md:126` | `tests/test_change.py:25` | RM-4 cites the frozen-patch test for `FROZEN latch with aperture rule, DRIFT latch`, but its call `C.classify_tiles(st, frames[i], e, params=params)` supplies no luma or grid and does not verify DRIFT. | minor |
| S2-035 | Roadmap evidence scope | `hyperbooks/HB-VC-s2-change/book.md:126` | `tests/test_change.py:166` | RM-5 cites test_resid_field for `SHADE_ONLY flag` and state/summary behavior, although its assertions inspect only residual values such as `int(o["resid"][0])`. | minor |
| S2-036 | Latch evidence scope | `hyperbooks/HB-VC-s2-change/book.md:177` | `tests/test_change.py:143` | The cited ramp test is attributed settlement behavior `settle the tile and advance ref`, but `self.assertLessEqual(first, 5)` checks onset and the fixture never constructs a plateau. | minor |
| S2-037 | Stall evidence scope | `hyperbooks/HB-VC-s2-change/book.md:381` | `tests/test_change.py:153` | The cited stationary-noise test does not establish silence `after DRIFT_STALL frames without a new maximum`; `self.assertFalse((o["cls"] >= 2).any())` inspects output classes without checking stall or reference advancement. | minor |
| S2-038 | Stale private-symbol inventory | `hyperbooks/HB-VC-s2-change/book.md:370` | `visioncore/change.py:205` | The inventory headed `Private:` omits _tile_sad_windows despite the definition `def _tile_sad_windows(y_cur, y_prev, c, s, tx, ty):` and correct documentation elsewhere in the book. | minor |

### Interpretation notes

- S2-003 concerns the lower endpoint; S2-004 is a separate issue because a final JITTER class can become DRIFT even while the activity mask stays cold.
- S2-013 identifies the omitted first-frame exception to the general flag description; the ordinary `classify_tiles` clearing rule is correct.
- S2-025 concerns the unqualified residual wording: its adjacent explanation about `r1` is correct, and the detailed algorithm at `hyperbooks/HB-VC-s2-change/book.md:148` correctly distinguishes reference residual from interframe residual.
- S2-026 concerns a directly supplied custom parameter table; `SIG_DEAD_Q4` is not profile-overridable through the registry (`visioncore/params.py:18`).
- S2-019 counts 11 bytes of counters/history, 24 bytes of confidence masks, and 96 bytes of descriptor records per tile, totaling 131 bytes; NumPy object overhead and the luma plane are additional.
- S2-029 and S2-033 establish that periodic-content tests and MOVED outcomes exist in the test source; independent external-truth recall remains ABSENT.

## Standalone symbol and anchor coverage omissions

The containing record is cited in both indexes because the omitted symbol itself has no line. All existing entries were checked separately and remain correctly bound to their source spans. Enum values are already documented at `hyperbooks/HB-VC-s2-change/book.md:69` and `hyperbooks/HB-VC-s2-change/book.md:84`; `classify_tiles.sad_for` is correctly described at `hyperbooks/HB-VC-s2-change/book.md:346`. These rows concern their missing standalone entries.

| ID | Kind | Book coordinate | Code or test coordinate | Mismatch | Severity |
|---|---|---|---|---|---|
| S2-039 | Standalone symbol/anchor omission | `hyperbooks/HB-VC-s2-change/symbols.jsonl:85`; `hyperbooks/HB-VC-s2-change/anchors.jsonl:85` | `visioncore/change.py:12` | Within `visioncore/change.py::__module__`, a standalone index entry for _POP8 is ABSENT in both metadata files despite the code definition `_POP8 = np.array`. | minor |
| S2-040 | Standalone symbol/anchor omission | `hyperbooks/HB-VC-s2-change/symbols.jsonl:85`; `hyperbooks/HB-VC-s2-change/anchors.jsonl:85` | `visioncore/change.py:64` | Within `visioncore/change.py::__module__`, a standalone index entry for _ALL64 is ABSENT in both metadata files despite the code definition `_ALL64 = np.uint64`. | minor |
| S2-041 | Standalone symbol/anchor omission | `hyperbooks/HB-VC-s2-change/symbols.jsonl:85`; `hyperbooks/HB-VC-s2-change/anchors.jsonl:85` | `visioncore/change.py:39` | Within `visioncore/change.py::__module__`, a standalone index entry for C_UNCHANGED is ABSENT in both metadata files despite the code definition `C_UNCHANGED`. | minor |
| S2-042 | Standalone symbol/anchor omission | `hyperbooks/HB-VC-s2-change/symbols.jsonl:85`; `hyperbooks/HB-VC-s2-change/anchors.jsonl:85` | `visioncore/change.py:39` | Within `visioncore/change.py::__module__`, a standalone index entry for C_JITTER is ABSENT in both metadata files despite the code definition `C_JITTER`. | minor |
| S2-043 | Standalone symbol/anchor omission | `hyperbooks/HB-VC-s2-change/symbols.jsonl:85`; `hyperbooks/HB-VC-s2-change/anchors.jsonl:85` | `visioncore/change.py:39` | Within `visioncore/change.py::__module__`, a standalone index entry for C_CHANGED is ABSENT in both metadata files despite the code definition `C_CHANGED`. | minor |
| S2-044 | Standalone symbol/anchor omission | `hyperbooks/HB-VC-s2-change/symbols.jsonl:85`; `hyperbooks/HB-VC-s2-change/anchors.jsonl:85` | `visioncore/change.py:39` | Within `visioncore/change.py::__module__`, a standalone index entry for C_MOVED is ABSENT in both metadata files despite the code definition `C_MOVED`. | minor |
| S2-045 | Standalone symbol/anchor omission | `hyperbooks/HB-VC-s2-change/symbols.jsonl:85`; `hyperbooks/HB-VC-s2-change/anchors.jsonl:85` | `visioncore/change.py:39` | Within `visioncore/change.py::__module__`, a standalone index entry for C_FLICKER is ABSENT in both metadata files despite the code definition `C_FLICKER`. | minor |
| S2-046 | Standalone symbol/anchor omission | `hyperbooks/HB-VC-s2-change/symbols.jsonl:85`; `hyperbooks/HB-VC-s2-change/anchors.jsonl:85` | `visioncore/change.py:39` | Within `visioncore/change.py::__module__`, a standalone index entry for C_FROZEN is ABSENT in both metadata files despite the code definition `C_FROZEN`. | minor |
| S2-047 | Standalone symbol/anchor omission | `hyperbooks/HB-VC-s2-change/symbols.jsonl:85`; `hyperbooks/HB-VC-s2-change/anchors.jsonl:85` | `visioncore/change.py:39` | Within `visioncore/change.py::__module__`, a standalone index entry for C_DRIFT is ABSENT in both metadata files despite the code definition `C_DRIFT`. | minor |
| S2-048 | Standalone symbol/anchor omission | `hyperbooks/HB-VC-s2-change/symbols.jsonl:90`; `hyperbooks/HB-VC-s2-change/anchors.jsonl:90` | `visioncore/change.py:197` | Within `visioncore/change.py::_morton_xy`, a standalone index entry for _morton_xy.comp is ABSENT in both metadata files despite the code definition `def comp(v):`. | minor |
| S2-049 | Standalone symbol/anchor omission | `hyperbooks/HB-VC-s2-change/symbols.jsonl:99`; `hyperbooks/HB-VC-s2-change/anchors.jsonl:99` | `visioncore/change.py:351` | Within `visioncore/change.py::classify_tiles`, a standalone index entry for classify_tiles.sad_for is ABSENT in both metadata files despite the code definition `def sad_for(vec):`. | minor |
| S2-050 | Standalone symbol/anchor omission | `hyperbooks/HB-VC-s2-change/symbols.jsonl:99`; `hyperbooks/HB-VC-s2-change/anchors.jsonl:99` | `visioncore/change.py:369` | Within `visioncore/change.py::classify_tiles`, a standalone index entry for classify_tiles.offer is ABSENT in both metadata files despite the code definition `def offer(vec, member, glob):`. | minor |
| S2-051 | Standalone symbol/anchor omission | `hyperbooks/HB-VC-s2-change/symbols.jsonl:27`; `hyperbooks/HB-VC-s2-change/anchors.jsonl:27` | `tests/test_change.py:88` | Within `tests/test_change.py::test_hysteresis_no_flip_in_band`, a standalone index entry for TestClasses.test_hysteresis_no_flip_in_band.frame is ABSENT in both metadata files despite the code definition `def frame(k):`. | minor |
| S2-052 | Standalone symbol/anchor omission | `hyperbooks/HB-VC-s2-change/symbols.jsonl:9`; `hyperbooks/HB-VC-s2-change/anchors.jsonl:9` | `tests/test_change.py:198` | Within `tests/test_change.py::TestMotion._setup`, a standalone index entry for TestMotion._setup.tiles is ABSENT in both metadata files despite the code definition `def tiles(y):`. | minor |
| S2-053 | Standalone symbol/anchor omission | `hyperbooks/HB-VC-s2-change/symbols.jsonl:62`; `hyperbooks/HB-VC-s2-change/anchors.jsonl:62` | `tests/test_change_fix.py:151` | Within `tests/test_change_fix.py::test_mv_ratio_from_passed_params`, a standalone index entry for TestParamsArgUsed.test_mv_ratio_from_passed_params.n_moved is ABSENT in both metadata files despite the code definition `def n_moved(params):`. | minor |
| S2-054 | Standalone symbol/anchor omission | `hyperbooks/HB-VC-s2-change/symbols.jsonl:110`; `hyperbooks/HB-VC-s2-change/anchors.jsonl:110` | `visioncore/change.py:58` | Within `visioncore/change.py::residual`, a standalone index entry for residual.d is ABSENT in both metadata files despite the code definition `d = lambda f:`. | minor |

## Generated import coverage omissions

Only missing imports between the three generated source files are counted; external-module coverage policy is ABSENT.

| ID | Kind | Book coordinate | Code or test coordinate | Mismatch | Severity |
|---|---|---|---|---|---|
| S2-055 | Generated import omission | `hyperbooks/HB-VC-s2-change/book.md:505` | `tests/test_change.py:6` | The generated row for `tests/test_change.py` lists no imports, omitting the in-source-set edge tests.test_change to visioncore.change established by `from visioncore import change as C, layout, params as P`. | minor |
| S2-056 | Generated import omission | `hyperbooks/HB-VC-s2-change/book.md:506` | `tests/test_change_fix.py:6` | The generated row for `tests/test_change_fix.py` lists no imports, omitting the in-source-set edge tests.test_change_fix to visioncore.change established by `from visioncore import change as C, layout, params as P`. | minor |
| S2-057 | Generated import omission | `hyperbooks/HB-VC-s2-change/book.md:506` | `tests/test_change_fix.py:7` | The generated row for `tests/test_change_fix.py` lists no imports, omitting the in-source-set edge tests.test_change_fix to tests.test_change established by `from tests.test_change import mk, run`. | minor |

## Generated static-call coverage omissions

These are direct local or explicit-import-resolvable calls. An enclosing table header is used when the caller has no row. The current extractor may attribute a nested body's calls to its enclosing tool; existing parent-attributed edges are therefore not labeled fabricated. The rows below record missing independently named edges.

| ID | Kind | Book coordinate | Code or test coordinate | Mismatch | Severity |
|---|---|---|---|---|---|
| S2-058 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:511` | `tests/test_change.py:20` | The generated `calls` entry leaves the independently named edge run to ClassState.init ABSENT, although the call `C.ClassState.init(frames[0])` exists in the documented source. | minor |
| S2-059 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:511` | `tests/test_change.py:21` | The generated `calls` entry leaves the independently named edge run to classify_first ABSENT, although the call `C.classify_first(st, frames[0])` exists in the documented source. | minor |
| S2-060 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:511` | `tests/test_change.py:25` | The generated `calls` entry leaves the independently named edge run to classify_tiles ABSENT, although the call `C.classify_tiles(st, frames[i], e, params=params)` exists in the documented source. | minor |
| S2-061 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:511` | `tests/test_change_fix.py:12` | The generated `calls` entry leaves the independently named edge grid_tiles to tests.test_change.mk ABSENT, although the call `mk(gw * gh)` exists in the documented source. | minor |
| S2-062 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:514` | `tests/test_change_fix.py:121` | The generated `tests/test_change_fix.py::TestShadeOnly._step` entry leaves the independently named edge TestShadeOnly._step to ClassState.init ABSENT, although the call `C.ClassState.init(t0)` exists in the documented source. | minor |
| S2-063 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:514` | `tests/test_change_fix.py:122` | The generated `tests/test_change_fix.py::TestShadeOnly._step` entry leaves the independently named edge TestShadeOnly._step to classify_first ABSENT, although the call `C.classify_first(st, t0, y_cur=prev)` exists in the documented source. | minor |
| S2-064 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:514` | `tests/test_change_fix.py:123` | The generated `tests/test_change_fix.py::TestShadeOnly._step` entry leaves the independently named edge TestShadeOnly._step to classify_tiles ABSENT, although the call `C.classify_tiles(st, t1, np.zeros(len(t1), bool), y_cur=cur_plane, grid=(s, gw, gh))` exists in the documented source. | minor |
| S2-065 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:511` | `tests/test_change_fix.py:164` | The generated `calls` entry leaves the independently named edge TestBoundaries._state to mk ABSENT, although the call `mk(n)` exists in the documented source. | minor |
| S2-066 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:511` | `tests/test_change_fix.py:165` | The generated `calls` entry leaves the independently named edge TestBoundaries._state to ClassState.init ABSENT, although the call `C.ClassState.init(t)` exists in the documented source. | minor |
| S2-067 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:511` | `tests/test_change_fix.py:166` | The generated `calls` entry leaves the independently named edge TestBoundaries._state to classify_first ABSENT, although the call `C.classify_first(st, t)` exists in the documented source. | minor |
| S2-068 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:513` | `tests/test_change.py:206` | The generated `tests/test_change.py::TestMotion._setup` entry leaves the independently named edge TestMotion._setup to TestMotion._setup.tiles ABSENT, although the call `tiles(prev)` exists in the documented source. | minor |
| S2-069 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:511` | `tests/test_change.py:199` | The generated `calls` entry leaves the independently named edge TestMotion._setup.tiles to mk ABSENT, although the call `mk(gw * gh)` exists in the documented source. | minor |
| S2-070 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:511` | `tests/test_change.py:89` | The generated `calls` entry leaves the independently named edge test_hysteresis_no_flip_in_band.frame to mk ABSENT, although the call `mk(2)` exists in the documented source. | minor |
| S2-071 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:511` | `tests/test_change_fix.py:152` | The generated `calls` entry leaves the independently named edge test_mv_ratio_from_passed_params.n_moved to ClassState.init ABSENT, although the call `C.ClassState.init(t0)` exists in the documented source. | minor |
| S2-072 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:511` | `tests/test_change_fix.py:153` | The generated `calls` entry leaves the independently named edge test_mv_ratio_from_passed_params.n_moved to classify_first ABSENT, although the call `C.classify_first(st, t0, y_cur=prev)` exists in the documented source. | minor |
| S2-073 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:511` | `tests/test_change_fix.py:154` | The generated `calls` entry leaves the independently named edge test_mv_ratio_from_passed_params.n_moved to classify_tiles ABSENT, although the call `C.classify_tiles` exists in the documented source. | minor |
| S2-074 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:511` | `visioncore/change.py:202` | The generated `calls` entry leaves the independently named edge _morton_xy to _morton_xy.comp ABSENT, although the call `return comp(m), comp(m >> 1)` exists in the documented source. | minor |
| S2-075 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:520` | `visioncore/change.py:363` | The generated `visioncore/change.py::classify_tiles` entry leaves the independently named edge classify_tiles to classify_tiles.sad_for ABSENT, although the call `sad_for((0, 0))` exists in the documented source. | minor |
| S2-076 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:520` | `visioncore/change.py:382` | The generated `visioncore/change.py::classify_tiles` entry leaves the independently named edge classify_tiles to classify_tiles.offer ABSENT, although the call `offer(gv, cand_base, True)` exists in the documented source. | minor |
| S2-077 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:511` | `visioncore/change.py:353` | The generated `calls` entry leaves the independently named edge classify_tiles.sad_for to _tile_sad_clip ABSENT, although the call `_tile_sad_clip(y_cur, state.y_prev, vec, s, gh, gw, tx, ty)` exists in the documented source. | minor |
| S2-078 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:511` | `visioncore/change.py:370` | The generated `calls` entry leaves the independently named edge classify_tiles.offer to classify_tiles.sad_for ABSENT, although the call `sad_for(vec)` exists in the documented source. | minor |
| S2-079 | Generated static-call omission | `hyperbooks/HB-VC-s2-change/book.md:523` | `visioncore/change.py:61` | The generated `visioncore/change.py::residual` entry leaves the independently named edge residual to residual.d ABSENT, although the call `d("y_q4")` exists in the documented source. | minor |

## VM checks and observed counterexamples

The required primary suite ran against the unchanged connector-fetched source snapshot with bytecode writes disabled:

```text
VM command: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_change
.......................
----------------------------------------------------------------------
Ran 23 tests in 0.182s

OK
VM exit code: 0
```

The duration above is **VM** timing only. The host determines performance. The separate required `test -s docs/audit/HB-VC-s2-change.md` result is recorded in the task report after this file is written.

Small deterministic probes used the existing in-RAM descriptor fixtures to resolve the disputed semantics; they did not change the test suite:

```text
VM behavioral counterexamples; no timing measurements
band lower boundary: [(0, 0), (2, 50), (1, 7)]
constant residual 30 strictly inside band: [(0, 0), (1, 7), (6, 15), (6, 22)]
words_eq after drift: [(0, 0), (1, 5), (6, 10), (6, 10)]
small/larger mask residual: 0 8
classify_first cls/flags: 0 68
missing field: ValueError no field of name xh64
state bytes/tile excluding luma: 131
y_prev retained when y_cur None: True
empty matching arrays: ZeroDivisionError integer division or modulo by zero
```

The class/residual sequences use `y_q4` values `[1000,1200,1228]`, `[1000,1030,1060,1090]`, and `[1000,1020,1040,1040]` respectively with the existing `tests.test_change.mk` and `run` helpers (`tests/test_change.py:10`, `tests/test_change.py:18`). The mask comparison flips three bits and permits zero versus three bits. The first-frame flag input is GLOBAL_MV plus SHADE_ONLY.

A clipped-window witness reused the fixed-seed scroll fixture at `tests/test_change.py:192`, setting the left-edge previous-two-frame descriptors to the current descriptors so those active edge tiles initially satisfy A-B-A:

```text
VM exact RAM edge suppression counterexample
global_mv [4, 0]
initial flicker at clipped left edge tile indices [0, 8, 24]
class [2, 2, 2] mv [0, 0, 0] motion-history bit [1, 1, 1]
edge source counts [192, 192, 192]
```

This simultaneously witnesses CHANGED output and recorded motion history for 192-of-256-pixel source windows. Frames, luma planes, pixel buffers, and fixture arrays remained in RAM; no binary artifact is part of the change.

## Verified metadata and provenance

All **113 anchor records** and **113 symbol records** have matching IDs, paths, kinds, source byte slices, raw slice hashes, byte counts, and line spans in the current source. All **50 generated tool-symbol rows** at `hyperbooks/HB-VC-s2-change/book.md:436` through `hyperbooks/HB-VC-s2-change/book.md:485` match those entries. No stale-anchor-span finding is warranted.

| Current generated source inventory | Lines | UTF-8 bytes | SHA-256 |
|---|---:|---:|---|
| `tests/test_change.py` | 243 | 9600 | `8517e74eba1cc566df86a0704fa944a1d57971b7e7d02acdb1caadb9a71a8ab2` |
| `tests/test_change_fix.py` | 213 | 9391 | `7f9feb4ba870da43986c893def152feabfc3f2918f65703722503dda624c55f6` |
| `visioncore/change.py` | 502 | 23772 | `2659416b081a9a8014898b7aa554c8d37a2cc8ae513ac34c73e29710a146fdec` |

The inventory statements at `hyperbooks/HB-VC-s2-change/book.md:428`, `hyperbooks/HB-VC-s2-change/book.md:429`, `hyperbooks/HB-VC-s2-change/book.md:430` and the repeated provenance entries at `hyperbooks/HB-VC-s2-change/book.md:575`, `hyperbooks/HB-VC-s2-change/book.md:576`, `hyperbooks/HB-VC-s2-change/book.md:577` match the observed bytes. The three `claims.jsonl` objects match their embedded book copies at lines 162, 167, and 172.

The historical S2 receipt's four source hashes match the current test, implementation, layout, and parameter files (`hyperbooks/HB-VC-s2-change/receipts/R-s2-change-unittest.json:28`, `hyperbooks/HB-VC-s2-change/receipts/R-s2-change-unittest.json:29`, `hyperbooks/HB-VC-s2-change/receipts/R-s2-change-unittest.json:30`, `hyperbooks/HB-VC-s2-change/receipts/R-s2-change-unittest.json:31`); its older recorded commit is not by itself a source mismatch. The RT0D02 receipt's new digest also matches the current implementation.

The companion specification was consulted as supporting evidence, but its implementation snapshot and several declared hashes differ from current source; those supplementary provenance discrepancies are detailed in the report. All audit coordinates come from current source reads.

## ABSENT and limits of establishment

- A specified safe error/return contract for empty S2 inputs, and same-count grid-geometry validation, are ABSENT; the actual observed empty-input exception and the implemented length check are reported above.
- An independent classifier inside `tests.test_change.run` is ABSENT; the helper calls the production implementation, while expected-class assertions live in test methods.
- Complete historical runtime-edge evidence for the rendered count of 417 and graph-node count of 126 is ABSENT from the inspected source set; abbreviated runtime rows are not treated as missing tests or fabricated edges.
- A newly executed RT0D02 sealed-baseline equivalence result is ABSENT: the matching test and receipt were read, but the sealed baseline and proof table were not fetched or executed.
- New calibration trials are ABSENT; the existing JSON calibration record was read and its selected row records 1020 runs and zero false-positive runs, consistent with the validation test.
- Host performance, a latency guarantee, external literature/excerpt truth, and real-world motion recall are ABSENT from this audit's evidence; the book's 3 ms budget remains a target.
- The correctness of a claim's specifically cited witness is distinguished from the truth of the implementation property; an incomplete witness is not presented as a demonstrated implementation failure.

## Mismatch counts by severity

| Severity | Count |
|---|---:|
| blocker | 0 |
| major | 17 |
| minor | 62 |
| **Total** | **79** |

The total includes 38 behavioral/contract/evidence/inventory rows and 41 standalone-index or generated-edge coverage rows. Collection and acceptance remain the orchestrator's decisions.

## Claims verified as consistent

Each row identifies the exact supporting coordinates and scopes the statement to the evidence actually available.

| ID | Verified claim | Book or claims coordinate | Supporting code, test, or receipt coordinate |
|---|---|---|---|
| V001 | C-S2-TESTS: the source-matched receipt records 23 tests, also observed in this VM run. | `hyperbooks/HB-VC-s2-change/claims.jsonl:1` | `hyperbooks/HB-VC-s2-change/receipts/R-s2-change-unittest.json:22` |
| V002 | C-S2-FAILURES: the source-matched receipt records zero failures, also observed in this VM run. | `hyperbooks/HB-VC-s2-change/claims.jsonl:2` | `hyperbooks/HB-VC-s2-change/receipts/R-s2-change-unittest.json:20` |
| V003 | C-S2-ERRORS: the source-matched receipt records zero errors, also observed in this VM run. | `hyperbooks/HB-VC-s2-change/claims.jsonl:3` | `hyperbooks/HB-VC-s2-change/receipts/R-s2-change-unittest.json:19` |
| V004 | T0_Q4 = 32 | `hyperbooks/HB-VC-s2-change/book.md:92` | `visioncore/params.py:15` |
| V005 | HYST_BAND_PERMILLE = 100 | `hyperbooks/HB-VC-s2-change/book.md:93` | `visioncore/params.py:16` |
| V006 | H_DEAD = 2 | `hyperbooks/HB-VC-s2-change/book.md:94` | `visioncore/params.py:17` |
| V007 | SIG_DEAD_Q4 = 24 | `hyperbooks/HB-VC-s2-change/book.md:95` | `visioncore/params.py:18` |
| V008 | DRIFT_U_Q4 = 35 | `hyperbooks/HB-VC-s2-change/book.md:96` | `visioncore/params.py:22` |
| V009 | DRIFT_C_Q4 = 8; the complete S2 source contains no read of this parameter. | `hyperbooks/HB-VC-s2-change/book.md:97` | `visioncore/params.py:21` |
| V010 | DRIFT_STALL = 8 | `hyperbooks/HB-VC-s2-change/book.md:98` | `visioncore/params.py:26` |
| V011 | FROZEN_M = 8 | `hyperbooks/HB-VC-s2-change/book.md:99` | `visioncore/params.py:24` |
| V012 | FROZEN_COV_PPM = 10000 | `hyperbooks/HB-VC-s2-change/book.md:100` | `visioncore/params.py:25` |
| V013 | MV_SAD_RATIO_DEN = 3 | `hyperbooks/HB-VC-s2-change/book.md:101` | `visioncore/params.py:27` |
| V014 | GLOBAL_SHARE_PPM = 500000 | `hyperbooks/HB-VC-s2-change/book.md:102` | `visioncore/params.py:28` |
| V015 | FROZEN_PRE_ACT = 8 | `hyperbooks/HB-VC-s2-change/book.md:103` | `visioncore/change.py:31` |
| V016 | W_CHROMA = 8 | `hyperbooks/HB-VC-s2-change/book.md:104` | `visioncore/change.py:29` |
| V017 | W_HAM = 8 | `hyperbooks/HB-VC-s2-change/book.md:105` | `visioncore/change.py:30` |
| V018 | MOTION_D_MAX = 64 | `hyperbooks/HB-VC-s2-change/book.md:106` | `visioncore/change.py:33` |
| V019 | SHADE_DEN = 64 | `hyperbooks/HB-VC-s2-change/book.md:107` | `visioncore/change.py:34` |
| V020 | SHADE_EDGE_MAX = 8 | `hyperbooks/HB-VC-s2-change/book.md:108` | `visioncore/change.py:35` |
| V021 | ISLAND_MAX = 16 | `hyperbooks/HB-VC-s2-change/book.md:109` | `visioncore/change.py:36` |
| V022 | T_enter uses integer floor scaling by the positive hysteresis band. | `hyperbooks/HB-VC-s2-change/book.md:80` | `visioncore/change.py:50` |
| V023 | T_exit uses integer floor scaling by the negative hysteresis band. | `hyperbooks/HB-VC-s2-change/book.md:80` | `visioncore/change.py:51` |
| V024 | Effective drift_u is max(T_enter, DRIFT_U_Q4). | `hyperbooks/HB-VC-s2-change/book.md:80` | `visioncore/change.py:51` |
| V025 | Public classify_tiles signature and optional arguments match. | `hyperbooks/HB-VC-s2-change/book.md:46` | `visioncore/change.py:303` |
| V026 | Length mismatch raises E_VC_GRID_MISMATCH. | `hyperbooks/HB-VC-s2-change/book.md:55` | `visioncore/change.py:312` |
| V027 | General-step flags clear GLOBAL_MV and SHADE_ONLY before classification. | `hyperbooks/HB-VC-s2-change/book.md:49` | `visioncore/change.py:321` |
| V028 | C-S2-UNCH: exact equality forces r1 to zero. | `hyperbooks/HB-VC-s2-change/book.md:184` | `visioncore/change.py:327` |
| V029 | INV-S2-EXACT: exact equality excludes active classification. | `hyperbooks/HB-VC-s2-change/book.md:177` | `visioncore/change.py:329` |
| V030 | Residual weights, additive terms, dead zone, and integer result match. | `hyperbooks/HB-VC-s2-change/book.md:78` | `visioncore/change.py:61` |
| V031 | Residual mask=None counts all sig64 bits. | `hyperbooks/HB-VC-s2-change/book.md:78` | `visioncore/change.py:60` |
| V032 | Seven class identifiers retain the documented order 0 through 6. | `hyperbooks/HB-VC-s2-change/book.md:84` | `visioncore/change.py:39` |
| V033 | SHADE_ONLY is bit 2. | `hyperbooks/HB-VC-s2-change/book.md:84` | `visioncore/change.py:37` |
| V034 | GLOBAL_MV is bit 6. | `hyperbooks/HB-VC-s2-change/book.md:84` | `visioncore/change.py:38` |
| V035 | Confidence uses the strict integer deviation predicate. | `hyperbooks/HB-VC-s2-change/book.md:115` | `visioncore/change.py:91` |
| V036 | Nonpositive confidence dead band gives all-one masks. | `hyperbooks/HB-VC-s2-change/book.md:227` | `visioncore/change.py:76` |
| V037 | Confidence intermediates widen to int64 above tile size 64. | `hyperbooks/HB-VC-s2-change/book.md:230` | `visioncore/change.py:89` |
| V038 | The tile dtype is asserted compatible with the declared layout size. | `hyperbooks/HB-VC-s2-change/book.md:136` | `visioncore/change.py:26` |
| V039 | Activity history is captured on an active-to-inactive transition. | `hyperbooks/HB-VC-s2-change/book.md:141` | `visioncore/change.py:335` |
| V040 | Motion history is captured on an active-to-inactive transition. | `hyperbooks/HB-VC-s2-change/book.md:141` | `visioncore/change.py:336` |
| V041 | still_run saturates at 255. | `hyperbooks/HB-VC-s2-change/book.md:82` | `visioncore/change.py:337` |
| V042 | The documented hot/cold activity comparisons are correct when distinguished from final class stability. | `hyperbooks/HB-VC-s2-change/book.md:140` | `visioncore/change.py:329` |
| V043 | The provisional class for nonexact inactive tiles is JITTER. | `hyperbooks/HB-VC-s2-change/book.md:142` | `visioncore/change.py:341` |
| V044 | Initial FLICKER detection is descriptor-level A-B-A. | `hyperbooks/HB-VC-s2-change/book.md:143` | `visioncore/change.py:345` |
| V045 | Motion acceptance requires a complete source window. | `hyperbooks/HB-VC-s2-change/book.md:144` | `visioncore/change.py:371` |
| V046 | Motion acceptance requires strict SAD-ratio improvement. | `hyperbooks/HB-VC-s2-change/book.md:144` | `visioncore/change.py:372` |
| V047 | Motion acceptance uses the inclusive residual ceiling. | `hyperbooks/HB-VC-s2-change/book.md:144` | `visioncore/change.py:372` |
| V048 | A later candidate replaces the best only with strictly smaller SAD. | `hyperbooks/HB-VC-s2-change/book.md:346` | `visioncore/change.py:372` |
| V049 | Global publication uses the inclusive explained-share threshold. | `hyperbooks/HB-VC-s2-change/book.md:144` | `visioncore/change.py:383` |
| V050 | Island order uses descending size, label tie order, and ISLAND_MAX. | `hyperbooks/HB-VC-s2-change/book.md:347` | `visioncore/change.py:401` |
| V051 | Island candidates are offered in sorted vector order. | `hyperbooks/HB-VC-s2-change/book.md:346` | `visioncore/change.py:408` |
| V052 | Active class assignment precedence ends with FLICKER after CHANGED and MOVED. | `hyperbooks/HB-VC-s2-change/book.md:145` | `visioncore/change.py:418` |
| V053 | Frame liveness uses publication or active-tile coverage. | `hyperbooks/HB-VC-s2-change/book.md:146` | `visioncore/change.py:422` |
| V054 | The base FROZEN predicate requires stillness and prior activity. | `hyperbooks/HB-VC-s2-change/book.md:146` | `visioncore/change.py:424` |
| V055 | The aperture FROZEN exception uses non-motion prior activity. | `hyperbooks/HB-VC-s2-change/book.md:146` | `visioncore/change.py:437` |
| V056 | FROZEN excludes a tile from DRIFT candidacy. | `hyperbooks/HB-VC-s2-change/book.md:147` | `visioncore/change.py:440` |
| V057 | Settling uses stall greater than or equal to DRIFT_STALL. | `hyperbooks/HB-VC-s2-change/book.md:147` | `visioncore/change.py:446` |
| V058 | SHADE_ONLY uses the documented signed-pixel counts and edge difference. | `hyperbooks/HB-VC-s2-change/book.md:147` | `visioncore/change.py:459` |
| V059 | DRIFT output uses the residual against the reference. | `hyperbooks/HB-VC-s2-change/book.md:148` | `visioncore/change.py:464` |
| V060 | Output resid is min(255, r shifted right by 2). | `hyperbooks/HB-VC-s2-change/book.md:148` | `visioncore/change.py:465` |
| V061 | UNCHANGED output residual is zero. | `hyperbooks/HB-VC-s2-change/book.md:148` | `visioncore/change.py:467` |
| V062 | Reference updates occur for CHANGED, MOVED, FLICKER, and settled tiles. | `hyperbooks/HB-VC-s2-change/book.md:148` | `visioncore/change.py:472` |
| V063 | The summary reports seven class counts. | `hyperbooks/HB-VC-s2-change/book.md:50` | `visioncore/change.py:485` |
| V064 | C-S2-COV and INV-S2-COV: coverage excludes UNCHANGED and JITTER. | `hyperbooks/HB-VC-s2-change/book.md:187` | `visioncore/change.py:486` |
| V065 | Projection cost normalizes overlap with floor division. | `hyperbooks/HB-VC-s2-change/book.md:239` | `visioncore/change.py:168` |
| V066 | Projection ties use cost, magnitude, then signed displacement. | `hyperbooks/HB-VC-s2-change/book.md:239` | `visioncore/change.py:171` |
| V067 | Projection acceptance uses the literal ratio 3. | `hyperbooks/HB-VC-s2-change/book.md:242` | `visioncore/change.py:174` |
| V068 | Motion search is capped at min(s, MOTION_D_MAX). | `hyperbooks/HB-VC-s2-change/book.md:238` | `visioncore/change.py:182` |
| V069 | Island projection bounds include one tile of margin. | `hyperbooks/HB-VC-s2-change/book.md:251` | `visioncore/change.py:298` |
| V070 | Initialization copies three descriptor record arrays. | `hyperbooks/HB-VC-s2-change/book.md:299` | `visioncore/change.py:132` |
| V071 | digest_bytes uses the specified stable array order. | `hyperbooks/HB-VC-s2-change/book.md:311` | `visioncore/change.py:139` |
| V072 | Luma is padded by edge replication. | `hyperbooks/HB-VC-s2-change/book.md:323` | `visioncore/change.py:154` |
| V073 | Requested tile indices are validated before signed conversion. | `hyperbooks/HB-VC-s2-change/book.md:344` | `visioncore/change.py:217` |
| V074 | Valid negative tile indices are retained. | `hyperbooks/HB-VC-s2-change/book.md:344` | `visioncore/change.py:225` |
| V075 | Full requested windows are processed in batches capped at 128. | `hyperbooks/HB-VC-s2-change/book.md:344` | `visioncore/change.py:234` |
| V076 | SAD accumulation uses int64. | `hyperbooks/HB-VC-s2-change/book.md:344` | `visioncore/change.py:250` |
| V077 | Per-vector SAD/count results are cached within the frame. | `hyperbooks/HB-VC-s2-change/book.md:346` | `visioncore/change.py:352` |
| V078 | Default thresholds are exactly (35, 28, 35). | `hyperbooks/HB-VC-s2-change/book.md:80` | `tests/test_change.py:32` |
| V079 | The record-layout test checks the 32-byte tile size. | `hyperbooks/HB-VC-s2-change/book.md:82` | `tests/test_change.py:39` |
| V080 | C-S2-DRIFT: the cited synthetic ramp reaches DRIFT within five frames. | `hyperbooks/HB-VC-s2-change/book.md:188` | `tests/test_change.py:143` |
| V081 | C-S2-DRIFT: the cited stationary-noise fixture contains no dirty classes. | `hyperbooks/HB-VC-s2-change/book.md:188` | `tests/test_change.py:153` |
| V082 | C-S2-FROZEN: the previously active patch becomes FROZEN. | `hyperbooks/HB-VC-s2-change/book.md:189` | `tests/test_change.py:121` |
| V083 | C-S2-FROZEN: the static-HUD fixture remains UNCHANGED. | `hyperbooks/HB-VC-s2-change/book.md:189` | `tests/test_change.py:122` |
| V084 | C-S2-FROZEN: the insufficient-coverage fixture never reaches FROZEN. | `hyperbooks/HB-VC-s2-change/book.md:189` | `tests/test_change.py:134` |
| V085 | C-S2-DET, scoped to the cited fixture: repeated output tile bytes match. | `hyperbooks/HB-VC-s2-change/book.md:186` | `tests/test_change.py:176` |
| V086 | The cited input-immutability fixture leaves descriptor bytes unchanged. | `hyperbooks/HB-VC-s2-change/book.md:49` | `tests/test_change.py:182` |
| V087 | Inline anchor vc.s2.drift resolves to the named implementation. | `hyperbooks/HB-VC-s2-change/book.md:31` | `visioncore/change.py:42` |
| V088 | Inline anchor vc.s2.residual resolves to the named implementation. | `hyperbooks/HB-VC-s2-change/book.md:31` | `visioncore/change.py:54` |
| V089 | Inline anchor vc.s2.sig_conf resolves to the named implementation. | `hyperbooks/HB-VC-s2-change/book.md:31` | `visioncore/change.py:67` |
| V090 | Inline anchor vc.s2.motion resolves to the named implementation. | `hyperbooks/HB-VC-s2-change/book.md:31` | `visioncore/change.py:179` |
| V091 | Inline anchor vc.s2.island_vector resolves to the named implementation. | `hyperbooks/HB-VC-s2-change/book.md:31` | `visioncore/change.py:294` |
| V092 | Inline anchor vc.s2.classify resolves to the named implementation. | `hyperbooks/HB-VC-s2-change/book.md:31` | `visioncore/change.py:303` |
| V093 | Inline anchor vc.s2.classify_first resolves to the named implementation. | `hyperbooks/HB-VC-s2-change/book.md:31` | `visioncore/change.py:490` |
| V094 | The pipeline classifies before filling xh64. | `hyperbooks/HB-VC-s2-change/book.md:177` | `visioncore/pipeline.py:223` |
| V095 | Lazy xh64 hashes dirty classes and copies the previous label for other tiles. | `hyperbooks/HB-VC-s2-change/book.md:177` | `visioncore/desc.py:123` |
| V096 | The RT0D02 receipt binds its new source digest to the current implementation. | `hyperbooks/HB-VC-s2-change/book.md:340` | `eval/results/rt0d02/EQUIV_RECEIPT.json:6` |
| V097 | The selected calibration record contains 1020 X4 runs and zero false-positive runs; these are recorded results, not new trials. | `hyperbooks/HB-VC-s2-change/book.md:115` | `tests/golden/nr_sigdead_fit.json:134`; `tests/golden/nr_sigdead_fit.json:125`; `tests/golden/nr_sigdead_fit.json:127`; `tests/golden/nr_sigdead_fit.json:136` |

