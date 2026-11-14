# W8 phase-one contract code

`BridgeToWorker` and `WorkerToBridge` validate the immutable projection and
preserve all bridge fields. `EncodeBridgeResult` validates task and receipt
structure/bindings, copies the nine PROTOCOL identifiers plus `role`, and
returns an explicit parity/live envelope. `OutboxBytes` serializes only admitted
live envelopes; it never writes a file.

The pinned v3 canon does not admit `os-capsule`. Live encoding is therefore
unavailable (normalized rc 4) until an independently reviewed contract update.
A non-OS task cannot be relabelled. Parity outbox attempts return rc 5.

`ValidateLaunchSpec` is pure: JSON validation plus memory/swap equality,
CPU-set disjointness, backend-specific devices, and a required workspace tmpfs.
The trusted interactive CPU set must be supplied, even when explicitly empty.
It invokes no runtime, shell, image builder, firewall, or filesystem writer.

`CheckResult` is an offline status/Reject-class parity model with explicit
snapshot inputs. It is **not** a lease service, admission endpoint, or production
verification boundary. Caller-supplied observations do not prove runtime facts.
Runtime grant validation, atomic fencing, and live containment belong to later
phases. A caller collecting results must carry the full receipt in the report
and verify actual source, artifact, and report bytes independently.

The embedded schema is an exact snapshot of
`contracts/worker/os.cu.worker-task.v1.schema.json`; a test detects drift.
Only its audited keyword subset is interpreted. Python assertions become
component checks for the few path/ref patterns whose lookahead RE2 cannot run.

`Canonical` implements worker binding bytes. `BridgeCanonical` uses the canon's
C0 escaping. Both refuse ambiguous integer-only JSON, invalid Unicode and
unknown object keys at schema boundaries. Errors contain class/reason, never
captured source bytes. There is no runtime output or frame persistence.

Fixtures are synthetic. Regenerate from the repository root:

```sh
python3 -B testdata/workerbridge/generate.py
taskset -c 10-14 go test ./src/workerbridge/...
```

The generator reads the canon tools next to `OS_CU_CANON_BRIDGE` (schema
directory), or the host default specified by the co-signed model. Go tests
consume only committed fixtures; Python and the host canon are not dependencies
of the Go package or its tests. Provenance pins the independent oracle sources.
