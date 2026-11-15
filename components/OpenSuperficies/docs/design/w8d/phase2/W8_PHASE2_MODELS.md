# W8 Phase 2 Sealed-Fix Models and Documentation (W8_PHASE2_MODELS)

## 1. Overview and Design Scope
This document specifies the sealed-fix executable models and doc bindings for W8 Phase 2 (W8-P2-01), incorporating design guidelines from `W8_PHASE2_DESIGN_v3.1` (section S7 sealed-fix register, Appendix judge adoption order row W8-P2-01) and judge findings `JP2-01`, `JP2-05`, `JP2-07`, `JP2-09`.

All measurements, limits, and numbers in this document are explicitly tagged with `[MEASURED]`, `[CITED]`, `[TARGET]`, or `[AUSENTE]` in compliance with W8 documentation directives. Timing annotations are labeled `VM` (e.g., `1.30s` `VM`).

---

## 2. Sealed Fix Pinning

### JP2-01: Extract Admission Dispatch & Base64 Run Refusal
Extract admission dispatches strictly by file extension suffix:
- **`.json`**: Validated using `artifact_ok` (persistent projection object).
- **`.md`, `.txt`**: Must be strict UTF-8 with no NUL (`0x00`) byte `[CITED]`. In addition, `report.md` must contain a 64-hex SHA-256 digest line `[CITED]`.
- **`.patch`, `.diff`**: Must be strict UTF-8 with no NUL byte `[CITED]`, and contain mandatory unified diff headers: `--- `, `+++ `, and `@@` `[CITED]`.
- **Base64 Run Refusal**: Any whitespace-free run of at least `1024` `[CITED]` characters from the base64 alphabet (`A-Z`, `a-z`, `0-9`, `+`, `/`, `=`) is refused across all text extraction formats.

#### Live Go Extractor (`src/w8seat/extract.go` & `src/w8seat/patchpolicy/patch.go`) Analysis
Comparison of the live Go B3 extractor (`textCheck` and `AdmitExtract`) with the JP2-01 specification:

| Feature / Check | JP2-01 Model Rule | Live Go Extractor Implementation | Stricter / Looser | Code Citation |
| :--- | :--- | :--- | :--- | :--- |
| File Suffix Dispatch | Dispatches `.json`, `.md`, `.txt`, `.patch`, `.diff` | Dispatches `.json`, `.patch`, `.diff`, `.md`, `.txt` | Equivalent | `src/w8seat/extract.go:32-82` |
| UTF-8 & NUL byte | Rejects invalid UTF-8 and NUL bytes | Rejects non-UTF-8 (`!utf8.Valid`) and NUL bytes (`bytes.IndexByte(b, 0) >= 0`) | Equivalent | `src/w8seat/extract.go:24` |
| Encoded / Base64 Run | Refuses runs of `>= 1024` `[CITED]` base64 chars | Refuses `hexRun` (`>= 129` hex) and `encodedRun` (`>= 100` base64 chars unless lowercase hex) | **Go is Stricter** (`100` vs `1024` chars) | `src/w8seat/extract.go:18-19,24-30` |
| `.md` / `.txt` Printability | Requires UTF-8 and no NUL | Restricts chars to `unicode.IsPrint` plus `\n`, `\r`, `\t` | **Go is Stricter** (printability check) | `src/w8seat/extract.go:76-80` |
| `report.md` Digest | Mandatory 64-hex SHA-256 digest line | Does not specifically check for SHA-256 digest in `report.md` | **Go is Looser** | `src/w8seat/extract.go:75-81` |
| `.patch` / `.diff` Headers | Requires `--- `, `+++ `, and `@@` | Enforces full git unified diff parser (`diff --git`, `--- a/`, `+++ b/`, `@@`) | **Go is Stricter** | `src/w8seat/patchpolicy/patch.go:44-162` |
| `.json` Validation | Validates via `artifact_ok` | Decodes JSON and recurses through strings checking `hexRun`/`encodedRun` | **Go is Stricter** on string encoding | `src/w8seat/extract.go:33-70` |

