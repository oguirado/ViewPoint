# Pre-Registration: OpenSuperficies HyperBook Routing Evaluation (`eval/hb_os/`)

## 1. Scope and Provenance
- **Campaign / Lane**: `os-cu-0` Wave 5a, Room 1, Lane E (`eval/hb_os/`).
- **Author**: Gemini 4 Argon (medium), strictly blind to all HyperBook prose (`hyperbooks/**`), contracts (`contracts/**`), and documentation (`docs/**`).
- **Subject Commit**: `4cc6765b9c03351fa4775dce2446862313c634c2` (`tag: os-cu-w4` ancestor / HEAD of `OpenSuperficies`).
- **Target Surface**: `src/cubook/{action,ajson,capture,exec,mcp,prodgate,rc}` (40 non-test `.go` files, 272 callable symbols) and `cmd/srf-cu-{exec,mcp,snap,wlcap}` (5 non-test `.go` files including `cmd/srf-cu-exec/wl.go`, 35 callable symbols) [MEASURED].

## 2. Dataset Partition and Quotas
- **DEV (`eval/hb_os/dev/hb_dev.jsonl`)**: 60 rows total (`split: "DEV"`):
  - `FIND`: 30 rows (`1` gold `sid` each, proportional to non-test `.go` surface with `>= 2` rows per subpackage plus CLI coverage).
  - `ABSENT`: 22 rows (`gold: []`, plausible computer-use / systems capabilities absent from `OpenSuperficies`).
  - `CLAIM`: 8 rows (6 `supported`, 2 `contradicted`; `receipt` under `claims.jsonl:OS-C01..OS-C08`, `gold_line` verified at `4cc6765`).
- **Held-Out TEST (`eval/hb_os/heldout/hb_test.jsonl`)**: 100 rows total (`split: "TEST"`), sized for two-sided paired sign-flip permutation power (`2^60` exact space, sampled via `100,000` flips from `Random(0)`):
  - `FIND`: 60 rows (`1` gold `sid` each, `>= 2` rows per subpackage across all 7 `src/cubook/*` packages plus CLI `cmd/srf-cu-*/*.go::func` including `cmd/srf-cu-exec/wl.go`).
  - `ABSENT`: 40 rows (`gold: []`, plausible out-of-scope queries with 0 Go corpus token hits).
- **CLI Coverage**: 10 `FIND` rows total across splits (`3` in DEV, `7` in TEST) targeting `cmd/srf-cu-*/*.go::func`, including 3 rows covering `cmd/srf-cu-exec/wl.go` (`readStart`, `waitStart`, `runWL`) per AW5-07.

## 3. Row Schema (AW5-02)
Every JSONL line contains the exact 12-key schema required by `hb_eval.py` and `hb_gates2.py`:
```json
{
  "qid": "OS-D-F01",
  "split": "DEV",
  "class": "FIND",
  "text": "...",
  "intent": "find_symbol",
  "gold": ["src/cubook/capture/analyzer.go::GridFor"],
  "gold_tests": [],
  "gold_line": "src/cubook/capture/analyzer.go:L35-L46",
  "receipt": "",
  "verdict": "",
  "source_event": "git:4cc6765",
  "notes": "..."
}
```
- Pointer receivers are normalized to `Recv.Method` (never `(*Recv).Method`) to satisfy `D("sid")` (`^[A-Za-z0-9_./-]{1,160}::[A-Za-z_][A-Za-z0-9_.]{0,127}(#b[0-9]{1,3})?$`).

## 4. Blindness and Zero-Leakage Invariants
1. **Zero Identifier / Stem Overlap**: Every query `text` across `DEV` and `TEST` has `0` token overlap (`>= 4` chars, case-folded, plus camelCase and snake_case sub-tokens) against all `1,875` distinct Go AST identifiers and file stems across all `86` `.go` files in `src/cubook/**` and `cmd/srf-cu-*/**` [MEASURED].
2. **Zero Corpus Hits on ABSENT**: Every `>= 4`-char token in `ABSENT` query texts has `0` lexical matches anywhere in the `86` `.go` files [MEASURED].
3. **Strict Split Disjointness**: Zero `DEV` `qid`s appear in `TEST`, `split` fields match the enclosing file, and `.sha256` seals guard against tampering.

## 5. Primary & Secondary Endpoints and Statistical Decision Rule
- **Evaluated Systems**: `v2` (HyperBook-aware router) vs `B1` (plain BM25F baseline); `B0`/`B2` reported descriptively.
- **Metrics (`hb_eval.py`)**:
  - `FIND`: `recall@1`, `recall@5`, `MRR` (exact `Fraction`), and `nDCG@5` (`Decimal`).
  - `ABSENT`: abstention `precision`, `recall`, and `F1` (exact `Fraction`).
  - `CLAIM`: verdict accuracy (`DEV` only).
- **Primary Endpoint**: Per-query reciprocal rank (`RR`) of `v2` on held-out `TEST` `FIND` (`n = 60`), `before` (skeleton companion index with 0 prose / 0 `hb.inv.v2`) vs `after` (authored `HB-OS-cubook`), evaluated with identical frozen `params.json` (`8120aab`, `sha256: be9da324...`).
- **Statistical Test**: Two-sided paired sign-flip permutation test (`hb_eval.sign_flip_p`: exact `2^n` for `n <= 20` non-zero deltas, else `100,000` flips seeded from `Random(0)`).
- **Success Criterion**: `p < 0.05` AND `mean(ΔRR) > 0` on held-out `TEST` `FIND`, with secondary guardrail that `ABSENT` `F1` does not drop by more than `0.05`. Otherwise the mandatory claim wording is `"did not move"`.
- **Execution Discipline**: Zero retrieval or metric runs are performed on `TEST` during query authoring. `TEST` is run once in the `before` snapshot and once in the `after` snapshot under `--lock` after `hb_freeze.py --write`.
