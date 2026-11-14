# W8 Muse task source

This package prepares **data**, not an execution request with ambient authority.
The existing operator Muse queue uses one job ID per line. This W8-specific
source uses a different explicit transport, one line per registered job:

    task-id<TAB>/absolute/clean/brief.md<TAB>/absolute/clean/output.json

Every line has exactly three fields; the last field identifies the job's
intended output and is frozen in the manifest, **not** written by this tool.
Brief text is passive objective data, never instructions to the source.
The source makes no model request, starts no runner, does not execute gates,
and does not grant or fabricate bridge registrations.

## Inputs and outputs

Install/build the command with the repository's normal Go tooling, then
supply the required operator-controlled bindings. Example (substitute a real
forty-character integration SHA and already populated bridge directory):

    srf-w8-muse-source \
      --queue /operator/w8/queue.tsv \
      --bridge-dir /operator/campaign/bridge \
      --repository oguirado/OpenSuperficies \
      --base-commit 0123456789abcdef0123456789abcdef01234567 \
      --attempt a1 \
      --gate-argv-json '[["go","test","./..."]]' \
      --resource-policy-json '{"inference":"host","max_cpu_ms":900000,"max_memory_bytes":268435456,"max_wall_ms":900000}' \
      --obligations-json '["respect the registered write paths","retain verification evidence"]' \
      --max-in-flight 6 --in-flight 0 --seat-ready \
      --wave-out /operator/w8/pending-wave.json \
      --manifest-out /operator/w8/pending-wave.manifest.json

Seat readiness is **not** guessed from task prose or from a model. The
operator/seat controller must independently verify the seat and open-wave
state before setting --seat-ready. When unverified, the default fails closed.
A refused/unavailable seat, an open wave (--wave-open), zero free admission
credits or an existing wave/manifest gives EN_DUDA (exit 6). There is no
fallback to directly launching a runner. Keep both destination paths stable;
the manifest and wave must be reconciled and retired by the operator before
another issuance. Repointing output paths does not constitute reconciliation.

The wave file contains exactly the JSON array expected by srf-w8-muse:
task_id, attempt and objective per item; it never adds other fields.
The source emits at most the available credits (default limit **6 TARGET**)
and never more than 64 items or w8muse.MaxHeader bytes.

Each emitted manifest entry freezes the repository, base commit, task and
attempt IDs, absolute brief/output paths, the **exact brief text**, SHA-256
of brief bytes, ordered gate argv arrays, resource policy and obligations.
It includes SHA-256 of each frozen field and the generated wave. The entire
queue must validate, even if admission credits select only a prefix.
Generated briefs and gates are not executed or interpreted here.

The source reads the existing builder task and expected record in the bridge,
validates the frozen bridge task definition, verifies the repository/source
commit binding and checks for an active digest-bound lease in the ledger.
Absent or invalid registration returns exit 4. It does not alter that bridge.

## Bounds and failure classes

- Queue: at most 256 KiB and 64 newline-separated records; exact TAB shape,
  canonical absolute paths, unique lowercase/digit/single-dash task IDs.
- Brief: regular, non-symlink, UTF-8, nonempty text, at most 64 KiB.
- Wave: 1..64 items, strict known JSON fields only; attempt a followed by
  digits, and length at most 1 MiB (the driver's MaxHeader).
- Gate argv: 1..32 lists, each with 1..64 arguments; UTF-8 and byte bounds.
  Resource policy requires positive CPU, memory and wall-time limits and
  inference=host. Obligations: 1..32 bounded, nonempty strings.
- Invalid input gives exit 2, no returned/generated wave. Missing registered
  builder prerequisites give 4. Indeterminate admission or output publication
  gives 6 and requires reconciliation. No exit 1 is produced.

The command uses exclusive creates and writes the manifest first, the wave
last. It does not remove a successful manifest or wave: collection/retirement
is the operator's responsibility. It never calls the seat or the bridge
journal-creation API, performs network requests, or invokes an OS process.

Tests are in source_test.go; fixture cases are in
testdata/w8wave/musesource/cases.json. An injected read-only registration
check is used *only inside the package's tests* to exercise deterministic
wave generation; the exported Build always reads and validates the bridge.