---

### JP2-05: Scheduler Lease & State Transitions
- **`BROWSER_READY` Refusal**: Validating a `scheduler_lease` carrying state `BROWSER_READY` is refused.
- **Browser Path**: Browser lifecycle is represented via `ADMITTED`, `STARTING`, `RUNNING`, with tab readiness handled purely as an observations field.
- **`ADMITTED` to `REFUSED`**: Legal only when `exec_started` is `false` `[CITED]` and failure reason is `containment` or `unavailable` `[CITED]`.

---

### JP2-07: Body Storage, Resolution, and Janitor Reaping
- **`put_body` / `resolve_body`**:
  - Accepts `artifact_ok` OR `payload_ok`.
  - Requires canonical UTF-8 bytes (`canonical_bytes`).
  - Refuses any whitespace-free base64 run of `>= 1024` `[CITED]` characters in body.
  - Bodies are addressed by SHA-256 digest (`3f706e236537d8a4369e160a28f73e5a40b9104085422d715a316b241320ef22` `[MEASURED]`), and the digest is re-verified upon `resolve_body`.
- **P2-05 Wave Janitor**: Reaps `run_dir/bodies` files only for references acknowledged by the aggregator; unacknowledged bodies survive.

---

### JP2-09: Runtime Object Definition & Fold-in Matrix
- **Runtime Object**: Any runtime object carrying `image_tag` or `env_image` is refused by the closed runtime definition.
- **Fold-in Matrix**: Drops all C1 per-commit image premises, `image_tag`, and `env_image`. Only C1 microVM rootfs and nesting feed P2-08.
- **S3 Results**: C1 to C3 S3 synthesis results are marked `[AUSENTE]`.

---

## 3. Disjoint Write-Paths Table

The write paths for Phase 2 cards are pairwise disjoint (except sequential reuse of 02a with 02b and 03a with 03b), and disjoint from B1, B2, B3, RAMCAP, and AGENTS.md paths:

| Card ID | Designated Write Surface Paths |
| :--- | :--- |
| **P2-01** | `docs/design/w8d/phase2`, `contracts/worker/tests/test_w8_phase2_models.py` |
| **P2-02a** | `src/orchestratorbook/arrendador` |
| **P2-02b** | `src/orchestratorbook/arrendador` *(Sequential reuse with P2-02a)* |
| **P2-03a** | `src/orchestratorbook/gobernador` |
| **P2-03b** | `src/orchestratorbook/gobernador` *(Sequential reuse with P2-03a)* |
| **P2-04** | `DROP` (`src/w8wave/runner` must NOT exist) |
| **P2-05** | `src/w8wave/supervisor`, `src/orchestratorbook/wal/w8_desenlace.go` |
| **P2-06** | `src/w8wave/switchboard` |
| **P2-07** | `src/w8wave/aggregator` |
| **P2-08** | `src/w8wave/microvm` |
| **P2-09** | `src/w8wave/ui` |
| **P2-10** | `src/w8wave/memory` |
| **P2-11** | `src/w8wave/pilot`, `scripts/w8/p2_pilot.sh` |

---

## 4. Test Suite Controls and Results

The executable test model suite `contracts/worker/tests/test_w8_phase2_models.py` contains:
1. **Positive Controls**:
   - `fix.patch` passes.
   - `report.md` with a 64-hex SHA-256 digest passes.
   - Speech act payload round-trips through `put_body` and `resolve_body`.
2. **Must-FAIL Controls**:
   - Old all-JSON extract rule refusing `report.md`.
   - A `BROWSER_READY` lease.
   - A runtime carrying `image_tag`.
   - An overlapping write path.
   - A body stored without acknowledgement being reaped.
   - A `1024`-character base64 run in `frame.txt` or body.
3. **Directory Scan Test**: Scan test over `src/w8wave` passes when the directory is absent.
