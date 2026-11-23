# Local asset publication and restore

Run from the asset-factory root with `python3 -B -m tools.publish`. Supply a factory configuration with `--config FILE` or the default factory configuration at `${XDG_CONFIG_HOME:-~/.config}/viewpoint/asset-factory/publish.json`. Set `destination_root` to `:local:/absolute/isolated/publish` and `source_root` to an absolute evidence directory. Named backends and legacy remote configuration refuse with `remote_disabled`. This publisher operates directly on local file descriptors and has no external transfer process.

`--where` prints the configured destination. `export --plan SOURCE.json --out PLAN.json` hashes the declared sources and their validated evidence. Ordinary releases go to `class/id/version/`. Review assets go to `_review/class/id/version/` and include a verdict marker. A release requires subject-bound gate receipts and a complete rights declaration. The broker supplies current rights adjudication.

Source filenames must have the extension admitted by their declared artifact path. The publisher admits `asset.glb` and GLB files under `meshes/`, PNG and EXR files under `textures/`, JSON under `receipts/` and `records/`, and `asset.blend`. The existing render, physics and CAD paths retain their individual extension restrictions. Export planning, approval replay and import inventory reject credential names, including case variants, alternate leading punctuation and backup spellings.

## Trusted host entry points

The embedding host authenticates each principal and constructs `NamespaceAuthority` from protected principal-to-destination grants. Pass the opaque session from `authority.session(principal)` to `publish`, `plan_import`, `restore` or `doctor`. The host supplies the approved local restore root when planning an import. Standalone CLI transfer commands require that trusted host integration and return `namespace_session_required` without it. Planning an export is available offline.

Each export also requires an `af.publish-approval/v1` whose `plan_sha256` is the canonical digest of the exact plan and whose `destination_root` matches the configured root. The approval must permit folder creation and metadata updates. The plan binds every asset version and file digest. A changed plan, wrong namespace or unsafe root refuses before publication.

## Immutable events and restore

Published files and `_estado/events/<event_sha256>.json` use atomic no-replace installation. An existing identical file is reusable after verification. A differing file is preserved and refused. Events use the SHA-256 of their exact canonical bytes as their filenames. Readers authenticate and merge the immutable events. `_index/catalog.json`, `_estado/log.jsonl` and the catalogue snapshots are derived views. The publisher verifies those views from the authorized root after replacement. Retry the approved plan after an interruption to reconcile them.

Each event retains `local_sha256`, `local_size` and `local_md5` from the submitted bytes. `remote_evidence` is the inherited contract field for observed destination size and MD5. In this component the destination is local. The publisher compares actual destination SHA-256 with the source independently. Restore also rehashes the files it admits.

`plan_import` binds the selected assets, their full file inventory and the event-derived catalogue digest. Restore revalidates that plan. It opens source and destination paths without following directory or file symlinks and rejects credential-named components before opening bytes. It writes into a private staging directory under the import store and verifies every staged asset before the first final move. Each move uses the original staging parent fd and an authorized destination parent fd. The publisher verifies the moved inode and its byte closure again, syncs the directories and only then counts verified files. Existing identical assets are rehashed and reused. Each version has its own atomic commit. A refused multi-version receipt retains the list of versions committed before the refusal.

The filesystem contract requires Linux directory-relative operations, `renameat2` with `RENAME_NOREPLACE`, `statx` with `STATX_MNT_ID`, and directory `fsync` support. The trusted embedding host chooses the roots. Lookups below each root stay on its mount, including import cleanup. Guard the store against arbitrary write access by the service's own OS identity. A detached stage or an entry with unproven recovery identity is preserved for inspection.

The systemd per-user runtime directory, normally `$XDG_RUNTIME_DIR`, owns `af-publish-locks/`. Its ceiling is `MAX_LOCK_FILES`, currently 1,024 entries [TARGET]. Existing lock inodes remain reusable at capacity. Admission of a new lock is serialized and refuses with `lock_capacity_exceeded` at the ceiling. Keep this directory intact while any publisher or restore can hold a lock. It can be cleared with those operations stopped, or reclaimed by systemd when the runtime directory is torn down.

`static_gate.scan_surface` checks the publisher's imports and static process capabilities. The focused suite checks its rejection of shell and expanded launch arguments and exercises a live argv-only call through the shared process helper.
