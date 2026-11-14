# contracts/cubook: conformance contract of the four srf-cu-* CLIs

Black-box contract for `srf-cu-exec`, `srf-cu-mcp`, `srf-cu-snap`, `srf-cu-wlcap` (code is truth: the contract was recorded
from the Go binaries at 07d789b; a mismatch is a finding, not a code fix). Any implementation can be driven:

    python3 -B contracts/cubook/run_conformance.py --bin-dir DIR      # rc 0 pass, 5 fail/missing binary, 2 usage; never 1
    python3 -B -m unittest discover -s contracts/cubook/tests -t .    # schemas closed+in sync, fixtures, mutant kill 100%
    python3 -B contracts/cubook/make_schemas.py [--check]             # regenerate/check schemas (strict() = hbtools rule)
    python3 -B contracts/cubook/record_fixtures.py --bin-dir DIR      # rebuild fixtures (case table = the inventory)

`CUBOOK_WORK` moves the scratch dir (default `build/cubook_conf`, must not be RAM-backed: srf-cu-snap refuses it).
Frames only touch `/dev/shm/vqa-ramws/opensuperficies/os-cu-0/conf-<pid>-*`, created and removed by the runner.

## Schemas (`schemas/<id with / -> ->.schema.json`, JSON Schema 2020-12, `additionalProperties:false`)
Wire ids: vqa.action/v1, vqa.act-plan/v1, vqa.act-step/v1 (src/cubook/action/types.go), vqa.frame/v1 and
os.cu.capture-receipt/v1 (capture/schema.go), os.cu.prereg-wl/v2|v3 and os.cu.wl-act-step/v1 (exec/wlplan.go),
os.cu.ast/v1 (mcp/astgate.go). Plus os.cu.rc/v1 (enum 0,2..7), os.cu.mcp-tools/v1 (tools/list result, 10 tools) and the
conformance-only os.cu.mcp-wire/v1 (JSON-RPC response line). prereg-wl leaves top-level/item keys open (`x-open`), as the Go decoder
does; only `steps` is closed. Not covered: os.cu.wlcap-start/v1, wlcap-bench/v1, wlcap-receipt/v1, chunk-ready/v1, mcp-registry/v1,
mcp-fake/v1, vqa.capsule/v1 (internal files, no CLI stdout contract; see REPORT_K).

## Fixtures (`fixtures/<cli>/<case>/`)
`argv.json` (`{WORK}` cwd, `{RAM}` managed ramws dir), `env.json` (`set`/`unset`; OS_CU_TEST is unset by default),
`stdin`, `expected_stdout` (byte-identical), `expected_rc`, `files/`, `expected_files/`, `meta.json`
(`ram` managed|unmanaged, `volatile`, `stderr_contains`, `stdout_schema`, `expect_files[{path,bytes,schema,jsonl}]`).
Hermetic only: `OS_CU_TEST=1`, `--mock`, `--fake-fb`, `--mock-gui`, `--fake`. Today no stdout needs normalisation
(`volatile` is used only by wlcap/bench_ok); if a fixture declares it, keys must be in the closed list ts_ns, ts_ms, started_ns, ended_ns, elapsed_ms,
dur_ms, pid, tmp_dir, work_dir, out_dir, run_id, span_ms, longest_run_ms, segment_ms (wlcap bench pacing, wall-clock) and each must be present pre-strip. Time-dependent files (`t_us`) are
schema-validated, not byte-compared. srf-cu-mcp has no process rc 5: gate refusals are in-band tool results.

## Mutants (`mutants/mNN_*.py`)
14 shims wrap the real binary (`CUBOOK_REAL_BIN`) and corrupt one behaviour (rc 1, extra field, rc 0 on gate, dropped trailing
check, roi "target2", test-only flag without OS_CU_TEST, dropped tool, frames cap, newline, ...). The test table in
`tests/test_conformance.py` names the fixture that must kill each; the suite must return rc 5 for every one.
