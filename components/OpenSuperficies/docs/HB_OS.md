# HB-OS: hyperbook of the cubook code (W5a)

Book `hyperbooks/HB-OS-cubook/` (hb-hybrid/v1, id `HB-OSCUBOOK-001`) documents `src/cubook/**` and `cmd/srf-cu-*` for retrieval by agents. Code is truth: a doc/code mismatch is a finding, never a code fix. The pinned engine tooling `hyperbooks/_tools/hbtools_v2/` and `hyperbooks/COMPANION_FORMAT.json` are read-only copies (vision-engine `681dc0b`, language-neutral; `hyperbooks/PROVENANCE.json` keeps the `e738181` pin as history); format gaps go to `hyperbooks/_tools/os/REQUEST_VQA0.md`.

## Layout
| path | role |
|---|---|
| `hyperbooks/HB-OS-cubook/` | book.md (s0..s7 authored, s8..s10 `HB:GEN` fences), mathlinks, manifest, aliases, symbols, anchors, graph, nodes, claims, `index/` |
| `hyperbooks/_tools/os/goextract/` | Go `go/ast` extractor, emits `hb.symbols.bundle/v1` (`sha256_ast` of printed decl, comments stripped) |
| `hyperbooks/_tools/os/hb_os_driver.py` | runs hbtools_v2 (`681dc0b`) with `--symbols-bundle` / `HBV2_SYMBOLS_BUNDLE` (goextract bundle, lang `go`, `source_b64` added, `HB_ID` passed as `hb_id`, validated against `hbtools_v2/schema/hb.symbols.bundle.v1.json`); no monkeypatch (`install()` is a no-op kept for `measure.sh`); the pin sub-anchors only `lang python` symbols (`hb_build.py:149`) |
| `hyperbooks/_tools/os/hb_os_gates.sh` | gate entry; `hb_os_checks.py` = OS gates; `admission_os.py` + `OS_FORMAT.json` = admission overlay |
| `hyperbooks/_tools/os/tests/` | gate, mutant and INT tests (synthetic Go fixture) |
| `contracts/cubook/` | JSON schemas, fixtures, `run_conformance.py`, mutant shims (see its README.md) |
| `eval/hb_os/` | blind query sets (DEV 60, TEST 100), `PREREG.md`, `measure.sh`, `MEASURE.md`, `runs/` |

## Rebuild and gate
```
taskset -c 10-14 bash hyperbooks/_tools/os/hb_os_gates.sh HB-OS-cubook [outdir]   # extract, build, index, live + OS gates, admission; rc 0|2|3|5
taskset -c 10-14 python3 -B -m unittest discover -s hyperbooks/_tools/os/tests -t .
```
The gate no longer self-heals (JW5A-01): before extract it compares the committed `manifest.json` `files["book.md"]` sha with `book.md` and exits rc 5 `E_BOOK_DRIFT` on a mismatch, and it hashes the whole book dir around build+index and exits rc 5 if the build would change it, so a book-only prose edit and a code-only edit both fail until the book is rebuilt on purpose. `scripts/cu/gates.sh` adds `hb-os-cubook-clean` (`git diff --exit-code -- hyperbooks/HB-OS-cubook`) after `hb-os-cubook`. A cited line, `sha_ast` or file hash that disagrees with live code is rc 5 (`g_os_anchor`, `g_os_drift`, `g_os_rebuild`). 15 hbtools gates are skipped on purpose; each skip is listed with reason and replacement in `gates.json` `skipped[]` (Go has no `tools/`, `tests/` layout, no Python `ast`). `scripts/cu/gates.sh` runs `hb-os-tools`, `hb-os-cubook`, `hb-os-cubook-clean`, `hb-os-eval` (eval/hb_os unittests), `hb-os-leak` (`leak_check.py`) and `contracts-cubook`; an absent `manifest.json` is rc 5 `E_BOOK_DRIFT` (RD5A-01); `go-vet` covers `./src/cubook/...` and the five `./cmd/srf-cu-*` CLIs, Go gates use `-p 5`. Panel 3 is checked per id: `test_hb_os_int.Panel3Oracles` requires book.md to cite every `contracts/cubook/schemas/*.schema.json` path and `$id`, plus `run_conformance.py` and `tests/test_conformance.py` (no count floor).

## Contracts
`contracts/cubook`: 15 schemas, closed except the `x-open` prereg-wl top level and item keys (`vqa.action|act-plan|act-step/v1`, `vqa.frame/v1`, `os.cu.capture-receipt/v1`, `os.cu.prereg-wl/v2|v3`, `os.cu.wl-act-step/v1`, `os.cu.ast/v1`, `os.cu.rc/v1`, `os.cu.mcp-tools/v1`, `os.cu.mcp-wire/v1`, `os.cu.wlcap-bench|receipt|start/v1`), 40 hermetic fixtures, 14 mutant shims all killed. The book s7 Panel 3 points at `contracts/cubook/run_conformance.py` and `tests/test_conformance.py`. Check: `go build -p 5 -o build/contracts_bin/ ./cmd/srf-cu-{exec,mcp,snap,wlcap}` then `python3 -B contracts/cubook/run_conformance.py --bin-dir build/contracts_bin` (needs Python `jsonschema`).

## Eval protocol
`bash eval/hb_os/measure.sh <label> <book_dir>` (see `eval/hb_os/MEASURE.md`): same driver, params (`8120aab`), split dirs for `before` and `after`; DEV in-sample; TEST run once per label (`E_TEST_ONCE`). Primary endpoint: v2 reciprocal rank on TEST FIND, paired sign-flip, success p<0.05 and mean delta>0 (`eval/hb_os/PREREG.md`). `eval/hb_os/measure.sh` still calls `D.install()` and disables `hb_pack.subanchor` itself; do not change it (before/after parity; with the 681dc0b pin the disable is a no-op for Go). Replication labels: `before_681dc0b`, `after_681dc0b`.

## Parity notes (JW5A-06)
- `build_link_issues_test_files` is 11 in `before` and 0 in `after`: the skeleton book accepted 11 E_BOOK_LINK issues; the final book has none. The measured pair is pinned to book `5a186d9`, receipts `a6027ab` (`runs/run_{dev,test}_{before,after}.json` are never rewritten).
- `after` ran at `5a186d9` with driver `bb0d9423`; INT later changed the driver (`82b53cd` sha `363291ad`), so HEAD cannot re-derive the `after` index byte for byte without measure.sh's own subanchor disable. `measure.sh` now stamps `driver_head_sha256`. The FX1 book/extractor shas differ from the measured pin; replication under the `681dc0b` pin (both labels) is W5b.
- s4 states the `ajson.Open` code as `E_GATE` (the Go comment still says `E_RAM_LOCKED`; book follows code).
- Honest reading of the result: the pre-registered threshold was met narrowly (exact p = 23/512 = 0.0449; 10 wins / 2 losses; effect concentrated in 12/60 queries); it needs replication. Query authors never read `hyperbooks/` or `contracts/`; book writers never open `eval/hb_os/`.
