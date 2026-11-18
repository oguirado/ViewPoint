# Asset Factory

SPDX-License-Identifier: AGPL-3.0-only. See LICENSE and NOTICE.

F0-skeleton checks contracts, provider identity, scheduler, cache, receipt bridge,
documentation budget and this gate. Its PASS establishes skeleton checks only.
Model inference, appearance, Blender and production acceptance need their own evidence.
ViewPoint ships the AGPL-3.0 Asset Factory slots, contracts and adapters. Users supply
their own local models and are responsible for their licences and use. Model licence
identifiers, URLs, jurisdiction and commercial use can be recorded as optional user
declarations. Qualification and execution use technical evidence such as byte pins,
offline operation, output contracts and resource limits. Shipped code, patches and
dependency locks must remain AGPL-compatible. The factory downloads no weights by default.

Qualification pins keep their path labels in the canonical record.
Relative paths resolve from the installed component. Set any external
roots used by the operation to absolute directories in the trusted launcher.

| Root | Contents |
|---|---|
| `AF_MODELS_ROOT` | Local model inventory, with a directory for each model. |
| `AF_CAMPAIGN_ROOT` | Campaign evidence, quote corpus, TRELLIS approval and shared execution lease. |
| `AF_RUNTIME_ROOT` | Pinned environments and freeze files. |
| `AF_CONSTRUCT_SOURCE_ROOT` | The Construct source and historical asset corpus. |
| `AF_F1U1_REPOS` | Upstream TRELLIS build repositories for the source-dependent tests. |
| `VPFIX_OWNER_TREE` | ViewPoint owner checkout for wire verification if its schemas are outside the current repository. |

A missing external root leaves its evidence unavailable. Retained source labels
identify the original corpus bytes by digest. For live TRELLIS admission the
trusted launcher must supply the campaign root and an approval for the current
provider, calibration and admission code.

Run the same whole gate locally and in CI from the component root. The caller
must supply an existing output directory; keep reports outside the component.

```sh
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
  taskset -c 0-15 nice -n 15 bash -c '
    exec 9</proc
    exec unshare -Urpf --mount-proc --kill-child env \
      AF_PROC_BACKEND=pidns AF_PROC_PIDNS_PROC=/proc/1/fd/9 \
      PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests CUDA_VISIBLE_DEVICES= \
      HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 timeout 300 \
      python3 -B -m tools.gates --all --root . \
        --report /tmp/af-report.json --receipt /tmp/af-receipt.json
  '
```

The test backend requires an explicit `AF_PROC_BACKEND=pidns`, a genuine outer
kernel procfs retained before `unshare` through `AF_PROC_PIDNS_PROC`, and
`PYTHONPATH=.:tests` from the component root. Under the same wrapper, replace the
gate command with `python3 -B -m unittest discover` for the full suite. Default
discovery and the gate explain a missing environment before loading test jobs.
Mapped root alone or a directory containing forged proc text proves nothing.
Production uses systemd services and never falls back after manager failure.

`python3 -B -m tools.gates --self-test` exercises planted failure and zero-check
controls. `python3 -B -m tools.docs_ratio --root .` inventories all disk files.
Markdown physical lines must fit half the `.py`, `.sh`, `*.schema.json` lines.
Blank and unterminated lines count; arbitrary JSON adds no code credit. Internal
symlink aliases add no credit; escaping symlinks and binary text padding are errors.
Structurally valid PNG references are hashed with zero line credit.

Missing legs, skipped checks and empty coverage prevent PASS. The gate translates
unittest failures to stage failure; it never returns stage rc 1. Every child has
a deadline and an owned job. Service success requires both a successful main
exit and systemd `Result=success`; `JobResult.reason` preserves typed failures.
Refusals print JSON and preserve caller
outputs. A successful report has a hash-bound receipt published last. Readers
must verify that hash and all bound files; a crash can leave a mismatched pair.

Process limits: PROC-B deliberate cgroup escape is OUT_OF_CONTRACT. Beside it,
P8-NAME external replacement by another controller under the identical unit name
is OUT_OF_CONTRACT. Names are locally generated from uuid4 (122 random bits
[CITED: UUID version/variant layout]); reuse requires deliberately creating that
same af-job-* name. Name syntax alone is not a manager invocation identity.
Pending starts have a creation record before the manager request; lost replies
trigger owned stop/reset-failed. Unretired transactions remain strongly retained
for `asset_factory.proc.reconcile_owned`; retirement releases that retention.
