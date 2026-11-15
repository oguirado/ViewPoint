# Quickstart

Run from the repo root (Linux, Go 1.x on PATH or `/usr/local/go/bin`, Python 3; no network, no docker).

```bash
taskset -c 10-14 bash scripts/cu/gates.sh      # wheel + unit/vision/Go suites + mutants; rc 0 ok, 2 usage, 5 failure
taskset -c 10-14 bash scripts/cu/run_suite.sh  # CU suite (skips the live-host packages); --plan lists the steps
make build                                     # go build ./cmd/... into bin/ (untracked)
```

`make test` runs `go test ./src/...` over the whole tree, including packages that start a host X server or move the pointer; use `scripts/cu/run_suite.sh` instead.
Both scripts honour `CU_CPUSET` (default `10-14`). Hyperbook gate: `bash hyperbooks/_tools/os/hb_os_gates.sh HB-OS-cubook`.
Web agents: see the "External web agents" section of `AGENTS.md`. More: `README.md`, `docs/CU_W4.md`, `docs/CU_KNOWN_LIMITATIONS.md`.
