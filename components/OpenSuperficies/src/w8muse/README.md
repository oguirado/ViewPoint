# Host Muse driver

`srf-w8-muse` is a seat-agnostic host driver. It consumes a bounded JSON array of
`{task_id, attempt, objective, patch_path?}` wave items. It does not invoke a model,
build an image, execute a task gate on the host, or acquire a Docker socket.

Use an installed conductor-built binary; this lane does not build or install it:

```sh
srf-w8-muse --dry --campaign-dir <scratch> --bridge-dir <campaign>/bridge \
  --repo-dir <read-only-mirror> --wave <registered-wave.json>
```

The bridge directory must contain the task, expected-evidence record and a
current matching `lease_granted` ledger event. Only registered builder tasks on
canon writer surfaces are accepted in dry mode. Live mode requires `os-capsule`
and the frozen bridge codec's live validation; it remains unavailable before E3.
The mirror's remote must match the dispatched GitHub repository. The observed
`bridge/integration` tip must contain the task pin in the local object database.
The tip is read with an unauthenticated `ls-remote` (`--tip-source remote`, public
repositories). A private repository needs `--tip-source mirror`: the conductor fetches
`+refs/heads/*:refs/heads/*` into the read-only mirror with its own credentials,
and the driver reads `refs/heads/bridge/integration` without network or credentials.
In both modes the bundle names that branch (git cannot bundle a bare commit, and a
clone from a bundle takes only refs/heads); the runner checks out the exact pin.
Git counts the bundle into a sink, then pipes a second pass directly to submit;
no host bundle file is written. Credentials are not forwarded to the capsule.

The wire format is a big-endian uint32 JSON length, JSON, then submit's bundle
and patch streams. The protocol is frozen with BL1. Handles are the lowercase
SHA256 of canonical JSON `[task_id,attempt_id,lease_id]`, persisted before submit.
Only `wait` and `receipt` retry transport errors, with three attempts per read.
No submit, patch application, acknowledgement or push is automatically retried.
The persistent attempt marker refuses another normal invocation for that attempt.

Collection writes receipt and extracted text into a staging directory on the
campaign filesystem, forces file mode 0644, syncs, and atomically renames before
acknowledgement. Dry results always have `bridge_result:null`, reason unavailable,
no outbox and no publisher call; their driver code is at least 4, preserving a
higher worker failure code. The worker receipt retains its actual exit code.

A lost seat produces a **local status** with code 6 and reason `seat_lost`, never
a fabricated worker receipt. After the seat restarts, use the same command with
`--reconcile` to fetch its authoritative restart receipt. This reuses the original
worker bytes and deterministic handle without running Muse or submit again.
The authoritative receipt can itself have code 6. An uncertain push is never
retried; a separate bridge attempt is required after operator reconciliation.

Publication uses the dispatched `writer_branch` (`bridge/<task>/<attempt>`),
registered outbox paths and expected-record PROMPT-ID nonce. Only a policy-checked
delta and the result/report pair are staged in a disposable clone at the pin.
The extracted report is preserved. Task gates never run in the publisher.

`b3_live_acceptance.sh --plan` documents the conductor-only checks, prerequisites,
inputs and the deliberate host-gate rerun deviation for auto_ingest. This lane
runs only fake unit tests and shell syntax checks. Production Docker, image pins,
kill-switch reconciliation and publication require independent conductor checks.
