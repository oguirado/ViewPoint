# W8 host seat

The executable runs as the dedicated `osworker` host account. It exposes only
submit/wait/receipt/ack/cancel/health on the fixed Unix socket. No gate runs on
the host, no build API exists, and no bridge outbox or push is written here.

Wire: big-endian uint32 JSON-header length, strict JSON, then the declared bundle
and patch bytes for submit. See Request/Response in protocol.go. Handles are
SHA256 of the worker-canonical `[task_id, attempt_id, lease_id]`. Peer UID owns the
handle; active verbs consult arrendador, while completed receipts remain readable
by that owner after revocation. A repeated submit compares the task, observed tip
and both stream hashes before returning the existing handle/receipt. It never
renews the lease or launches again. The queued/live payload RAM budget can refuse
an otherwise valid bundle with unavailable; it never spools a bundle to disk.

The conductor supplies a merged-commit `go build -trimpath` binary and its hash
to operator setup's `--seat-bin` and `--seat-sha256`. Setup installs the binary
root:root 0555 [CITED: osworker-setup.sh], the embedded-source seccomp profile root:root 0444 [CITED: osworker-setup.sh], the operator
CPU partition and the user service. osworker joins admin solely to chgrp its
seat endpoint; this does not grant admin access to the Docker socket directory.
Installation atomically replaces the verified executable. Operator setup refreshes
the dedicated user manager when adding its admin socket group, and restarts the
seat after installation; run that maintenance while the worker bank is idle.
Rollback removes the socket before the runtime directory. Kill off stops the
seat before the user daemon. No kill script writes a worker receipt: a driver
seat_lost record is local, and startup reconciliation writes the authoritative
indeterminate receipt. Shutdown preserves incomplete attempt rows for that pass.

Startup requires the existing builder `base_image_records.jsonl` and matching
root-owned builder approval. `W8_CACHE_MANIFEST` (or `--cache-manifest`) names the
warm-job manifest `{volume:"os_worker_cache",manifest_sha256:<measured>,read_only:true}`.
The unit uses `%h/.local/state/w8/cache-manifest.json`. Creation of this measured
warm-job receipt is a conductor dependency; no hash is invented on its absence.

C1 accepts the image's literal recipe PATH as an image constant; every other
name remains in the frozen task enum with a fixed seat value. PATH never enters
the task env_names or launch overrides. Approved runner pins come from the
conductor's reproducible build. The runner samples workspace statfs used bytes
at ingest start, after clone, after gate and at extract, retaining the maximum
across verb processes in tmpfs. The seat re-reads status after extract and refuses
missing, malformed, decreasing or over-limit peaks. Host memory.peak/pids.peak
and measured combined-ring counters remain required; absence never becomes zero.
Text evidence requests require authored evidence.json inside
the capsule; source/file digests are measured by the runner and bound by the seat.

Until the external canon admits os-capsule, responses have bridge_result:null
and reason:unavailable. Muse independently validates and encodes receipts; only
its separately gated live publisher can publish after E3. Unit fixtures use fake
runtime observations, not host evidence. Live daemon, seccomp, image, cleanup and
kill-switch qualification has not been performed by this lane.
