# Asset Factory

SPDX-License-Identifier: AGPL-3.0-only. See LICENSE and NOTICE.

F0-skeleton checks contracts, licence policy, scheduler, cache, receipt bridge,
documentation budget and this gate. Its PASS establishes skeleton checks only.
Model inference, appearance, Blender and production acceptance need their own evidence.
Each user's config requires `commercial_use`, with no default. Provider rights
are checked separately. Hunyuan3D 2.1 is manifest-only; it is not built or run.

Run the same whole gate locally and in CI from the component root. The caller
must supply an existing output directory; keep reports outside the component.

```sh
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
  env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. timeout 300 \
  python3 -B -m tools.gates --all --root . --report /tmp/af-report.json --receipt /tmp/af-receipt.json
```

`python3 -B -m tools.gates --self-test` exercises planted failure and zero-check
controls. `python3 -B -m tools.docs_ratio --root .` inventories all disk files.
Markdown physical lines must fit half the `.py`, `.sh`, `*.schema.json` lines.
Blank and unterminated lines count; arbitrary JSON adds no code credit. Internal
symlink aliases add no credit; escaping symlinks and binary text padding are errors.
Structurally valid PNG references are hashed with zero line credit.

Missing legs, skipped checks and empty coverage prevent PASS. The gate translates
unittest failures to stage failure; it never returns stage rc 1. Every child has
a deadline and an owned process group. Refusals print JSON and preserve caller
outputs. A successful report has a hash-bound receipt published last. Readers
must verify that hash and all bound files; a crash can leave a mismatched pair.
