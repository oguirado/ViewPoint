# W8 synthetic manual first-task fixtures

**SYNTHETIC ONLY; NOT AUTHORITY.** These files never launch a task. `builder.py` uses Python stdlib to deterministically construct an offline first-task, closed Muse wave item, fake attempt/lease registration, pin identities, a schema-shaped **simulated** successful local receipt, unavailable publication projection and eight expected-refusal cases. Every digest and commit is synthetic; live approval, real registration, observed host metrics, authorization and publisher capability remain **AUSENTE**. The built JSON must never be submitted as a real grant.

The design follows `src/w8muse/README.md`, `src/w8muse/protocol.go`, `src/w8muse/registration.go`, `src/w8seat/receipt.go` and `docs/design/W8_WORKER_BANK.md` at `bridge/integration`. `wave_item` has exactly `task_id`, `attempt`, `objective` and optionally `patch_path`; no wave field carries launch authority. Objective text is passive data. The fake `Port` binds task identity, registered digest, fresh attempt/lease, read-only warm cache availability and seat connectivity. The local launch identity projection is **not** a seat `launch_spec` API and never becomes argv. The example gate is ordered Python-stdlib-only argv in `task.gate`; the builder never executes it.

The example runtime preserves the W8 worker profile, pinned source commit, `os-worker-base` digest/recipe/approval identity, runner/profile/cache identities, tmpfs workspace and `os_worker_cache` read-only manifest, host-only inference, gate observed status, `task_sha256`, receipt grant identity, source/output identity, extract text digest, simulated safe observations and verified purge. Quantities in gate/time/CPU/probe examples are **TARGET** simulations; W8 protocol and seat constants are **CITED** by the sources. Unknown real values are **AUSENTE**.

**Seat progression, documentation only:** submit; wait; authoritative receipt; stage extracted UTF-8 text and receipt durably with mode `0644`, sync and atomic rename; acknowledgement only after durable collection; verification of the receipt's existing cleanup/purge evidence. The seat performs its purge before the receipt; verification does not issue a new purge. Only `wait` and `receipt` transport reads permit bounded retries (three attempts **CITED** in Muse README). Seat loss means local `seat_lost` rc 6 and requires reconciliation; it is never turned into a worker receipt. All example operations are uninvoked.

**Publication:** the hypothetical simulated worker receipt carries rc 0, but dry Muse publication on `chatgpt-mcp` remains `bridge_result:null`, driver rc 4 `unavailable`, no outbox, no publisher, no os-capsule authorization. A simulated receipt is not accepted live evidence.

`cases.json` and the pytest tests assert **must-FAIL**: unknown launch key, bind request, env-file and runtime-socket requests => rc 5 containment; unknown wave key and unregistered task => rc 2 input; missing cache => rc 4 unavailable; seat down => rc 6 `EN_DUDA` without receipt. No unsafe case can be a successful first task. `Port` is in-memory; no real cache, socket, Docker, sudo, model, network, X11, process gate or filesystem capture is used.

**Exit-code contract (CITED):** 0 verified success; 2 invalid input; 3 execution/setup; 4 unavailable prerequisite; 5 gate/containment; 6 indeterminate/quarantine; 7 operator. **1 is never success or refusal.** The CLI returns 0 only after writing and reading back deterministic UTF-8 fixture JSON, 2 for parse/input errors and 3 for write/setup/unexpected errors; it cannot return host-side 4–7 because it does not drive a seat. The pytest process status is solely the test-runner exit status.

Run at repository root (pytest is the test runner, not a task dependency):

````sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m pytest -q tests/w8evidence/manualfixtures
````

Generate full text-only fixtures in a scratch directory:

````sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/w8evidence/manualfixtures/builder.py --output-dir /tmp/w8-synthetic-fixtures
````

Only `fixtures.json` is generated. No binaries, frames or credentials are read or written. No test or fixture claims conductor acceptance or real host proof.
