# Proposed Local Training-Signal Carrier Contract (`contracts/w8evidence/training`)

## Scope and Non-Claims

This package defines a proposed local, text-only training-signal carrier (`w8-training-signal-v1`) and a pure Python stdlib validator (`validator.py`) with `pytest` unit tests (`test_training_validator.py`).

- **Not a JSON Schema file** and **does not modify `contracts/worker/`**.
- **Proposed local contract only**: this package does not claim or invent any absent `wender` API, live framebuffer capture, or model learning result.
- **Passive data only**: captured text is treated strictly as passive data, never orchestration or executable instructions.
- **RAM-only frame boundary**: raw frames remain in bounded RAM outside this validator (`frame_metadata.ram_only == True`); only 64-character lowercase hex SHA-256 digests (or `GRAB_UNAVAILABLE` for a dummy renderer) may appear in the carrier.
- **Zero secret/environment access**: no credentials, tokens, keys, environment dumps, or process command lines are read or accepted.

## Restated Binding

1. **Frozen Provenance & Versions**:
   - `versions`: closed object freezing `repository_commit` (40 lowercase hex characters, CITED), `engine_version`, `source_version`, and `tool_version`.
   - Execution coordinates: integer `seed`, `requested_ticks`, `actual_ticks` (`0 <= actual_ticks <= requested_ticks`), `target`, `tab_id`, `session_id`, and `segment_id`.
   - `acquisition_provenance`: closed object freezing `source`, `source_qualification`, `capture_retention`, `renderer_mode`, and `byte_bounds`.
2. **Canonical Integer-JSON `state` & Explicit Gameplay Hash Mapping**:
   - `state` is encoded and verified using strict canonical integer-JSON rules (CITED from `src/workerbridge/json.go`): UTF-8 without BOM, no lone surrogates, no floats, no `-0` or non-finite numbers, no duplicate object keys, sorted keys, compact `,` / `:` separators, and max nesting depth `128` (CITED).
   - `gameplay_state_hash` maps explicitly to `state_sha256` (`gameplay_state_hash == state_sha256 == sha256(canonical_encode(state))`).
3. **Distinct AX Digests**:
   - `ax_sha256` and `ax_stable_sha256` are required 64-hex SHA-256 digests and must be distinct (`ax_sha256 != ax_stable_sha256`).
   - Gameplay hashes (`state_sha256`, `gameplay_state_hash`) and AX hashes (`ax_sha256`, `ax_stable_sha256`) must be distinct.
4. **UI-Tree Diff Bound by `diff_sha256`**:
   - `ui_tree_diff` is a closed, text-only canonical integer-JSON diff summary bound to `(session_id, segment_id, tab_id, target)` and verified against `diff_sha256`.
   - Raw HTML or raw AX trees destined for a model are rejected.
5. **Validated T2 Trace Bytes**:
   - `t2_trace` is bound by `t2_trace_sha256` and contains closed trace events scoped to `(session_id, segment_id, tab_id, target)` with monotonic `tick`, strictly monotonic `seq`, explicit `admission_verdict` (`ADMITTED`, `DENIED`, `DEFERRED`), and explicit `executor_verdict` (`APPLIED`, `REFUSED`, `NOOP`, `ABORTED`).
6. **Frame Metadata Referenced by SHA Only & Dummy Renderer Rule**:
   - `frame_metadata` references frames by 64-hex SHA-256 only (`status: "REFERENCED"`) with `ram_only: true`.
   - A dummy renderer (`renderer_mode: "dummy"`) must report `status: "GRAB_UNAVAILABLE"`, `frame_sha256: null`, and trace `frame_ref: "GRAB_UNAVAILABLE"` — never claiming a frame.
7. **`AUSENTE` Blocks Promotion**:
   - Missing or `"AUSENTE"` values for `capture_retention`, `byte_bounds`, or `source_qualification` are recorded as `AUSENTE` and block promotion (exit code `5`).

## CLI Exit Code Contract (CITED)

From `docs/design/W8_WORKER_BANK.md` §6 and `src/workerbridge/json.go:35-45`:

| Exit Code | Provenance | Meaning |
|---:|---|---|
| `0` | CITED | Verified success |
| `2` | CITED | Invalid input, CLI usage, or schema/version shape |
| `3` | CITED | Execution or setup failure (e.g. unreadable file or internal setup fault) |
| `4` | CITED | Unavailable prerequisite (e.g. missing supply port or unresolvable byte bundle) |
| `5` | CITED | Gate, validation, policy, or containment refusal (including `AUSENTE` blocking promotion) |
| `6` | CITED | Indeterminate quarantine (e.g. mutated replay or corrupted/mismatched supplied bytes) |
| `7` | CITED | Operator authorization required (`operator_override`) |
| `1` | CITED | Never returned for success or refusal |

All quantitative settings in gate command literals (such as `taskset -c 10-14` and the `100` TARGET byte ceiling on `AF_UNIX` socket paths under `/tmp/w8*`) are `TARGET`; protocol constants from the bound sources are `CITED`; unknown facts are `AUSENTE`.
