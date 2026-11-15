# Vision Memory (`vmem/`) Specification

This specification documents the `vmem/` package as implemented in the bound source files at ref `bridge/integration`:
- `vmem/store.py` (lines `1–263`)
- `vmem/units.py` (lines `1–320`)
- `vmem/ingest.py` (lines `1–390`)
- `vmem/receipts.py` (lines `1–193`)
- `vmem/tiers.py` (lines `1–75`)
- `vmem/blobs.py` (lines `1–137`)
- `vmem/compact.py` (lines `1–130`)
- `vmem/router.py` (lines `1–453`)
- `vmem/gates.py` (lines `1–185`)
- `vmem/cli.py` (lines `1–112`)
- `AGENTS.md` (lines `1–57`)

Anything referenced by these files whose definition is outside the bound source files is marked `ABSENT`.

## 1. Package Architecture, Headers, and Global Rules

### 1.1 Layering, Ownership, and Import Boundaries
- **Owner and stage:** `VMEM` owns `vmem/`, which implements stage `S7` at layer `5` and links to HyperBook `HB-VC-s7-vmem` (`AGENTS.md:28`, `vmem/store.py:1`, `vmem/units.py:1`, `vmem/ingest.py:1`, `vmem/receipts.py:1`, `vmem/tiers.py:1`, `vmem/blobs.py:1`, `vmem/compact.py:1`, `vmem/router.py:1`, `vmem/gates.py:1`, `vmem/cli.py:1`).
- **Allowed imports:** `AGENTS.md:24` states `vmem -> visioncore.{ids,layout,errors,canon,schemas} + stdlib` (`visioncore` never imports `vmem` or `vadapt`, `AGENTS.md:23`). `vmem/tiers.py:11` notes that `vmem` may not import `visioncore.params` (`g_layer`).
- **Sealed tree:** `AGENTS.md:55` lists `vmem/` as a sealed tree (`gate g_sealed_untouched`, tag `ve-sealed-1`).
- **Git exclusions:** `AGENTS.md:43` forbids committing `build/`, `*.npy`, or `vmem` stores.
- **Global exit codes and stdout contract:** `AGENTS.md:37` defines exit codes `0` OK, `2` usage/schema, `3` reject, `4` stale, `5` gate, `6` budget, never `1`, with one canonical JSON object on stdout (`vmem/cli.py:2, 102–108`). `AGENTS.md:38` requires a refusal (`rc != 0`) to leave caller-owned outputs byte-identical by staging to `<out>.tmp` and calling `os.replace`.

### 1.2 Module Registry and Code Anchors

| File | `@module` ID | Layer | Stage | Owner | Declared `@module` Deps | Code Anchors (`# @anchor:`) |
|---|---|---|---|---|---|---|
| `vmem/store.py:1` | `vm.store` | `5` | `S7` | `VMEM` | `vc.canon`, `vc.errors`, `vm.blobs`, `vm.receipts` | `vm.store.Store` (`vmem/store.py:75`), `vm.store.commit` (`vmem/store.py:140`), `vm.store.rollback` (`vmem/store.py:201`) |
| `vmem/units.py:1` | `vm.units` | `5` | `S7` | `VMEM` | `vc.canon`, `vc.errors`, `vc.ids`, `vc.layout` | `vm.units.validate_unit` (`vmem/units.py:117`) |
| `vmem/ingest.py:1` | `vm.ingest` | `5` | `S7` | `VMEM` | `vc.canon`, `vc.errors`, `vm.blobs`, `vm.receipts`, `vm.store` | `vm.ingest.ingest` (`vmem/ingest.py:181`) |
| `vmem/receipts.py:1` | `vm.receipts` | `5` | `S7` | `VMEM` | `vc.canon`, `vc.errors`, `vm.blobs` | `vm.receipts.Journal` (`vmem/receipts.py:43`) |
| `vmem/tiers.py:1` | `vm.tiers` | `5` | `S7` | `VMEM` | `vc.errors` | `vm.tiers.fg_state` (`vmem/tiers.py:25`) |
| `vmem/blobs.py:1` | `vm.blobs` | `5` | `S7` | `VMEM` | `vc.canon`, `vc.errors` | `vm.blobs.put_blob` (`vmem/blobs.py:61`) |
| `vmem/compact.py:1` | `vm.compact` | `5` | `S7` | `VMEM` | `vc.canon`, `vc.errors`, `vc.ids`, `vm.tiers`, `vm.units` | `vm.compact.compact` (`vmem/compact.py:57`) |
| `vmem/router.py:1` | `vm.router` | `5` | `S7` | `VMEM` | `vc.canon`, `vc.errors`, `vm.blobs`, `vm.store` | `vm.router.sig_lookup` (`vmem/router.py:200`), `vm.router.route` (`vmem/router.py:257`), `vm.router.answer` (`vmem/router.py:419`) |
| `vmem/gates.py:1` | `vm.gates` | `5` | `S7` | `VMEM` | `vc.canon`, `vc.errors`, `vm.blobs`, `vm.store` | `vm.gates.verify` (`vmem/gates.py:149`) |
| `vmem/cli.py:1` | `vm.cli` | `5` | `S7` | `VMEM` | `vc.canon`, `vc.errors`, `vm.store` | `vm.cli.main` (`vmem/cli.py:99`) |

## 2. Store Layout and Generations (`vmem/store.py`, `vmem/ingest.py`)

### 2.1 On-Disk Directory Layout
Under a store root directory `root` (`str(root)`, created with `os.makedirs(self.root, exist_ok=True)` at `vmem/store.py:77, 80`):

| Relative Path | Producer / Consumer | Format & Purpose |
|---|---|---|
| `.lock` | `Store.lock` (`vmem/store.py:90–102`) | Lock file opened with `os.O_RDWR \| os.O_CREAT` mode `0o644` and locked via `fcntl.flock(fd, fcntl.LOCK_EX \| fcntl.LOCK_NB)`. |
| `CURRENT` | `G.current_gen`, `G.flip` (`vmem/store.py:51, 67, 112, 121, 124, 146, 182, 188, 206, 251`) | Pointer to the current generation number `n`. Exact on-disk encoding inside `vmem/gen.py` is `ABSENT`. |
| `blobs/sha256/<sha[:2]>/<sha>.<ext>` | `blob_path`, `put_blob`, `get_blob`, `iter_blobs`, `delete_blob` (`vmem/blobs.py:41, 65, 97, 115–123, 128`) | Content-addressed immutable (`0o444`) blob file where `<sha>` is 64 lowercase hex characters and `<ext>` is in `("png", "bin", "mp4")` (`vmem/blobs.py:10–11, 82`). |
| `blobs/sha256/<sha[:2]>/<sha>.<ext>.tmp` | `put_blob`, `clean_tmps` (`vmem/blobs.py:45–58, 72–91`) | Temporary staging file during `put_blob`, replaced via `os.replace(tmp, path)` (`vmem/blobs.py:91`) or unlinked by `clean_tmps` (`vmem/blobs.py:53`). |
| `journal/%06d.jsonl` | `Journal` (`vmem/receipts.py:14, 40, 45, 63, 169`) | Hash-chained JSONL journal segments (`000001.jsonl`, etc.), rotated at `VM_JSEG_CAP` (`4,194,304` B, `vmem/receipts.py:11, 34–36, 165–167`). |
| `journal/base_%06d.json` | `Journal.base`, `Journal.checkpoint` (`vmem/receipts.py:15, 107–113, 131`) | Compaction checkpoint JSON `{gen, seq, h}` (`vmem/receipts.py:130–134`). |
| `journal/gc_%06d.txt` | `Store.gc_blobs` (`vmem/store.py:232–233`) | Newline-terminated sorted list of deleted blob SHA-256 strings for `gc` journal record `seq`. |
| `quarantine/<run_id_sanitized>/<spool_basename>` | `_quarantine` (`vmem/ingest.py:108–112, 198, 202`) | Quarantined spool file moved via `shutil.move` when `parse_spool` fails; `<run_id_sanitized>` is `re.sub(r"[^A-Za-z0-9_]", "_", run_id or "unknown")[:40]` (`vmem/ingest.py:109`). |
| `gen/%06d/` | `G.gen_dir`, `G.publish_gen`, `write_gen` (`vmem/store.py:60, 134, 181`, `vmem/ingest.py:129, 140–154, 173`) | Generation directory for generation `n` (formatted as `%06d` in `vmem/ingest.py:129, 173`; internal `G.gen_dir`/`G.publish_gen` implementation in `vmem/gen.py` is `ABSENT`). |
| `gen/%06d/units/active.jsonl` | `_UNIT_FILES`, `write_gen`, `_load`, `View.units`, `_g_nofloat`, `verify` (`vmem/store.py:14, 152`, `vmem/ingest.py:147, 171`, `vmem/router.py:146`, `vmem/gates.py:26, 44, 160`) | Canonical JSONL of active `vmem.unit/v1` records sorted by `u["id"]` (`vmem/units.py:189–194`). |
| `gen/%06d/units/archive.jsonl` | `_UNIT_FILES`, `write_gen`, `_load`, `_g_nofloat`, `verify` (`vmem/store.py:14, 152`, `vmem/ingest.py:150–151, 171`, `vmem/gates.py:44, 160, 168`) | Canonical JSONL of archived/retired `vmem.unit/v1` records sorted by `u["id"]`, written by `write_gen` only when `archive` is non-empty (`vmem/ingest.py:150–151`). |
| `gen/%06d/episodes/<run_id>.jsonl` | `_carry_episodes`, `write_gen`, `ingest` (`vmem/ingest.py:123–137, 144–146, 327–328`) | Canonical JSONL of episode dicts for `<run_id>` with `gop_blob_hex` stripped (`vmem/ingest.py:327`), carried verbatim across child generations (`vmem/ingest.py:123–137, 144`). |
| `gen/%06d/catalogue/foreground.tsv` | `render_catalogue`, `check_catalogue`, `Store.commit`, `write_gen`, `_g_catalogue` (`vmem/units.py:22, 265–291`, `vmem/store.py:162–165`, `vmem/ingest.py:148`, `vmem/gates.py:76–82`) | TSV file with header `id\tkey\tpin\ttk\tfirst_run\tlast_run\tsummary\tcrop_sha8` (`vmem/units.py:22`) and one row per FG unit sorted by `(key, id)` (`vmem/units.py:265–278`). |
| `gen/%06d/VISUAL_DIGEST.md` | `_digest`, `write_gen`, `View.digest`, `_g_caps` (`vmem/ingest.py:20, 115–120, 149`, `vmem/router.py:162–166`, `vmem/gates.py:14, 121–125`) | UTF-8 summary starting with `# VISUAL_DIGEST` and `units=<total> fg=<fg_count>`, followed by up to 40 FG units sorted by `id` as `<id> <pin> <summary[:60]>`, truncated to `DIGEST_MAX = 6000` bytes (`vmem/ingest.py:20, 115–120`). |
| `gen/%06d/index/tk.json` | `build_tk_index`, `write_gen`, `View.tk_index` (`vmem/router.py:81–91, 156–160`, `vmem/ingest.py:152`) | Canonical JSON mapping `tk -> {"golden": [unit_id, ...], "last_good": unit_id \| null, "units": [unit_id, ...]}` built from active units (`vmem/router.py:81–91`, `vmem/ingest.py:152`). |
| `gen/%06d/index/sig/ids.bin` | `build_sig_index`, `sig_lookup` (`vmem/router.py:45–58, 202`, `vmem/ingest.py:153`) | ASCII lines `<unit_id>\t<sig64>\n` for all anchors with `sig64` on active `defect`, `region_event`, and `baseline` units sorted by `id` (`vmem/router.py:48–54`). |
| `gen/%06d/index/sig/b0.bin` .. `b3.bin` | `build_sig_index`, `_read_pairs`, `sig_lookup` (`vmem/router.py:55–58, 195–197, 210–217`, `vmem/ingest.py:153`) | Packed little-endian `<HI` (`band u16`, `row u32`) pairs sorted by `(band, row)` where `band = (int(sig64, 16) >> (16 * k)) & 0xFFFF` for `k in range(4)` (`vmem/router.py:55–57`). |

### 2.2 Constants and Helper Functions in `vmem/store.py`
- `_UNIT_FILES = ("units/active.jsonl", "units/archive.jsonl")` (`vmem/store.py:14`).
- `BLOB_POINTS = ("blob_tmp_written", "blob_chmod", "gc_journaled")` (`vmem/store.py:72`): durability hook names beyond `gen.POINTS` (`vmem/gen.py` `POINTS` definition is `ABSENT`).
- `_parse_units(raw, strict=True)` (`vmem/store.py:17–31`): Decodes `raw` as UTF-8 (`errors="replace"`), skips blank lines, parses each line with `canon.loads(ln)` (`vmem/store.py:24`), and when `strict=True` calls `U.validate_unit(u)` (`vmem/store.py:25–26`). On `VqaError`, re-raises if `strict=True`, otherwise ignores the line (`vmem/store.py:28–30`).
- `_check_fg_edits(parent, child)` (`vmem/store.py:34–47`): Enforces immutability of published FG units (`U.is_fg(old)`, `vmem/store.py:38`). For every FG unit `old` in `parent`, looks up `new = byid[old["id"]]` in `child` (`vmem/store.py:36, 40`). Compares all fields except `"status"` and `"last_gen"` (`vmem/store.py:41–42`). Status may either stay unchanged (`new["status"] == old["status"]`) or transition from `old["status"] == "active"` to `new["status"] == "superseded"` only if some unit `u` in `child` has a link in `u["links"]` with `l["rel"] == "supersedes"` and `l["to"] == old["id"]` (`vmem/store.py:43–45`). If any other field changed, status transition is invalid, or `new["last_gen"] < old["last_gen"]`, raises `VqaError("E_GATE", {"reason": "g_fg_monotone", "edited": old["id"]})` (`vmem/store.py:46–47`).

### 2.3 `Snapshot` (`vmem/store.py:50–69`)
- `Snapshot(store, n)` (`vmem/store.py:53–56`): Stores `self.store = store`, `self.gen = n`, loads `self.manifest = G.read_manifest(store.root, n)`, and records `self.manifest_sha = G.manifest_sha(store.root, n)`.
- `Snapshot.read(rel)` (`vmem/store.py:58–63`): Reads and returns raw bytes of `os.path.join(G.gen_dir(self.store.root, self.gen), rel)`. On `OSError`, raises `VqaError("E_ID", {"reason": "file_missing", "path": rel[:60]})` (`vmem/store.py:62–63`).
- `Snapshot.validate()` (`vmem/store.py:65–69`): Checks `G.current_gen(self.store.root) != self.gen` or `G.manifest_sha(self.store.root, self.gen) != self.manifest_sha`; if either changed, raises `VqaError("E_STALE", {"reason": "current_changed", "gen": self.gen})` (`vmem/store.py:67–68`), else returns `True` (`vmem/store.py:69`).

### 2.4 `Store` (`vmem/store.py:75–263`, `# @anchor:vm.store.Store`)
- `Store.__init__(root)` (`vmem/store.py:76–81`): Sets `self.root = str(root)`, `self.hook = None`, `self._lockfd = None`, creates `self.root` (`os.makedirs(self.root, exist_ok=True)`), and initializes `self.journal = Journal(self.root)`.
- `Store._point(name)` (`vmem/store.py:83–85`): Calls `self.hook(name)` if `self.hook` is set.
- `Store.lock()` (`vmem/store.py:87–102`): Context manager that opens `<root>/.lock` (`os.O_RDWR | os.O_CREAT`, `0o644`) and acquires a non-blocking exclusive flock (`fcntl.LOCK_EX | fcntl.LOCK_NB`, `vmem/store.py:90–92`). On `OSError`, closes `fd` and raises `VqaError("E_STALE", {"reason": "lock_held"})` (`vmem/store.py:93–95`). Sets `self._lockfd = fd`, runs `self.recover()`, yields `self`, and resets `self._lockfd = None` and closes `fd` in `finally` (`vmem/store.py:96–102`).
- `Store._need_lock()` (`vmem/store.py:104–106`): Raises `VqaError("E_USAGE", {"reason": "lock_required"})` if `self._lockfd is None`.
- `Store.recover()` (`vmem/store.py:108–118`):
  1. Calls `self.journal.recover_torn()` (`vmem/store.py:110`).
  2. Calls `clean_tmps(self.root)` (`vmem/store.py:111`).
  3. Reads `cur = G.current_gen(self.root)` and collects all `gen` numbers from `"flip"` journal records (`vmem/store.py:112–113`).
  4. Computes `acked = max([cur or 0] + flips)` and removes unacknowledged generations via `removed = G.cleanup_unacked(self.root, acked)` (`vmem/store.py:114–115`).
  5. If `cur is not None and cur not in flips`, appends a reconciled flip record: `self.journal.append("flip", {"gen": cur, "manifest_sha256": G.manifest_sha(self.root, cur), "reconciled": 1})` (`vmem/store.py:116–117`).
  6. Returns `removed` (`vmem/store.py:118`).
- `Store.current()` (`vmem/store.py:120–121`): Returns `G.current_gen(self.root)`.
- `Store.snapshot()` (`vmem/store.py:123–127`): Calls `n = G.current_gen(self.root)`; if `n is None`, raises `VqaError("E_STALE", {"reason": "empty_store"})`, else returns `Snapshot(self, n)`.
- `Store.gen_units(n, strict=False)` (`vmem/store.py:129–138`): Reads `units/active.jsonl` and `units/archive.jsonl` under `G.gen_dir(self.root, n)` (ignoring `OSError` per missing file), parses via `_parse_units(..., strict)`, and returns the concatenated list.
- `Store.commit(files, blobs=(), extra=None, strict=True)` (`vmem/store.py:140–184`, `# @anchor:vm.store.commit`):
  1. Requires lock via `self._need_lock()` (`vmem/store.py:145`).
  2. Computes `cur = G.current_gen(self.root)` and `n = max([cur or 0] + G.list_gens(self.root)) + 1`, then validates `G.gen_name(n)` before any write (`vmem/store.py:146–148`).
  3. When `strict=True` (`vmem/store.py:151–165`):
     - Parses and validates `units` from `files.get("units/active.jsonl", b"")` and `files.get("units/archive.jsonl", b"")` (`vmem/store.py:152`).
     - If `cur is not None`, loads `parent_units = self.gen_units(cur)` and runs `C.g_fg_monotone(parent_units, units)` and `_check_fg_edits(parent_units, units)` (`vmem/store.py:153–156`).
     - Runs gates `_g_grid` and `_g_cite` from `vmem.gates` on `{"units": units}`; if either returns `False`, raises `VqaError("E_GATE", {"reason": name})` (`vmem/store.py:157–160`).
     - If `"catalogue/foreground.tsv"` is in `files`, runs `U.check_catalogue(units, files["catalogue/foreground.tsv"].decode("utf-8"))`; otherwise if any FG unit exists (`fgset`), raises `VqaError("E_GATE", {"reason": "g_catalogue", "why": "missing_catalogue"})` (`vmem/store.py:161–165`).
  4. Writes all `blobs` via `shas = [put_blob(self.root, data, ext, self._point) for data, ext in blobs]` (`vmem/store.py:166`).
  5. When `units is not None` (`strict=True`, `vmem/store.py:167–179`):
     - Verifies every SHA in `U.blob_refs(u)` exists in `iter_blobs(self.root)`; if any are missing (`lost`), raises `VqaError("E_GATE", {"reason": "g_blob_integrity", "missing": lost[:8]})` (`vmem/store.py:168–171`).
     - Verifies every referenced blob passes `verify_blob(self.root, s, e)`; if any fail (`corrupt`), raises `VqaError("E_GATE", {"reason": "g_blob_integrity", "corrupt": corrupt[:8]})` (`vmem/store.py:172–175`).
     - Updates `extra` with `"unit_count": len(units)`, `"fg_count": sum(1 for u in units if U.is_fg(u))`, `"blob_bytes": sum(sizes.values())`, `"fg_blob_bytes": C.fg_bytes(units, sizes)`, and `"fg_ids_sha256": C.fg_ids_sha256(units)` (`vmem/store.py:176–179`).
  6. Sets `extra["blob_count"] = len(iter_blobs(self.root))` (`vmem/store.py:180`).
  7. Publishes generation `n` via `msha = G.publish_gen(self.root, n, cur, files, extra, self._point)`, flips `CURRENT` via `G.flip(self.root, n, self._point)`, appends journal record `self.journal.append("flip", {"gen": n, "manifest_sha256": msha})`, and returns `{"gen": n, "manifest_sha256": msha, "blobs": shas}` (`vmem/store.py:181–184`).
- `Store.window_refs()` (`vmem/store.py:186–199`): Returns `set()` if `G.current_gen(self.root)` is `None`. Otherwise unions `U.blob_refs(u)` across all units in `self.gen_units(n)` for every generation `n in G.list_gens(self.root)` with `n >= cur - G.VM_GEN_KEEP + 1` (`vmem/store.py:192–196`). If `U.blob_refs(u)` raises `KeyError` or `TypeError`, raises `VqaError("E_GATE", {"reason": "g_window", "why": "malformed_unit", "gen": n})` (`vmem/store.py:197–198`).
- `Store.rollback(to)` (`vmem/store.py:201–208`, `# @anchor:vm.store.rollback`): Requires lock via `self._need_lock()`. Checks `G.in_window(self.root, to)`; if `False`, raises `VqaError("E_ID", {"reason": "gen_outside_window", "to": to})` (`vmem/store.py:204–205`). Calls `G.flip(self.root, to, self._point)`, appends journal record `self.journal.append("rollback", {"to": to})`, and returns `to` (`vmem/store.py:206–208`).
- `Store.gc_blobs(referenced=None)` (`vmem/store.py:210–238`):
  1. Requires lock via `self._need_lock()` (`vmem/store.py:213`).
  2. Verifies `cur = self.current()` is not `None` and that the journal contains a `"flip"` record with `body.get("gen") == cur` and `body.get("manifest_sha256") == G.manifest_sha(self.root, cur)`; otherwise raises `VqaError("E_GATE", {"reason": "g_window", "why": "flip_unacknowledged"})` (`vmem/store.py:214–218`).
  3. Computes `keep = self.window_refs()` (`vmem/store.py:219`). If `referenced is not None`, checks `miss = sorted(keep - set(referenced))`; if non-empty, raises `VqaError("E_GATE", {"reason": "g_window", "missing": miss[:8]})`, else unions `keep |= set(referenced)` (`vmem/store.py:220–224`).
  4. Finds `doomed = [(sha, ext) for sha, ext in iter_blobs(self.root) if sha not in keep]` (`vmem/store.py:225`).
  5. If `doomed` is non-empty: appends journal record `type="gc"` with `{"n": len(names), "deleted_sha256": canon.sha256_bytes("\n".join(names).encode("ascii")), "deleted_head": names[:16]}`, writes `journal/gc_%06d.txt` (`% rec["seq"]`), triggers hook `self._point("gc_journaled")`, and calls `delete_blob(self.root, sha, ext)` for each doomed blob (`vmem/store.py:226–236`).
  6. Calls `self._prune_gens(cur)` and returns `[sha for sha, _e in doomed]` (`vmem/store.py:237–238`).
- `Store._prune_gens(cur)` (`vmem/store.py:240–246`): Sets `cut = cur - G.VM_GEN_KEEP + 1` and calls `G.delete_gen(self.root, n)` for every `n in G.list_gens(self.root)` where `n < cut`.
- `Store.compact_journal()` (`vmem/store.py:248–256`): Requires lock via `self._need_lock()`. Returns `None` if `cur = G.current_gen(self.root)` is `None`. Otherwise sets `cut = cur - G.VM_GEN_KEEP + 1`, finds `old = [r["seq"] for r in self.journal.records() if r["type"] == "flip" and r["body"].get("gen", cut) < cut]`, and returns `self.journal.checkpoint(max(old)) if old else None`.
- `Store.verify_blobs(quick=False)` (`vmem/store.py:258–263`): Lists `items = iter_blobs(self.root)` (sliced to `items[:64]` if `quick=True`) and returns `[s for s, e in items if not verify_blob(self.root, s, e)]`.

## 3. Unit Schema and Validation Rules (`vmem/units.py`)

### 3.1 Constants in `vmem/units.py`

| Constant | Value | Location |
|---|---|---|
| `UNIT_MAX_B` | `1024` | `vmem/units.py:10` |
| `KINDS` | `("baseline", "region_event", "episode", "verdict", "defect")` | `vmem/units.py:11` |
| `KIND_CODE` | `{"baseline": "BAS", "region_event": "EVT", "episode": "EPI", "verdict": "VER", "defect": "DEF"}` | `vmem/units.py:12` |
| `PINS` | `("FAIL", "GOLDEN", "CATALOGUED")` | `vmem/units.py:13` |
| `STATUSES` | `("active", "superseded", "retired")` | `vmem/units.py:14` |
| `PREDICATES` | `("baseline", "changed", "moved_from", "frozen", "flicker", "drift", "overlaps", "appeared", "vanished", "supersedes", "look_changed")` | `vmem/units.py:15–16` |
| `LINK_RELS` | `("moved_from", "supersedes", "same_defect")` | `vmem/units.py:17` |
| `ROLES` | `("tile", "ui", "actor", "a11y", "object", "gizmo", "island")` | `vmem/units.py:18` |
| `SCOPE_RE["app"]` | `re.compile(r"^[a-z0-9_]{1,32}$")` | `vmem/units.py:19` |
| `SCOPE_RE["scene"]` | `re.compile(r"^[a-z0-9_]{1,48}$")` | `vmem/units.py:19` |
| `HEX64` | `re.compile(r"^[0-9a-f]{64}$")` | `vmem/units.py:20` |
| `HEX16` | `re.compile(r"^[0-9a-f]{16}$")` | `vmem/units.py:21` |
| `CAT_HEADER` | `"id\tkey\tpin\ttk\tfirst_run\tlast_run\tsummary\tcrop_sha8"` | `vmem/units.py:22` |
| `VM_LOOK_SHARE_PPM` | `950000` | `vmem/units.py:23` |
| `SIG_HAM_MAX` | `3` | `vmem/units.py:24` |

### 3.2 Top-Level `vmem.unit/v1` Schema (`validate_unit`, `vmem/units.py:117–178`)
`validate_unit(u)` (`# @anchor:vm.units.validate_unit`, `vmem/units.py:117`) requires `isinstance(u, dict)` and `u.get("schema") == "vmem.unit/v1"` (`vmem/units.py:119–120`), and requires `set(u)` to equal the exact 23-key set `need` (`vmem/units.py:121–125`):

| Field | Type & Validation Rule | Failure Error Code & Detail | Location |
|---|---|---|---|
| `schema` | Must equal `"vmem.unit/v1"` | `E_SCHEMA` `{"reason": "schema"}` | `vmem/units.py:119–120` |
| `(key set)` | `set(u) == need` (23 keys) | `E_SCHEMA` `{"reason": "fields", "missing": ..., "extra": ...}` | `vmem/units.py:121–125` |
| `id` | `ids.parse_any(u["id"])` must return `("unit", f)` with `u["kind"] in KINDS` and `f["kind"] == KIND_CODE[u["kind"]]` | `E_ID` `{"id": u["id"], "kind": u["kind"]}` (or `VqaError` from `ids.parse_any`) | `vmem/units.py:126–128` |
| `key` | `isinstance(u["key"], str)` and `len(u["key"]) <= 96` | `E_SCHEMA` `{"reason": "key"}` | `vmem/units.py:129–130` |
| `kind` | Member of `KINDS` (`"baseline"`, `"region_event"`, `"episode"`, `"verdict"`, `"defect"`) matching `id` prefix | `E_ID` `{"id": u["id"], "kind": u["kind"]}` | `vmem/units.py:11–12, 127–128` |
| `layer` | In `layout.UNIT_LAYERS` (`("FG", "BG")` per `vmem/gates.py:37`; exact definition in `visioncore/layout.py` is `ABSENT`) | `E_SCHEMA` `{"reason": "layer_pin"}` | `vmem/units.py:131–132` |
| `pin` | In `layout.UNIT_PINS` (`("FAIL", "GOLDEN", "CATALOGUED", None)` per `vmem/gates.py:19`; exact definition in `visioncore/layout.py` is `ABSENT`), and `(u["layer"] == "FG") == (u["pin"] is not None)` | `E_SCHEMA` `{"reason": "layer_pin"}` or `{"reason": "layer_pin_mismatch", "id": u["id"]}` | `vmem/units.py:131–134` |
| `status` | In `STATUSES` (`"active"`, `"superseded"`, `"retired"`), and cannot have `status == "retired"` when `layer == "FG"` | `E_SCHEMA` `{"reason": "status", "id": u["id"]}` | `vmem/units.py:14, 135–136` |
| `born_gen` | Integer (not `bool`), `0 <= born_gen <= 999999` (`_int`) | `E_SCHEMA` `{"reason": "int_range", "field": "born_gen"}` | `vmem/units.py:32–34, 137` |
| `last_gen` | Integer (not `bool`), `0 <= last_gen <= 999999` (`_int`), and `last_gen >= born_gen` | `E_SCHEMA` `{"reason": "int_range", "field": "last_gen"}` or `{"reason": "gen_order"}` | `vmem/units.py:32–34, 138–140` |
| `scope` | Validated by `validate_scope(u["scope"])` (Section 3.3) | `E_SCHEMA` or `E_USAGE` (or `VqaError` from `ids.parse_any`) | `vmem/units.py:60–73, 141` |
| `tk` | `None` or `ids.parse_any(u["tk"])[0] == "track_key"` | `E_SCHEMA` `{"reason": "tk"}` (or `VqaError` from `ids.parse_any`) | `vmem/units.py:142–143` |
| `subject` | `isinstance(u["subject"], str)` and `len(u["subject"]) <= 48` | `E_SCHEMA` `{"reason": "subject"}` | `vmem/units.py:144–145` |
| `predicate` | In `PREDICATES` | `E_SCHEMA` `{"reason": "predicate"}` | `vmem/units.py:15–16, 146–147` |
| `value` | `isinstance(u["value"], str)` and `len(u["value"]) <= 64` | `E_SCHEMA` `{"reason": "value"}` | `vmem/units.py:148–149` |
| `value_num` | Integer (not `bool`), `-(1 << 63) <= value_num <= (1 << 63) - 1` | `E_SCHEMA` `{"reason": "int_range", "field": "value_num"}` | `vmem/units.py:32–34, 150` |
| `value_den` | Integer (not `bool`), `1 <= value_den <= 0xFFFFFFFF` | `E_SCHEMA` `{"reason": "int_range", "field": "value_den"}` | `vmem/units.py:32–34, 151` |
| `class` | In `layout.CLS` (exact tuple in `visioncore/layout.py` is `ABSENT`; bound files use `"UNCHANGED"`, `"JITTER"`, `"CHANGED"`, `"MOVED"`, `"FLICKER"`, `"FROZEN"`, `"DRIFT"`, `vmem/ingest.py:18, 94`) | `E_SCHEMA` `{"reason": "class", "cls": u["class"]}` | `vmem/units.py:152–153` |
| `look` | `None` or validated by `validate_look(u["look"], retired=(u["status"] == "retired"))` (Section 3.3) | `E_SCHEMA` | `vmem/units.py:101–115, 154–156` |
| `anchors` | `isinstance(u["anchors"], list)` and `len(u["anchors"]) <= 6`; each element validated by `_validate_anchor(a)` (Section 3.3) | `E_SCHEMA` `{"reason": "anchors"}` or anchor error | `vmem/units.py:75–98, 157–160` |
| `links` | `isinstance(u["links"], list)` and `len(u["links"]) <= 4`; each element `l` must be `dict` with `set(l) == {"rel", "to"}`, `l["rel"] in LINK_RELS`, and `ids.parse_any(l["to"])[0] == "unit"` | `E_SCHEMA` `{"reason": "links"}` or `{"reason": "link"}`, or `E_ID` `{"to": l["to"]}` | `vmem/units.py:161–167` |
| `verdict` | `None` or in `layout.VERDICTS` (exact tuple in `visioncore/layout.py` is `ABSENT`; `vmem/ingest.py:100` checks `("PASS", "FAIL", "CANNOT_SEE")`) | `E_SCHEMA` `{"reason": "verdict"}` | `vmem/units.py:168–169` |
| `check_id` | `None` or (`isinstance(u["check_id"], str)` and `len(u["check_id"]) <= 64`) | `E_SCHEMA` `{"reason": "check_id"}` | `vmem/units.py:170–171` |
| `receipt_sha256` | `isinstance(u["receipt_sha256"], str)` and `HEX64.match(u["receipt_sha256"])` | `E_SCHEMA` `{"reason": "receipt"}` | `vmem/units.py:172–173` |
| `summary` | `isinstance(u["summary"], str)` and `len(u["summary"]) <= 160` | `E_SCHEMA` `{"reason": "summary"}` | `vmem/units.py:174–175` |
| `(serialized size)` | `len(dump_unit(u).encode("utf-8")) <= UNIT_MAX_B` (`1024`) | `E_BUDGET` `{"id": u["id"], "limit": 1024}` | `vmem/units.py:10, 176–177` |

### 3.3 Sub-Object Schemas (`scope`, `anchor`, `look`)

#### `scope` (`validate_scope`, `vmem/units.py:60–73`)
Must be a `dict` with `set(s) == {"app", "scene", "res", "grid", "build_seq"}` (`E_SCHEMA` `{"reason": "scope_fields"}`, `vmem/units.py:61–62`):
- `app`: `str` matching `SCOPE_RE["app"]` (`^[a-z0-9_]{1,32}$`), else `VqaError("E_USAGE", {"scope": "app"})` (`vmem/units.py:19, 63–65`).
- `scene`: `str` matching `SCOPE_RE["scene"]` (`^[a-z0-9_]{1,48}$`), else `VqaError("E_USAGE", {"scope": "scene"})` (`vmem/units.py:19, 63–65`).
- `res`: `str` full-matching `r"[0-9]{1,4}x[0-9]{1,4}"`, else `E_SCHEMA` `{"reason": "res"}` (`vmem/units.py:66–67`).
- `grid`: `ids.parse_any(s["grid"])[0] == "grid"`, else `E_SCHEMA` `{"reason": "grid"}` (`vmem/units.py:68–70`).
- `build_seq`: `None` or integer `0 <= build_seq <= 0xFFFFFFFF`, else `E_SCHEMA` `{"reason": "int_range", "field": "build_seq"}` (`vmem/units.py:71–72`).

#### `anchor` (`_validate_anchor`, `vmem/units.py:75–98`)
Must be a `dict` with key set equal to `need` or `need | {"gop_blob"}`, where `need = {"run_id", "cite", "frame", "render_index", "bbox_px", "mv_px", "xh64", "sig64", "crop", "desc_blob"}` (`E_SCHEMA` `{"reason": "anchor_fields"}`, `vmem/units.py:76–78`):
- `run_id`: `ids.parse_any(a["run_id"])[0] == "run_id"`, else `E_SCHEMA` `{"reason": "anchor_run"}` (`vmem/units.py:79–80`).
- `cite`: `ids.parse_any(a["cite"])[0] == "cross_cite"`, else `E_SCHEMA` `{"reason": "anchor_cite"}` (`vmem/units.py:81–82`).
- `frame`: integer `0 <= frame <= 0xFFFFFFFF`, else `E_SCHEMA` `{"reason": "int_range", "field": "frame"}` (`vmem/units.py:83`).
- `render_index`: `None` or integer `0 <= render_index <= 0xFFFFFFFF`, else `E_SCHEMA` `{"reason": "int_range", "field": "render_index"}` (`vmem/units.py:84–85`).
- `bbox_px`: `list` of 4 integers each in `0..0xFFFF`, else `E_SCHEMA` `{"reason": "anchor_geom"}` or `{"reason": "int_range", "field": "bbox_px"}` (`vmem/units.py:86–90`).
- `mv_px`: `list` of 2 integers each in `-32768..32767`, else `E_SCHEMA` `{"reason": "anchor_geom"}` or `{"reason": "int_range", "field": "mv_px"}` (`vmem/units.py:87–92`).
- `xh64`, `sig64`: `str` matching `HEX16` (`^[0-9a-f]{16}$`), else `E_SCHEMA` `{"reason": "anchor_hash", "field": k}` (`vmem/units.py:93–95`).
- `crop`, `desc_blob`, `gop_blob`: `None` (or omitted for `gop_blob`) or `str` matching `HEX64` (`^[0-9a-f]{64}$`), else `E_SCHEMA` `{"reason": "anchor_blob", "field": k}` (`vmem/units.py:96–98`).

#### `look` (`validate_look`, `vmem/units.py:101–115`)
Must be a `dict` with `set(lk) == {"grid", "n", "ah64", "desc_blob", "y_q4_mean"}` (`E_SCHEMA` `{"reason": "look_fields"}`, `vmem/units.py:102–103`):
- `grid`: `ids.parse_any(lk["grid"])[0] == "grid"`, else `E_SCHEMA` `{"reason": "look_grid"}` (`vmem/units.py:104–105`).
- `n`: integer `1 <= n <= 0xFFFF`, else `E_SCHEMA` `{"reason": "int_range", "field": "look.n"}` (`vmem/units.py:106`).
- `y_q4_mean`: integer `0 <= y_q4_mean <= 0xFFFF`, else `E_SCHEMA` `{"reason": "int_range", "field": "look.y_q4_mean"}` (`vmem/units.py:107`).
- `ah64`: `str` matching `HEX16` (`^[0-9a-f]{16}$`), else `E_SCHEMA` `{"reason": "look_ah64"}` (`vmem/units.py:108–109`).
- `desc_blob`: if `None`, allowed only when `retired=True` (`E_SCHEMA` `{"reason": "look_desc_blob"}` when `retired=False`); if not `None`, must be `str` matching `HEX64` (`vmem/units.py:110–114`).

### 3.4 Unit Helpers, Pinning, Catalogue, and Look Equality (`vmem/units.py:38–57, 181–320`)
- `track_key(app, scene, W, H, role, m0, m1)` (`vmem/units.py:38–43`): Checks `role in ROLES` and `SCOPE_RE["app"].match(app)` and `SCOPE_RE["scene"].match(scene)` (else `E_SCHEMA` `{"reason": "track_key"}`), checks `0 <= m0 <= m1 <= 0xFFFF` (else `VqaError("E_VC_ID_RANGE", {"m0": m0, "m1": m1})`), and delegates to `ids.track_key(app, scene, W, H, role, m0, m1)`.
- `tk_hash8(tk)` (`vmem/units.py:46–47`): Returns `hashlib.sha256(tk.encode("utf-8")).hexdigest()[:8]`.
- `unit_key(app, scene, tk, kind)` (`vmem/units.py:50–51`): Returns `"vision.%s.%s.%s.%s" % (app, scene, tk_hash8(tk), kind)`.
- `scope_key(u)` (`vmem/units.py:54–56`): Returns `(s["app"], s["scene"], s["res"], s["grid"])` where `s = u["scope"]`.
- `dump_unit(u)` (`vmem/units.py:181–182`): Returns `canon.dumps(u)`.
- `load_units(text)` (`vmem/units.py:185–186`): Returns `[canon.loads(l) for l in text.splitlines() if l.strip()]`.
- `sort_units(units)` (`vmem/units.py:193–194`): Returns `sorted(units, key=lambda u: u["id"])`.
- `dump_units(units)` (`vmem/units.py:189–190`): Returns `"".join(dump_unit(u) for u in sort_units(units))`.
- `make_unit(n, kind, scope, tk, predicate="baseline", cls="UNCHANGED", pin=None, gen=1, anchors=None, look=None, links=None, verdict=None, check_id=None, receipt_sha256="0" * 64, subject=None, value="", value_num=0, value_den=1, summary="", status="active")` (`vmem/units.py:197–208`): Constructs a `vmem.unit/v1` dict with `id = ids.unit_id(KIND_CODE[kind], n)`, `layer = "FG" if pin else "BG"`, `born_gen = gen`, `last_gen = gen`, `subject = subject if subject is not None else tk_hash8(tk)`, and validates it via `validate_unit(u)`.
- `is_fg(u)` (`vmem/units.py:212–213`): Returns `u["pin"] is not None`.
- `apply_pins(units, fail_cites=())` (`vmem/units.py:216–229`): Collects `cites = set(fail_cites)` unioned with `{a["cite"] for a in u["anchors"]}` for every unit `u` where `u["verdict"] == "FAIL"` or `u["kind"] == "defect"` (`vmem/units.py:219–222`). Returns a new list where any unit with `u["pin"] is None` that has `u["verdict"] == "FAIL"`, `u["kind"] == "defect"`, or (`u["kind"] == "region_event"` and any `a["cite"] in cites`) is copied with `pin="FAIL", layer="FG"` (`vmem/units.py:224–229`).
- `pin_unit(u, pin="CATALOGUED")` (`vmem/units.py:232–235`): Validates `pin in PINS` (else `E_SCHEMA` `{"reason": "pin"}`) and returns `validate_unit(dict(u, pin=pin, layer="FG"))`.
- `supersede(old, new_id)` (`vmem/units.py:238–242`): Requires `is_fg(old)` (else `E_SCHEMA` `{"reason": "not_fg"}`) and returns `(dict(old, status="superseded"), {"rel": "supersedes", "to": old["id"]})`.
- `unit_run(u)` (`vmem/units.py:245–246`): Returns `u["anchors"][0]["run_id"] if u["anchors"] else None`.
- `blob_refs(u)` (`vmem/units.py:249–257`): Returns the set of non-empty `"crop"`, `"desc_blob"`, and `"gop_blob"` values across `u["anchors"]` plus `u["look"]["desc_blob"]` if `u["look"]` and `u["look"]["desc_blob"]` are truthy.
- `catalogue_rows(units)`, `render_catalogue(units)`, `check_catalogue(units, text)` (`vmem/units.py:261–291`):
  - `_tsv(s)` replaces `\t` and `\n` with spaces and slices `[:120]` (`vmem/units.py:261–262`).
  - `catalogue_rows(units)` emits one tab-separated row per FG unit (`id`, `key`, `pin`, `tk or ""`, `first_run`, `last_run`, `_tsv(summary)`, `crop[:8] if crop else ""`), sorted by `(key, id)` (`vmem/units.py:265–274`).
  - `render_catalogue(units)` prepends `CAT_HEADER + "\n"` (`vmem/units.py:277–278`).
  - `check_catalogue(units, text)` verifies the header equals `CAT_HEADER` (else `E_GATE` `{"reason": "g_catalogue", "why": "header"}`) and that the set/count of row IDs matches `sorted(u["id"] for u in units if is_fg(u))` with no duplicates (else `E_GATE` `{"reason": "g_catalogue", "missing": ..., "extra": ...}`) (`vmem/units.py:281–291`).
- `_tiles(blob)` and `look_eq(a, b, load_blob=None, t0_q4=32, ham_max=SIG_HAM_MAX)` (`vmem/units.py:295–320`):
  - If `a["grid"] != b["grid"]`, raises `VqaError("E_STALE", {"reason": "grid_mismatch", "a": a["grid"], "b": b["grid"]})` (`vmem/units.py:303–304`).
  - If `a["n"] != b["n"]`, returns `"DIFFERENT"` (`vmem/units.py:305–306`).
  - If `a["ah64"] == b["ah64"]`, returns `"SAME_EXACT"` (`vmem/units.py:307–308`).
  - If `load_blob is None` or not `a["desc_blob"]` or not `b["desc_blob"]`, returns `"DIFFERENT"` (`vmem/units.py:309–310`).
  - Unpacks tile descriptors from `load_blob(a["desc_blob"])` and `load_blob(b["desc_blob"])` via `_tiles` (which returns `[]` if `len(blob) % layout.TILE_SIZE != 0`, else unpacks each tile of size `layout.TILE_SIZE` via `layout.unpack_tile`, `vmem/units.py:295–298`). If `len(ta) != a["n"]` or `len(tb) != b["n"]` or `not ta`, returns `"DIFFERENT"` (`vmem/units.py:311–313`).
  - Counts matching tiles `x, y` where `x["xh64"] == y["xh64"]` or (`(x["sig64"] ^ y["sig64"]).bit_count() <= ham_max` and `abs(x["y_q4"] - y["y_q4"]) <= t0_q4` and `abs(x["sig_q4"] - y["sig_q4"]) <= t0_q4`) (`vmem/units.py:315–319`).
  - Returns `"SAME_NOISY"` if `match * 1000000 >= VM_LOOK_SHARE_PPM * len(ta)` (`950,000` ppm), else `"DIFFERENT"` (`vmem/units.py:320`).

## 4. Ingestion, Deduplication, and Writer Verbs (`vmem/ingest.py`)

### 4.1 Constants in `vmem/ingest.py`

| Constant | Value | Location |
|---|---|---|
| `SPOOL_SCHEMA` | `"vmem.spool/v1"` | `vmem/ingest.py:14` |
| `EPISODE_REQUIRED` | `("schema", "episode", "run_id", "gop", "f0", "f1", "n_frames", "cut", "gop_blob_sha256", "verdicts", "fg", "classes_ppm", "delta_bytes", "grid", "params_sha256")` | `vmem/ingest.py:15–16` |
| `REGION_REQUIRED` | `("id", "frame", "cls", "bbox_px", "xh64", "sig64", "render_index", "look", "desc_hex")` | `vmem/ingest.py:17` |
| `PRED` | `{"CHANGED": "changed", "MOVED": "moved_from", "FLICKER": "flicker", "FROZEN": "frozen", "DRIFT": "drift"}` | `vmem/ingest.py:18` |
| `ALIGN` | `("render_index", "step", "none")` | `vmem/ingest.py:19` |
| `DIGEST_MAX` | `6000` | `vmem/ingest.py:20` |
| `FG_EPISODE_MAX_B` | `102400` | `vmem/ingest.py:336` |
| `FG_EPISODE_CROPS` | `6` | `vmem/ingest.py:337` |

### 4.2 Spool Format and Validation (`parse_spool`, `_look_region`, `vmem/ingest.py:28–105`)
1. **Non-empty JSONL envelope:** `parse_spool(raw)` splits `raw.decode("utf-8")` into non-blank lines parsed with `canon.loads(l)` (`vmem/ingest.py:56`). If empty, raises `E_SCHEMA` `{"reason": "empty_spool"}` (`vmem/ingest.py:57–58`).
2. **Per-line envelope (`seq == i`):** Every line `i` must be a `dict` with `o.get("schema") == "vmem.spool/v1"`, `o.get("seq") == i`, `o.get("type") in ("run", "episode", "region", "look", "verdict")`, and `isinstance(o.get("body"), dict)`, else `E_SCHEMA` `{"reason": "spool_line", "seq": i}` (`vmem/ingest.py:59–62`).
3. **`run` record (`lines[0]`):**
   - `lines[0]["type"]` must be `"run"` (`E_SCHEMA` `{"reason": "first_not_run"}`, `vmem/ingest.py:63–65`).
   - `rid = run["run_id"]` must be a `str` with `ids.parse_any(rid)[0] == "run_id"` (`E_SCHEMA` `{"reason": "run_id"}`, `vmem/ingest.py:66–68`), and every line in the spool must have `o["run_id"] == rid` (`E_SCHEMA` `{"reason": "run_id_mixed"}`, `vmem/ingest.py:69–70`).
   - `rb = run["body"]` must contain `{"scope", "receipt_sha256", "profile_sha256", "align_source"}` with `rb["align_source"] in ALIGN` (`E_SCHEMA` `{"reason": "run_body"}`, `vmem/ingest.py:71–73`), `U.validate_scope(rb["scope"])` must pass (`vmem/ingest.py:74`), and `rb["receipt_sha256"]` must match `U.HEX64` (`E_SCHEMA` `{"reason": "receipt"}`, `vmem/ingest.py:75–76`).
4. **`episode` records:** Every `type == "episode"` body `b` must have all keys in `EPISODE_REQUIRED` (`E_SCHEMA` `{"reason": "episode_missing_field", "missing": miss}`, `vmem/ingest.py:79–81`), satisfy `b["f1"] == b["f0"] + b["n_frames"] - 1` and `b["run_id"] == rid` (`E_SCHEMA` `{"reason": "episode_f1"}`, `vmem/ingest.py:82–83`), and have `U.HEX64.match(str(b["gop_blob_sha256"]))` (`E_SCHEMA` `{"reason": "episode_gop_sha"}`, `vmem/ingest.py:84–85`).
5. **`look` normalization (`_look_region`, `vmem/ingest.py:28–50`) and `region` records (`vmem/ingest.py:87–96`):**
   - Any line with `type == "look"` is normalized via `_look_region(o["body"], rb["scope"])`: sets `cls="UNCHANGED"` (`vmem/ingest.py:30`); if `"id"` is already in `b`, returns `b` (`vmem/ingest.py:31–32`); otherwise requires `{"frame", "render_index", "role", "m0", "m1", "look", "xh64", "sig64"} <= set(b)` (`E_SCHEMA` `{"reason": "look_missing_field", "missing": ...}`, `vmem/ingest.py:33–35`), computes `b["id"] = ids.region_id(b["frame"], b["m0"], W, H)`, validates `ids.track_key(scope["app"], scope["scene"], W, H, b["role"], b["m0"], b["m1"])` and `U.validate_look(b["look"])` (`vmem/ingest.py:36–40`), requires `b["xh64"]` and `b["sig64"]` to be lists of length `b["look"]["n"]` of `HEX16` strings and replaces `b[k]` with `values[0]` (`E_SCHEMA` `{"reason": "look_hashes", "field": k}`, `vmem/ingest.py:41–46`), and computes `b["bbox_px"] = [x0 * s, y0 * s, min(W, (x1 + 1) * s), min(H, (y1 + 1) * s)]` from `layout.unmorton(b["m0"])` and `layout.unmorton(b["m1"])` (`vmem/ingest.py:47–49`).
   - Every resulting region `b` in `regs` must contain all keys in `REGION_REQUIRED` (`"desc_hex"` may be omitted only if `isinstance(b.get("look"), dict)` and `b["look"].get("desc_blob")` is truthy, else `E_SCHEMA` `{"reason": "region_missing_field", "missing": miss}`, `vmem/ingest.py:90–93`), have `b["cls"] in PRED or b["cls"] in ("UNCHANGED", "JITTER")` (`E_SCHEMA` `{"reason": "region_cls"}`, `vmem/ingest.py:94–95`), and pass `ids.parse_any(ids.cross_cite(rid, b["id"]))` (`vmem/ingest.py:96`).
   - At least one region/look record must exist (`if not regs: _bad("no_regions")`, `vmem/ingest.py:103–104`).
6. **`verdict` records:** Every `type == "verdict"` body `b` must have `b.get("verdict") in ("PASS", "FAIL", "CANNOT_SEE")`, non-empty `b.get("pointers")` where every pointer is in `byid = {b["id"] for b in regs}`, and `isinstance(b.get("check_id"), str)` (`E_SCHEMA` `{"reason": "verdict_body"}`, `vmem/ingest.py:97–102`).

### 4.3 Ingestion and Deduplication Pipeline (`ingest`, `vmem/ingest.py:181–333`, `# @anchor:vm.ingest.ingest`)
1. **Read spool:** Opens `spool` in binary mode; on `OSError`, raises `VqaError("E_IO", {"reason": "spool_unreadable"})` (`vmem/ingest.py:182–186`).
2. **Lock and parse with quarantine on failure:** Under `with Store(root).lock():`, calls `parse_spool(raw)` (`vmem/ingest.py:193–196`). If `parse_spool` raises `VqaError` or `(UnicodeError, ValueError, KeyError, TypeError)`, moves `spool` into `<root>/quarantine/<sanitized_run_id>/<basename>` via `_quarantine`, attaches `"quarantine": os.path.relpath(q, root)` to the error detail, and raises `VqaError` (`vmem/ingest.py:197–203`).
3. **Run-level deduplication (idempotency):** Loads `(active, archive) = _load(st)` from `CURRENT` (`vmem/ingest.py:165–177, 205`). If any existing unit in `active + archive` has an anchor with `a["run_id"] == rid`, returns immediately without writing a new generation or moving the spool: `{"run_id": rid, "already_ingested": True, "gen": st.current()}` (`vmem/ingest.py:206–207`).
4. **Per-region blob staging & anchor building (`vmem/ingest.py:227–245`):**
   - Reads descriptor bytes `desc = bytes.fromhex(b["desc_hex"]) if "desc_hex" in b else get_blob(root, b["look"]["desc_blob"])` and appends `(desc, "bin")` to `blobs` (`vmem/ingest.py:228–229`).
   - If `b.get("crop_hex")` is truthy, decodes crop bytes, computes `crop = canon.sha256_bytes(...)`, and appends `(crop_bytes, "png")` to `blobs` (`vmem/ingest.py:230–233`).
   - Resolves `tk = b.get("tk") or ids.track_key(scope["app"], scope["scene"], W, H, b.get("role", "tile"), b["m0"], b["m1"])`; stores `tk_of.setdefault(b["id"], tk)` (so verdict pointers resolve to the first record of an `id`) and `b["_tk"] = tk` (`vmem/ingest.py:234–237`).
   - Constructs `lk = dict(b["look"], grid=scope["grid"], desc_blob=canon.sha256_bytes(desc))` and anchor `an` (`render_index` is set to `None` when `run["align_source"] == "none"`, `mv_px` defaults to `[0, 0]`) (`vmem/ingest.py:238–245`).
5. **Appearance deduplication against `GOLDEN` chain (`vmem/ingest.py:246–276`):**
   - `gop_fail(frame)` (`vmem/ingest.py:221–226`) checks the first episode `e` in `eps` where `e["f0"] <= frame <= e["f1"]` and returns `any(x.get("verdict") == "FAIL" for x in e["verdicts"])`, falling back to `fail_run = any(v["verdict"] == "FAIL" for v in vds)` if no episode covers `frame` (`vmem/ingest.py:219–226`).
   - For each region `b` in `regs`, computes `chain = router.golden_chain(active + archive + new, tk)` (`vmem/ingest.py:252`):
     - **Case A (`not chain` — first appearance of `tk`):**
       - If `gop_fail(b["frame"])` is `True`: appends a `region_event` unit (`V-EVT-xxxxxx`) with `predicate=PRED.get(b["cls"], "appeared")`, `cls=b["cls"]`, `pin="FAIL"`, `summary="first_seen"` (`vmem/ingest.py:254–257`).
       - Otherwise: appends a `baseline` unit (`V-BAS-xxxxxx`) with `pin="GOLDEN"`, `verdict="PASS"`, `summary="baseline"` (`vmem/ingest.py:258–260`).
     - **Case B (`chain` exists):** Compares `rel = U.look_eq(chain[-1]["look"], lk, load_blob=loaded)` (`vmem/ingest.py:262`):
       - If `rel != "DIFFERENT"` (`"SAME_EXACT"` or `"SAME_NOISY"`): increments `bg_counter[tk]` (`vmem/ingest.py:263–265`). After the loop, for each `(tk, n)` in `sorted(bg_counter.items())`, emits one BG (`pin=None`) `baseline` unit with `value="@unchanged"`, `value_num=n`, `summary="@unchanged n=%d" % n` (`vmem/ingest.py:274–276`).
       - If `rel == "DIFFERENT"`: appends a `region_event` unit with `predicate=PRED.get(b["cls"], "look_changed")`, `cls=b["cls"]`, `pin="FAIL" if gf else None`, `summary=PRED.get(b["cls"], "look_changed")`, and if `not gf` (`gop_fail(b["frame"])` is `False`), also appends a new `GOLDEN` `baseline` unit with `verdict="PASS"`, `summary="new look"` (`vmem/ingest.py:266–273`).
6. **Verdict, defect, and episode units (`vmem/ingest.py:277–298`):**
   - For each `v` in `vds`: takes `ans = [anchors[p] for p in v["pointers"]][:6]` and `tk = tk_of[v["pointers"][0]]`, appends a `verdict` unit (`V-VER-xxxxxx`, `check_id=v["check_id"][:64]`, `summary="%s %s" % (v["check_id"][:40], v["verdict"])`), and if `v["verdict"] == "FAIL"`, also appends a `defect` unit (`V-DEF-xxxxxx`, `predicate="changed"`, `verdict="FAIL"`, `summary="defect %s" % v["check_id"][:40]`) (`vmem/ingest.py:278–287`).
   - For each `e` in `eps`: builds a GOP anchor at `frame=e["f0"]` (`cite=ids.cross_cite(rid, ids.global_id(e["f0"]))`, `gop_blob=e["gop_blob_sha256"]`, `xh64="0"*16`, `sig64="0"*16`, `bbox_px=[0,0,0,0]`, `mv_px=[0,0]`), sets `ev = any(x.get("verdict") == "FAIL" for x in e["verdicts"])`, and appends an `episode` unit (`V-EPI-xxxxxx`) on `first_tk = tk_of[regs[0]["id"]]` with `verdict="FAIL" if ev else "PASS"` and `summary="episode g%s f%d-%d%s"` (appending `" fg_hint_mismatch"` if `bool(e["fg"]) != ev`) (`vmem/ingest.py:288–297`).
   - Runs `everything = U.apply_pins(active + new)` (`vmem/ingest.py:298`).
7. **GOP blobs and per-FAIL-episode FG budget (`vmem/ingest.py:299–325`):**
   - If an episode carries `gop_blob_hex`, decodes `raw_b = bytes.fromhex(e["gop_blob_hex"])`, verifies `canon.sha256_bytes(raw_b) == e["gop_blob_sha256"]` (else raises `VqaError("E_GATE", {"reason": "g_blob_integrity", "gop": e["gop"], "why": "hash_mismatch"})`), and appends `(raw_b, "bin")` to `blobs` (`vmem/ingest.py:302–307`).
   - For every episode `e` with any `verdict == "FAIL"`, collects distinct crop SHAs `crops` from `regs` with `e["f0"] <= r["frame"] <= e["f1"]` and computes `fg_b = (gb or 0) + sum(size.get(c, 0) for c in crops)` where `gb` is the GOP blob byte size (`vmem/ingest.py:310–317`). If `len(crops) > FG_EPISODE_CROPS` (`6`) or `fg_b > FG_EPISODE_MAX_B` (`102400`), raises `VqaError("E_BUDGET", {"reason": "fg_episode_budget", "gop": e["gop"], "bytes": fg_b, "crops": len(crops), "limit": FG_EPISODE_MAX_B})` before any write (`vmem/ingest.py:318–320`).
   - If a FAIL episode's `gop_blob_sha256` is not in `have` (existing store blobs union staged `blobs`), raises `VqaError("E_GATE", {"reason": "g_blob_integrity", "missing": [e["gop_blob_sha256"]]})` (`vmem/ingest.py:321–323`). For non-FAIL units whose anchor `gop_blob` is not in `have`, sets `gop_blob=None` (`vmem/ingest.py:324–325`).
8. **Commit, journal, and `done/` move (`vmem/ingest.py:326–333`):**
   - Strips `"gop_blob_hex"` from `eps` to form `ep_lines`, calls `res = write_gen(st, new_active, archive, blobs, {"run_id": rid}, {rid: ep_lines} if ep_lines else None)`, and appends journal record `st.journal.append("ingest_begin", {"run_id": rid, "gen": res["gen"]})` (`vmem/ingest.py:327–329`).
   - Moves the spool file via `os.replace(spool, os.path.join(done, os.path.basename(spool)))` into `<spool_dir>/done/<basename>` and returns `{"run_id": rid, "gen": res["gen"], "units_added": len(new), "manifest_sha256": res["manifest_sha256"]}` (`vmem/ingest.py:330–333`).

### 4.4 Other Writer Verbs in `vmem/ingest.py` (`compact_run`, `gc_run`, `pin_run`, `run_verb`, lines `340–390`)
- `compact_run(root)` (`vmem/ingest.py:340–349`): Under `st.lock()`, loads `(active, archive) = _load(st)`, computes blob sizes on disk, runs `plan = C.compact(active, sizes, gen=(st.current() or 0) + 1)`, commits via `res = write_gen(st, plan["active"], archive + plan["archive"])`, appends journal record `st.journal.append("compact_begin", {"gen": res["gen"]})`, and returns `{"gen": res["gen"], "retired": len(plan["retired"]), "summaries": len(plan["summaries"]), "fg_ids_sha256": plan["fg_ids_sha256"]}`.
- `gc_run(root)` (`vmem/ingest.py:352–357`): Under `st.lock()`, runs `gone = st.gc_blobs()` and `base = st.compact_journal()`, returning `{"deleted": len(gone), "journal_base": base["seq"] if base else None}`.
- `pin_run(root, uid, receipt)` (`vmem/ingest.py:360–376`): Reads `receipt` file bytes and hashes via `rsha = receipt_sha256(f.read())` (raising `VqaError("E_IO", {"reason": "receipt_unreadable"})` on `OSError`, `vmem/ingest.py:361–365`). Under `st.lock()`, loads `(active, archive) = _load(st)`; if `uid` is not in `active`, raises `VqaError("E_ID", {"reason": "unit_not_found", "id": uid[:20]})` (`vmem/ingest.py:369–371`). Promotes `uid` via `U.pin_unit(dict(u, receipt_sha256=rsha), "CATALOGUED")` if `not U.is_fg(u)` (leaving already-FG units unchanged, `vmem/ingest.py:372–373`), commits via `res = write_gen(st, new, archive)`, appends journal record `st.journal.append("pin", {"id": uid, "gen": res["gen"]})`, and returns `{"gen": res["gen"], "id": uid, "pin": "CATALOGUED"}` (`vmem/ingest.py:374–376`).
- `run_verb(verb, root, a)` (`vmem/ingest.py:379–390`): Dispatches `"ingest"` (requiring `a.spool`, `"--spool"`), `"compact"`, `"gc"`, and `"pin"` (requiring `a.id`, `"ID"` and `a.receipt`, `"--receipt"`), raising `VqaError("E_USAGE", {"reason": "missing_arg", "arg": n})` if a required argument is `None` (`vmem/ingest.py:380–383`).

## 5. Receipts and Journal (`vmem/receipts.py`)

### 5.1 Constants and Helpers (`vmem/receipts.py:11–40`)
- `VM_JSEG_CAP = 4 * 1024 * 1024` (`4,194,304` bytes, `vmem/receipts.py:11`).
- `VM_JREC_MAX = 64 * 1024` (`65,536` bytes, `vmem/receipts.py:12`).
- `TYPES = ("ingest_begin", "flip", "compact_begin", "gc", "pin", "rollback")` (`vmem/receipts.py:13`).
- `_SEG = re.compile(r"^(\d{6})\.jsonl$")` (`vmem/receipts.py:14`).
- `_BASE = re.compile(r"^base_(\d{6})\.json$")` (`vmem/receipts.py:15`).
- `GENESIS = "0" * 64` (`vmem/receipts.py:16`).
- `_write_all(fd, data)` (`vmem/receipts.py:19–26`): Loops `os.write(fd, memoryview(data)[offset:])` until all bytes are written; if `written <= 0`, raises `VqaError("E_IO", {"reason": "journal_short_write"})` (`vmem/receipts.py:24–25`).
- `receipt_sha256(manifest_bytes)` (`vmem/receipts.py:29–31`): Returns `canon.sha256_bytes(manifest_bytes)`.
- `rotates(used, rec, cap=VM_JSEG_CAP)` (`vmem/receipts.py:34–36`): Returns `rec > cap - used` (subtraction form so no sum can wrap).
- `_seg_name(n)` (`vmem/receipts.py:39–40`): Returns `"%06d.jsonl" % n`.

### 5.2 `Journal` Class (`vmem/receipts.py:43–193`, `# @anchor:vm.receipts.Journal`)
- `Journal(root, seg_cap=VM_JSEG_CAP, rec_max=VM_JREC_MAX)` (`vmem/receipts.py:44–49`): Sets `self.dir = os.path.join(str(root), "journal")`, creates `self.dir`, and initializes `self._tail_cache = None`, `self._tail_stamp = None`.
- `segments()` (`vmem/receipts.py:51–52`): Returns sorted integer segment numbers matching `_SEG` in `self.dir`.
- `_stamp()` (`vmem/receipts.py:54–60`): Returns `((), None)` if no segments exist, else `(tuple(segs), (st.st_ino, st.st_size, st.st_mtime_ns))` of the last segment.
- `recover_torn()` (`vmem/receipts.py:65–87`): Reads the last segment; if it does not end with `b"\n"`, truncates after the last `b"\n"` (`keep = data.rfind(b"\n") + 1`, `vmem/receipts.py:74–75`); otherwise checks `_valid_line` on the last line and, if invalid, truncates it (`vmem/receipts.py:77–79`). When truncating, invalidates `_tail_cache`/`_tail_stamp` and calls `f.truncate(keep)`, `f.flush()`, `os.fsync(f.fileno())` (`vmem/receipts.py:80–86`). Returns bytes dropped (`len(data) - keep`, `vmem/receipts.py:87`).
- `_valid_line(b)` (`vmem/receipts.py:90–96`): Parses JSON, pops `"h"`, and checks `canon.sha256_obj(o) == h`.
- `records()` (`vmem/receipts.py:98–105`): Reads and parses all non-empty lines across `self.segments()` in ascending segment order.
- `base()` (`vmem/receipts.py:107–113`): Reads the highest-numbered `base_%06d.json` file in `self.dir`, or returns `None`.
- `_tail()` (`vmem/receipts.py:115–120`): Returns `(recs[-1]["seq"] + 1, recs[-1]["h"])` if `records()` is non-empty, else `(b["seq"] + 1, b["h"])` if `b = self.base()` is not `None`, else `(1, GENESIS)`.
- `checkpoint(upto_seq)` (`vmem/receipts.py:122–146`): Finds record with `r["seq"] == upto_seq` (returns `None` if not found, `vmem/receipts.py:126–128`). Computes `gen = len([n for n in os.listdir(self.dir) if _BASE.match(n)]) + 1`, writes `base = {"gen": gen, "seq": upto_seq, "h": hit[0]["h"]}` to `base_%06d.json` (`0o644`) via `_write_all` and `os.fsync(fd)`, calls `fsync_dir(self.dir)`, unlinks any non-final segment `n in segs[:-1]` whose records all have `seq <= upto_seq`, calls `fsync_dir(self.dir)` again, and returns `base` (`vmem/receipts.py:129–146`).
- `append(rtype, body=None)` (`vmem/receipts.py:148–179`):
  - Validates `rtype in TYPES`; if not, raises `VqaError("E_USAGE", {"reason": "bad_journal_type", "type": str(rtype)})` (`vmem/receipts.py:150–151`).
  - If `_tail_cache is None` or `self._stamp() != self._tail_stamp`, runs `self.recover_torn()` and recomputes `self._tail_cache = self._tail()` (`vmem/receipts.py:152–155`).
  - Builds `rec = {"seq": seq, "prev": prev, "type": rtype, "body": dict(body or {})}`, sets `rec["h"] = canon.sha256_obj(rec)`, and encodes `line = canon.dumps(rec).encode("utf-8")` (`vmem/receipts.py:157–159`).
  - If `len(line) > self.rec_max` (`65,536`), raises `VqaError("E_BUDGET", {"reason": "journal_record_max", "bytes": len(line)})` (`vmem/receipts.py:160–161`).
  - Selects current segment `cur = segs[-1] if segs else 1`; if `used > 0` and `rotates(used, len(line), self.seg_cap)`, rotates to `cur += 1` (`vmem/receipts.py:162–167`).
  - Opens `self.path(cur)` with `os.O_WRONLY | os.O_CREAT | os.O_APPEND` (`0o644`), writes `line` via `_write_all(fd, line)`, calls `os.fsync(fd)`, and if the segment file was newly created calls `fsync_dir(self.dir)` (`vmem/receipts.py:168–176`).
  - Updates `_tail_cache = (seq + 1, rec["h"])` and `_tail_stamp = self._stamp()`, and returns `rec` (`vmem/receipts.py:177–179`).
- `verify_chain()` (`vmem/receipts.py:181–193`): Initializes `(prev, seq) = (b["h"], b["seq"] + 1) if b else (GENESIS, 1)` where `b = self.base()`. Iterates `self.records()`, skipping records with `b and r.get("seq", 0) <= b["seq"]` (`vmem/receipts.py:186–187`), and verifies `r.get("seq") == seq`, `r.get("prev") == prev`, and `canon.sha256_obj(body) == h` (`vmem/receipts.py:188–192`). Returns `True` if all match, else `False`.

## 6. Storage Tiers and Budgets (`vmem/tiers.py`)

### 6.1 Constants and Functions (`vmem/tiers.py:8–46`)
- `MIB = 1 << 20` (`1,048,576`, `vmem/tiers.py:8`).
- `TIERS = ("L0", "L1", "L2")` (`vmem/tiers.py:9`). (No additional `L0` implementation exists in `vmem/tiers.py`; `L0` implementation details are `ABSENT`.)
- `L1_BASE = "/dev/shm"` (`vmem/tiers.py:10`).
- `DEFAULTS = {"L1_CAP_MIB": 512, "L2_CAP_MIB": 2048, "FG_WARN_PERMILLE": 250, "FG_BLOCK_PERMILLE": 900, "COMPACT_K_CLEAN": 5}` (`vmem/tiers.py:12–13`).
- `DIGEST_MAX_B = 6000` (`vmem/tiers.py:14`).
- `l1_cap_bytes(table=None)` (`vmem/tiers.py:17–18`): Returns `(table or DEFAULTS)["L1_CAP_MIB"] * MIB` (default `536,870,912` B).
- `l2_cap_bytes(table=None)` (`vmem/tiers.py:21–22`): Returns `(table or DEFAULTS)["L2_CAP_MIB"] * MIB` (default `2,147,483,648` B).
- `fg_state(fg_bytes, table=None)` (`vmem/tiers.py:25–33`, `# @anchor:vm.tiers.fg_state`): Computes `cap = l2_cap_bytes(t)` where `t = table or DEFAULTS`. Returns:
  - `"FG_BLOCK"` if `fg_bytes * 1000 > t["FG_BLOCK_PERMILLE"] * cap` (`vmem/tiers.py:29–30`),
  - `"FG_WARN"` if `fg_bytes * 1000 > t["FG_WARN_PERMILLE"] * cap` (`vmem/tiers.py:31–32`),
  - `"OK"` otherwise (`vmem/tiers.py:33`).
- `check_l2(total_bytes, table=None)` (`vmem/tiers.py:36–40`): If `total_bytes > l2_cap_bytes(table)`, raises `VqaError("E_BUDGET", {"reason": "l2_cap", "bytes": total_bytes})`, else returns `True`.
- `check_digest(data)` (`vmem/tiers.py:43–46`): If `len(data) > DIGEST_MAX_B` (`6000`), raises `VqaError("E_BUDGET", {"reason": "digest", "bytes": len(data)})`, else returns `True`.

### 6.2 `L1Stage` (`vmem/tiers.py:49–75`)
- `L1Stage(run_id, base=L1_BASE, cap_bytes=None)` (`vmem/tiers.py:52–56`): Sets `self.path = os.path.join(base, "vmem_" + run_id)`, `self.cap = l1_cap_bytes() if cap_bytes is None else cap_bytes`, `self.used = 0`, and creates `self.path` via `os.makedirs(self.path, exist_ok=True)`.
- `put(name, data)` (`vmem/tiers.py:58–66`):
  - If `"/" in name` or `name in ("", ".", "..")`, raises `VqaError("E_USAGE", {"name": name})` (`vmem/tiers.py:59–60`).
  - If `self.used + len(data) > self.cap`, raises `VqaError("E_BUDGET", {"reason": "l1_cap", "used": self.used, "add": len(data)})` (`vmem/tiers.py:61–62`).
  - Writes `data` to `os.path.join(self.path, name)`, increments `self.used += len(data)`, and returns the file path (`vmem/tiers.py:63–66`).
- `cleanup()` (`vmem/tiers.py:68–69`): Calls `shutil.rmtree(self.path, ignore_errors=True)`.
- `__enter__` / `__exit__` (`vmem/tiers.py:71–75`): Context manager returning `self` and calling `self.cleanup()` on exit.

## 7. Content-Addressed Blobs (`vmem/blobs.py`)

### 7.1 Constants (`vmem/blobs.py:10–11`)
- `_SHA = re.compile(r"^[0-9a-f]{64}$")` (`vmem/blobs.py:10`).
- `EXTS = ("png", "bin", "mp4")` (`vmem/blobs.py:11`).

### 7.2 Functions (`vmem/blobs.py:14–137`)
- `fsync_dir(path)` (`vmem/blobs.py:14–19`): Opens `str(path)` with `os.O_RDONLY`, calls `os.fsync(fd)`, and closes `fd` in `finally`.
- `write_all(fd, data)` (`vmem/blobs.py:22–30`): Writes `memoryview(data)` in a loop via `os.write`; if `written <= 0`, raises `OSError("write made no progress")` (`vmem/blobs.py:28–29`).
- `_check(sha, ext)` (`vmem/blobs.py:33–35`): Requires `isinstance(sha, str)`, `_SHA.match(sha)`, and `ext in EXTS`; otherwise raises `VqaError("E_USAGE", {"reason": "bad_blob_ref", "sha": str(sha)[:70], "ext": str(ext)})`.
- `blob_path(root, sha, ext)` (`vmem/blobs.py:38–41`): Calls `_check(sha, ext)` and returns `os.path.join(str(root), "blobs", "sha256", sha[:2], "%s.%s" % (sha, ext))`.
- `clean_tmps(root)` (`vmem/blobs.py:44–58`): Walks `<root>/blobs/sha256/*`, unlinks any file ending with `".tmp"` (ignoring `FileNotFoundError`), and returns the count `n` of unlinked `.tmp` files.
- `put_blob(root, data, ext="bin", point=None)` (`vmem/blobs.py:61–93`, `# @anchor:vm.blobs.put_blob`):
  1. Computes `sha = sha256_bytes(data)` and `path = blob_path(root, sha, ext)` (`vmem/blobs.py:64–65`).
  2. If `os.path.exists(path)`: verifies existing blob via `verify_blob(root, sha, ext)`; if `False`, raises `VqaError("E_GATE", {"reason": "g_blob_integrity", "sha": sha})`, else returns `sha` (`vmem/blobs.py:66–69`).
  3. Creates parent directory `d`, unlinks any stale `tmp = path + ".tmp"`, and opens `tmp` with `os.O_WRONLY | os.O_CREAT | os.O_TRUNC` mode `0o644` (`vmem/blobs.py:70–77`).
  4. Writes `data` via `write_all(fd, data)`, calls `point("blob_tmp_written")` if `point` is set, calls `os.fchmod(fd, 0o444)`, calls `point("blob_chmod")` if `point` is set, and calls `os.fsync(fd)` (`vmem/blobs.py:78–85`). On any `BaseException`, unlinks `tmp` and re-raises; always closes `fd` in `finally` (`vmem/blobs.py:86–90`).
  5. Atomically renames via `os.replace(tmp, path)`, flushes parent directory via `fsync_dir(d)`, and returns `sha` (`vmem/blobs.py:91–93`).
- `get_blob(root, sha, ext="bin")` (`vmem/blobs.py:96–102`): Validates path via `blob_path(root, sha, ext)` and returns file bytes; on `OSError`, raises `VqaError("E_ID", {"reason": "blob_missing", "sha": sha})`.
- `verify_blob(root, sha, ext="bin")` (`vmem/blobs.py:105–110`): Returns `sha256_bytes(get_blob(root, sha, ext)) == sha`, catching `VqaError` and returning `False`.
- `iter_blobs(root)` (`vmem/blobs.py:113–123`): Scans `<root>/blobs/sha256/*/*`, collects `(stem, ext)` where `_SHA.match(stem)` and `ext in EXTS`, and returns `sorted(out)`.
- `delete_blob(root, sha, ext="bin")` (`vmem/blobs.py:126–133`): Calls `os.unlink(blob_path(root, sha, ext))`; returns `True` if unlinked, `False` on `FileNotFoundError`.
- `is_immutable(root, sha, ext="bin")` (`vmem/blobs.py:136–137`): Returns `not (os.stat(blob_path(root, sha, ext)).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))`.

## 8. Compaction (`vmem/compact.py`)

### 8.1 Constants and Helpers (`vmem/compact.py:9–54`)
- `VM_GEN_KEEP = 4` (`vmem/compact.py:9`).
- `fg_ids_sha256(units)` (`vmem/compact.py:12–15`): Computes `ids_ = sorted(u["id"] for u in units if U.is_fg(u))` and returns `hashlib.sha256("".join(i + "\n" for i in ids_).encode()).hexdigest()`.
- `g_fg_monotone(parent_units, child_units)` (`vmem/compact.py:18–23`): Computes `missing = sorted({u["id"] for u in parent_units if U.is_fg(u)} - {u["id"] for u in child_units if U.is_fg(u)})`. If `missing` is non-empty, raises `VqaError("E_GATE", {"reason": "g_fg_monotone", "missing": missing[:20]})`, else returns `True`.
- `fg_bytes(units, blob_sizes=None)` (`vmem/compact.py:26–37`): Sums `len(U.dump_unit(u).encode("utf-8"))` over all FG units (`U.is_fg(u)`) plus `sizes.get(b, 0)` for each unique blob SHA `b` in `U.blob_refs(u)` across FG units.
- `_run_order(units_)` (`vmem/compact.py:40–46`): Maps each run ID `r = U.unit_run(u)` to `u["scope"]["build_seq"] or 0` and returns `sorted(runs, key=lambda r: (runs[r], r))`.
- `_strip_blobs(u, drop_look=False)` (`vmem/compact.py:49–54`): Returns a shallow copy of `u` where every anchor has `crop=None`, and when `drop_look=True` also sets `desc_blob=None` and `gop_blob=None` on every anchor and `desc_blob=None` on `u["look"]` (if `u["look"]` is truthy).

### 8.2 `compact` and `gc_plan` (`vmem/compact.py:57–130`)
- `compact(units_, blob_sizes=None, k_clean=None, table=None, gen=None)` (`vmem/compact.py:57–115`, `# @anchor:vm.compact.compact`):
  1. Uses `t = table or tiers.DEFAULTS` and `k = t["COMPACT_K_CLEAN"] if k_clean is None else k_clean` (`vmem/compact.py:63–64`).
  2. Checks `tiers.fg_state(fg_bytes(units_, blob_sizes), t) == "FG_BLOCK"`; if `True`, raises `VqaError("E_GATE", {"reason": "fg_cap"})` (`vmem/compact.py:65–66`).
  3. Sets `gen = gen if gen is not None else max([u["last_gen"] for u in units_] or [0]) + 1` (`vmem/compact.py:67`).
  4. Identifies `dirty_runs = {U.unit_run(u) for u in units_ if u["pin"] == "FAIL" and U.unit_run(u)}` (`vmem/compact.py:68`).
  5. Groups `units_` by `U.scope_key(u)`; within each scope, orders runs via `order = _run_order(us)` and filters `clean = [r for r in order if r not in dirty_runs]` (`vmem/compact.py:69–75`). If `len(clean) > k`, sets `cut = clean[-k] if k else None` and adds to `old_runs` all runs `r in order` with `cut is None or order.index(r) < order.index(cut)` (`vmem/compact.py:76–78`).
  6. Partitions each unit `u` in `units_` (`vmem/compact.py:80–90`):
     - If `U.is_fg(u)` or `r not in old_runs` or `u["status"] == "retired":` appends `u` unchanged to `archive` if `u["status"] == "retired"` else to `active` (`vmem/compact.py:82–84`).
     - Else if `u["kind"] == "episode"` and `u["verdict"] != "FAIL"`: appends `u["id"]` to `retired`, appends `dict(_strip_blobs(u, True), status="retired", last_gen=max(gen, u["last_gen"]))` to `archive`, and groups `(u, r)` under `(U.scope_key(u), u["scope"]["build_seq"])` (`vmem/compact.py:85–88`).
     - Otherwise (non-FG, non-episode or FAIL-verdict unit in `old_runs`): appends `_strip_blobs(u)` (dropping `crop` while keeping `desc_blob`) to `active` (`vmem/compact.py:89–90`).
  7. For each `(sk, bs)` group of retired episodes in sorted `(sk, bs or 0)` order, allocates the next `EPI` unit number after the maximum existing `episode` unit ID and creates one active `@unchanged` summary `episode` unit (`predicate="baseline"`, `cls="UNCHANGED"`, `gen=gen`, `value="@unchanged"`, `value_num=len(runs)`, `summary="@unchanged n_runs=%d runs_sha8=%s" % (len(runs), hashlib.sha256("".join(runs).encode()).hexdigest()[:8])`) (`vmem/compact.py:91–105`).
  8. Sorts `active = U.sort_units(active + summaries)` and `archive = U.sort_units(archive)`, computes `live` blob refs across `active` and `dropped_blobs = before - live`, and returns `{"active": active, "archive": archive, "retired": sorted(retired), "summaries": summaries, "live_blobs": live, "dropped_blobs": before - live, "fg_ids_sha256": fg_ids_sha256(active + archive)}` (`vmem/compact.py:106–115`).
- `gc_plan(all_blobs, window_gens, durable=True)` (`vmem/compact.py:118–130`):
  - Materializes `window = list(window_gens)`. If `len(window) > VM_GEN_KEEP` (`4`), raises `VqaError("E_USAGE", {"reason": "window_too_large", "n": len(window)})` (`vmem/compact.py:121–123`).
  - If `not durable`, returns `[]` (`vmem/compact.py:124–125`).
  - Unions `U.blob_refs(u)` across all units in all generations in `window` (`live`) and returns `sorted(set(all_blobs) - live)` (`vmem/compact.py:126–130`).

## 9. Router and Query Ladder (`vmem/router.py`)

### 9.1 Constants in `vmem/router.py`

| Constant | Value | Location |
|---|---|---|
| `SIG_HAM_MAX` | `3` (`U.SIG_HAM_MAX`) | `vmem/router.py:13` |
| `VM_LOOK_SHARE_PPM` | `950000` (`U.VM_LOOK_SHARE_PPM`) | `vmem/router.py:14` |
| `T0_Q4_DEFAULT` | `32` | `vmem/router.py:15` |
| `SHORTLIST_B` | `2000` | `vmem/router.py:16` |
| `ANSWER_SOFT_TOK` | `2520` | `vmem/router.py:17` |
| `ANSWER_HARD_TOK` | `4000` | `vmem/router.py:18` |
| `DIGEST_TOK` | `600` | `vmem/router.py:19` |
| `QUOTE_MIN_CHARS` | `12` | `vmem/router.py:20` |
| `K1_M`, `B_M` | `1200`, `750` | `vmem/router.py:21` |
| `ZONES` | `(("key", 3), ("subject", 2), ("predicate", 2), ("summary", 1), ("tk", 1))` | `vmem/router.py:22` |
| `ABSTAIN` | `{"a": "scope_unknown", "c": "build_out_of_range", "d": "tk_unknown", "e": "sig_no_hit", "f": "no_lexical_hit", "g": "check_failed"}` (letter `"b"` is `ABSENT`) | `vmem/router.py:23–24` |
| `_TOK` | `re.compile(r"[a-z0-9]+")` | `vmem/router.py:25` |
| `_HEX16` | `re.compile(r"^[0-9a-f]{16}$")` | `vmem/router.py:26` |

### 9.2 Token Estimation, Chains, Index Builders, and Integer BM25F (`vmem/router.py:29–130`)
- `est_tokens(nbytes)` (`vmem/router.py:29–31`): Returns `(nbytes + 3) // 4`.
- `_tokens(s)` (`vmem/router.py:34–35`): Returns `_TOK.findall(str(s).lower())`.
- `parse_sig(h)` (`vmem/router.py:38–41`): Requires `isinstance(h, str)` and `_HEX16.match(h)` (else raises `VqaError("E_USAGE", {"reason": "bad_sig", "sig": str(h)[:20]})`), returning `int(h, 16)`.
- `build_sig_index(units)` (`vmem/router.py:45–58`): Collects `(u["id"], a["sig64"])` for all anchors with truthy `"sig64"` on units with `kind in ("defect", "region_event", "baseline")` sorted by `u["id"]` (`vmem/router.py:47–53`). Emits `"index/sig/ids.bin"` as ASCII `"%s\t%s\n"` lines and `"index/sig/b0.bin"`..`"index/sig/b3.bin"` as sorted `<HI` (`band u16`, `row u32`) pairs (`vmem/router.py:54–58`).
- `_bs(u)` (`vmem/router.py:76–78`): Returns `int(u["id"][-6:])` if `u.get("scope", {}).get("build_seq")` is `None`, else `build_seq`.
- `golden_chain(units, tk)` (`vmem/router.py:61–65`): Filters non-retired `baseline` units with `u.get("tk") == tk` and `u.get("pin") == "GOLDEN"`, sorted by `(_bs(u), u["id"])`.
- `appearance_chain(units, tk)` (`vmem/router.py:68–73`): Filters non-retired units with `u.get("tk") == tk` and truthy `u.get("look")` that are either (`pin == "GOLDEN"` and `kind == "baseline"`) or (`kind == "region_event"` and `pin is not None`), sorted by `(_bs(u), u["id"])`.
- `build_tk_index(units)` (`vmem/router.py:81–91`): Maps each `tk` to `{"golden": [u["id"] for u in golden_chain(units, tk)], "last_good": ch[-1]["id"] if ch else None, "units": sorted_unit_ids}`, where `ch` filters `golden_chain(units, tk)` to `u.get("verdict") in (None, "PASS")`, and returns `{"index/tk.json": canon.dumps(out).encode("utf-8")}`.
- `_log2_ppm(num, den)` and `bm25f(units, qterms)` (`vmem/router.py:95–130`):
  - `_log2_ppm(num, den)` computes integer log2 approximation in ppm via bit-length difference `sh = num.bit_length() - den.bit_length()` and linear mantissa `sh * 1000000 + (n - d) * 1000000 // d` (`vmem/router.py:95–101`).
  - `bm25f(units, qterms)` returns `{}` if `not units` or `not qterms` (`vmem/router.py:106–107`). Accumulates weighted term frequencies `tf[t]` and document lengths `dl` across `ZONES` (`key` weight 3, `subject` 2, `predicate` 2, `summary` 1, `tk` 1; `links` are not indexed, `vmem/router.py:22, 109–115`). Sets `avg = max(1, sum(dls.values()) // n)` (`vmem/router.py:117`). For each unique query term `t in set(qterms)` with `df > 0`, computes `idf = _log2_ppm(2 * n - df + 1, df) + 1` and adds `idf * num // den` to `out[uid]` where `num = f * (K1_M + 1000) * 1000 * avg` and `den = f * 1000000 * avg + K1_M * ((1000 - B_M) * avg + B_M * dls[uid])` (`vmem/router.py:119–130`).

### 9.3 `View` and Query Verbs (`vmem/router.py:134–453`)
- `View(root)` (`vmem/router.py:134–169`): Pins `self.snap = Store(root).snapshot()` and `self.gen = self.snap.gen`. Lazily loads `units/active.jsonl` (`View.units`, `vmem/router.py:143–148`), looks up a single active unit by ID (`View.unit(uid)`, raising `VqaError("E_ID", {"reason": "unit_unknown", "id": str(uid)[:20]})` if absent, `vmem/router.py:150–154`), reads `index/tk.json` or rebuilds it on `VqaError` (`View.tk_index()`, `vmem/router.py:156–160`), reads `VISUAL_DIGEST.md` or returns `""` on `VqaError` (`View.digest()`, `vmem/router.py:162–166`), and validates snapshot consistency via `View.finish()` -> `self.snap.validate()` (`vmem/router.py:168–169`).
- `sig_lookup(view, sig, scan=False)` (`vmem/router.py:200–224`, `# @anchor:vm.router.sig_lookup`): Reads `index/sig/ids.bin`. When `scan=False`, binary-searches (`bisect_left`) each of the 4 16-bit bands `b0.bin`..`b3.bin` (by pigeonhole principle, Hamming distance `<= 3` across 64 bits must match at least one 16-bit band exactly) and filters candidates to Hamming distance `d <= SIG_HAM_MAX` (`3`, `vmem/router.py:208–223`). When `scan=True`, scans all rows in `ids.bin` up to `limit = SIG_HAM_MAX + 1` (`4`, `vmem/router.py:204–207`). Keeps the minimum distance per `uid` and returns `sorted((d, uid) for uid, d in hits.items())` (`vmem/router.py:222–224`).
- `route(root, q=None, scope=None, sig=None, tk=None, k=5, scan=False, grid=None, build=None, view=None)` (`vmem/router.py:257–295`, `# @anchor:vm.router.route`):
  - Validates `1 <= k <= 5` (else `E_USAGE` `{"reason": "bad_k", "k": k}`, `vmem/router.py:259–260`) and `q` or `sig is not None` (else `E_USAGE` `{"reason": "q_or_sig_required"}`, `vmem/router.py:261–262`).
  - Checks `_check_grid(v, grid)`: if `grid is not None` and `grids = {u.get("scope", {}).get("grid") for u in v.units}` is non-empty and `grid not in grids`, raises `VqaError("E_STALE", {"reason": "grid_mismatch", "grid": grid})` (`vmem/router.py:180–185, 264`).
  - Starts from non-retired active units `pool = [u for u in v.units if u.get("status") != "retired"]` and applies filters in order, raising `NOT_IN_VISUAL_MEMORY` (`_abstain`) on the first empty stage (`vmem/router.py:265–290`):
    1. `scope`: filters by `_scope_match(u, scope)` (`app` or `app/scene`); if empty, abstains with `"a"` (`"scope_unknown"`, `vmem/router.py:172–177, 266–269`).
    2. `build`: if `build is not None`, requires `pool` non-empty and `min(bs) <= build <= max(bs)` where `bs = [_bs(u) for u in pool]`; else abstains with `"c"` (`"build_out_of_range"`, `vmem/router.py:270–273`).
    3. `tk`: if truthy, requires `tk in v.tk_index()` and `any(u["tk"] == tk for u in pool)`; else abstains with `"d"` (`"tk_unknown"`, `vmem/router.py:274–277`).
    4. `sig`: if `sig is not None`, intersects `pool` with `sig_lookup(v, sig, scan)`; if empty, abstains with `"e"` (`"sig_no_hit"`), else scores `1000000 - hits[u["id"]] * 100000` (`vmem/router.py:279–284`).
    5. `q`: if truthy, computes `lex = bm25f(pool, _tokens(q))`; if empty, abstains with `"f"` (`"no_lexical_hit"`), else adds `lex[uid]` to `scores` and filters `pool` to `u["id"] in lex` (`vmem/router.py:285–290`).
  - Sorts `pool` by `(-scores[u["id"]], u["id"])`, formats rows `[u["id"], score, u["tk"], u.get("summary", "")]`, fits at most `k` rows into `SHORTLIST_B` (`2000` canonical bytes) via `_fit` (dropping tail rows and recording `"overflow"`, or raising `VqaError("E_BUDGET", {"reason": "shortlist_bytes", "cap": 2000})` if even 1 row exceeds `2000` B), sets `out["gen"] = v.gen`, calls `v.finish()`, and returns `out` (`vmem/router.py:239–254, 291–295`).
- `get(root, uid, view=None)` (`vmem/router.py:298–302`): Reads `u = v.unit(uid)`, calls `v.finish()`, and returns `u`.
- `evidence(root, uid, view=None)` (`vmem/router.py:305–319`): Reads `u = v.unit(uid)`, attaches `paths={"crop": blob_path(root, a["crop"], "png"), "desc_blob": blob_path(root, a["desc_blob"], "bin")}` (for present refs) to each anchor copy, calls `v.finish()`, and returns `{"id": uid, "anchors": anchors}`.
- `last_good(root, tk=None, subject=None, scope=None, view=None)` (`vmem/router.py:327–345`): Requires `(tk is None) != (subject is None)` (else `E_USAGE` `{"reason": "tk_xor_subject"}`, `vmem/router.py:329–330`). Filters by `scope` (abstaining with `"a"` if `scope` is set and `pool` is empty, `vmem/router.py:331–333`), resolves `tks` from `tk` or `u.get("subject") == subject` (abstaining with `"d"` if no matching `tk` or no good `golden_chain` unit with `_is_good(u)` exists, `vmem/router.py:322–324, 334–341`), picks `best = max(cands, key=lambda u: (_bs(u), u["id"]))`, calls `v.finish()`, and returns `{"unit_id": best["id"], "tk": best["tk"], "build_seq": best["scope"].get("build_seq"), "cite": best["anchors"][0]["cite"] if best.get("anchors") else None, "summary": best.get("summary", "")}` (`vmem/router.py:342–345`).
- `diff_region(root, tk, from_build=None, to_build=None, view=None)` (`vmem/router.py:348–382`):
  - Filters `pool = [u for u in v.units if u["tk"] == tk]` and `chain = appearance_chain(pool, tk)`; if either is empty, abstains with `"d"` (`vmem/router.py:351–356`).
  - Computes `keys = [_bs(u) for u in chain]`, `lo_b = keys[0] if from_build is None else from_build`, `hi_b = keys[-1] if to_build is None else to_build`. If `lo_b > hi_b` or `hi_b < keys[0]` or `lo_b > keys[-1]`, abstains with `"c"` (`vmem/router.py:357–361`).
  - Finds `base = max(0, bisect.bisect_right(keys, lo_b) - 1)` and `nxt = bisect.bisect_right(keys, lo_b)` (`vmem/router.py:362–363`).
  - Chooses result key `fkey = "first_run"` if `chain[0]["scope"].get("build_src") == "ingest_order"` or `chain[0]["scope"].get("build_seq") is None`, else `"first_change"` (`vmem/router.py:364–366`).
  - Scans `i` from `nxt` while `keys[i] <= hi_b`; at the first `i` where `look_eq(root, chain[base]["look"], chain[i]["look"]) == "DIFFERENT"`, sets `res[fkey] = {"build_seq": ..., "run_id": ..., "cite": ..., "ah64_old": ..., "ah64_new": ..., "class": ..., "look_eq": "DIFFERENT", "pin": ...}` and breaks (`vmem/router.py:367–377`).
  - Sets `res["granularity"] = "frame"` if all anchors across `chain` have `render_index is not None`, else `"keyframe"`, calls `v.finish()`, and returns `res` (`vmem/router.py:379–382`).
- `seen_defect(root, sig, cls=None, scope=None, view=None)` (`vmem/router.py:385–396`): Looks up `(d, uid)` in `sig_lookup(v, sig)`, keeps units with `layer == "FG"`, `kind in ("defect", "verdict")`, `_scope_match(u, scope)`, and `cls is None or u.get("class") == cls`. If empty, abstains with `"e"`, else calls `v.finish()` and returns `{"hits": [{"unit_id": uid, "dist": d} for d, uid in sorted(hits)]}`.
- `check_answer(root, ans, view=None)` (`vmem/router.py:399–416`): For each citation `c` in `ans.get("citations", ())`:
  - Resolves `u = v.unit(c.get("unit_id"))` (raising `VqaError("E_CITE", {"reason": "unit_unresolved", "unit_id": str(c.get("unit_id"))[:20]})` if missing, `vmem/router.py:404–407`).
  - Verifies `c.get("cite")` is in `{a.get("cite") for a in u.get("anchors", ())}` (else raises `VqaError("E_CITE", {"reason": "cite_unresolved", "unit_id": u["id"]})`, `vmem/router.py:408–410`).
  - Concatenates `text = "\n".join(str(u.get(f, "")) for f in ("key", "subject", "summary", "value"))`; if `len(quote) < QUOTE_MIN_CHARS` (`12`) or `quote not in text`, raises `_abstain("g", unit_id=u["id"], quote_len=len(quote))` (`NOT_IN_VISUAL_MEMORY`, `rc 3`, `vmem/router.py:411–414`).
  - Calls `v.finish()` and returns `{"checked": len(ans.get("citations", ())), "gen": v.gen}` (`vmem/router.py:415–416`).
- `answer(root, q=None, scope=None, sig=None, tk=None, max_tok=ANSWER_SOFT_TOK, grid=None)` (`vmem/router.py:419–453`, `# @anchor:vm.router.answer`):
  - Runs `sl = route(root, q=q, scope=scope, sig=sig, tk=tk, grid=grid, view=v)` and selects `top = v.unit(sl["r"][0][0])` (`vmem/router.py:422–424`).
  - Computes token costs for ladder levels `levels = [("V0", dtok), ("V1", stok), ("V3", utok), ("V4", ctok)]` where `dtok = min(DIGEST_TOK, est_tokens(len(digest.encode("utf-8"))))`, `stok = est_tokens(len(canon.dumps(sl).encode("utf-8")))`, `utok = est_tokens(len(canon.dumps(top).encode("utf-8")))`, and `ctok = 260 * len(crops)` for up to 2 crops on `top` (level `"V2"` is `ABSENT`, `vmem/router.py:425–431`).
  - Drops levels from the bottom (`V4`, then `V3`) while `total > max_tok and len(kept) > 2` (`vmem/router.py:432–436`). If `total > ANSWER_HARD_TOK` (`4000`), raises `VqaError("E_BUDGET", {"reason": "answer_tokens", "tok": total, "cap": ANSWER_HARD_TOK})` (`vmem/router.py:437–438`).
  - Builds `ans` (`schema="vmem.answer/v1"`, `status="answered"`, `citations=[{"unit_id": top["id"], "cite": cite, "quote": top.get("summary", "")}]`, `budget_tok=total`, `levels=kept`, `code="ANSWERED"`, `rc=0`, `advisory={"abstain": False, "cov_ppm": 0}`) and verifies it via `check_answer(root, ans, view=v)` (converting any `rc == 3` `VqaError` into `_abstain("g", unit_id=top["id"])`), calls `v.finish()`, and returns `ans` (`vmem/router.py:439–453`).

## 10. Verification Gates (`vmem/gates.py`)

### 10.1 Constants (`vmem/gates.py:13–23`)
- `UNIT_MAX_B = 1024` (`vmem/gates.py:13`), `DIGEST_MAX_B = 6000` (`vmem/gates.py:14`), `L2_CAP_MIB = 2048` (`vmem/gates.py:15`), `FG_BLOCK_PERMILLE = 900` (`vmem/gates.py:16`).
- `UNIT_REQUIRED = ("schema", "id", "key", "kind", "layer", "pin", "status", "scope", "tk", "predicate", "anchors")` (`vmem/gates.py:17`).
- `KINDS = ("baseline", "region_event", "episode", "verdict", "defect")` (`vmem/gates.py:18`).
- `PINS = ("FAIL", "GOLDEN", "CATALOGUED", None)` (`vmem/gates.py:19`).
- `GATES = ("g_schema", "g_nofloat", "g_ids", "g_layer_pin", "g_fg_monotone", "g_catalogue", "g_blob_integrity", "g_provenance", "g_cite", "g_grid", "g_caps", "g_window", "g_privacy")` (`vmem/gates.py:20–21`).
- `ABSENT = ("g_determinism", "g_cache_stable")` (`vmem/gates.py:22`; docstring at `vmem/gates.py:3` notes they need an ingest run and are reported under `absent`).

### 10.2 Gate Definitions (`vmem/gates.py:34–185`)

| Gate Name | Rule Checked (`c["units"]` = `active + archive` of target generation `n`) | Location |
|---|---|---|
| `g_schema` | Every unit in `c["units"]` has all keys in `UNIT_REQUIRED`, `schema == "vmem.unit/v1"`, `kind in KINDS`, `layer in ("FG", "BG")`, `pin in PINS`, and passes `U.validate_unit(u)`. | `vmem/gates.py:34–40` |
| `g_nofloat` | `_units(c["snap"], rel)` succeeds without `VqaError` (`canon.loads` rejects floats) for both `"units/active.jsonl"` and `"units/archive.jsonl"`. Also checked when loading `units` in `verify` (`vmem/gates.py:160–162`). | `vmem/gates.py:43–49, 160–162` |
| `g_ids` | Every `u` in `c["units"]` has `ids.parse_any(u["id"])[0] == "unit"`, `ids.parse_any(u["tk"])[0] == "track_key"`, and every anchor `a` has `ids.parse_any(a["cite"])[0] == "cross_cite"` without raising `VqaError`. | `vmem/gates.py:52–62` |
| `g_layer_pin` | `all((u["layer"] == "FG") == (u["pin"] is not None) for u in c["units"])`. | `vmem/gates.py:65–66` |
| `g_fg_monotone` | True if `c["parent"] is None`; otherwise `{u["id"] for u in c["parent_units"] if u["layer"] == "FG"} <= {u["id"] for u in c["units"] if u["layer"] == "FG"}`. | `vmem/gates.py:69–73` |
| `g_catalogue` | Reads `catalogue/foreground.tsv` (if missing, passes iff no unit has `layer == "FG"`), extracts first tab column of lines after header, and checks `got == sorted(u["id"] for u in c["units"] if u["layer"] == "FG")`. | `vmem/gates.py:76–82` |
| `g_blob_integrity` | Checks `U.blob_refs(u) <= have` for all `u` in `c["units"]` where `have = {s for s, _ in iter_blobs(c["root"])}`, then verifies `verify_blob(c["root"], s, e)` for all blobs (or `items[:64]` when `c["quick"]` is `True`). | `vmem/gates.py:85–92` |
| `g_provenance` | `all(isinstance(u.get("receipt_sha256"), str) and _SHA.match(u["receipt_sha256"]) for u in c["units"])`. | `vmem/gates.py:95–96` |
| `g_cite` | Every `u` with `kind in ("region_event", "defect", "verdict")` and `layer == "FG"` has non-empty `u.get("anchors")`, and every anchor `a` on any unit has `a.get("run_id") is not None` and `str(a.get("cite", "")).startswith(a["run_id"] + "/")`. | `vmem/gates.py:99–106` |
| `g_grid` | For every `u` in `c["units"]`, if `u.get("look")` is not `None`, `lk.get("grid") == u.get("scope", {}).get("grid")`. | `vmem/gates.py:109–115` |
| `g_caps` | Every unit has `len(canon.dumps(u).encode("utf-8")) <= UNIT_MAX_B` (`1024`), `VISUAL_DIGEST.md` (if present) has `<= DIGEST_MAX_B` (`6000`) bytes, `fg = c["snap"].manifest.get("fg_blob_bytes")` is an `int` (not `bool`), and `fg * 1000 <= FG_BLOCK_PERMILLE * L2_CAP_MIB * 1048576`. | `vmem/gates.py:118–129` |
| `g_window` | For every generation `n in G.list_gens(root)` with `cur - G.VM_GEN_KEEP + 1 <= n <= cur`, every unit `u in st.gen_units(n)` satisfies `U.blob_refs(u) <= have`. | `vmem/gates.py:132–142` |
| `g_privacy` | Always returns `True` (`# deferred (U-DEC-0005)`). | `vmem/gates.py:145–146` |
| `g_determinism`, `g_cache_stable` | `ABSENT` (not evaluated in `vmem/gates.py`; returned in the `"absent"` list of `verify`). | `vmem/gates.py:3, 22, 180` |

- `verify(root, quick=False, gen=None)` (`vmem/gates.py:149–181`, `# @anchor:vm.gates.verify`):
  - Takes `snap = Store(root).snapshot()` (`vmem/gates.py:151–152`). If `gen is not None`: checks `G.in_window(root, gen)` (raising `VqaError("E_ID", {"reason": "gen_outside_window", "gen": gen})` if `False`, `vmem/gates.py:154–155`) and opens `Snapshot(store, gen)` if `gen != snap.gen` (`vmem/gates.py:156–158`).
  - Loads `units = _units(snap) + _units(snap, "units/archive.jsonl")`; if `VqaError` is raised during loading, raises `VqaError("E_GATE", {"reason": "g_nofloat" if e.code == "E_FLOAT" else "g_schema", "cause": e.code})` (`vmem/gates.py:159–162`).
  - Loads `parent_units` if `parent = snap.manifest.get("parent_gen")` is in `G.list_gens(root)` (`vmem/gates.py:163–168`).
  - Evaluates each gate in `GATES`, mapping `True -> 0` and `False` (or `(VqaError, KeyError, TypeError, AttributeError)`) `-> 5` (`vmem/gates.py:172–177`).
  - Calls `snap.validate()` and returns `{"gates": out, "absent": list(ABSENT), "gen": n, "fg_count": len(fg), "fg_ids_sha256": C.fg_ids_sha256(units)}` (`vmem/gates.py:178–181`).
- `failed(res)` (`vmem/gates.py:184–185`): Returns `sorted(k for k, v in res["gates"].items() if v != 0)`.

## 11. CLI Specification (`vmem/cli.py`)

### 11.1 Argument Parser (`_Parser`, `_parser`, `vmem/cli.py:12–50`)
- Program name: `vmem` (`vmem/cli.py:27`).
- `_Parser.error(message)` raises `VqaError("E_USAGE", {"reason": "argparse", "msg": message[:120]})` (`vmem/cli.py:17–18`).
- `_Parser.exit(status=0, message=None)` raises `VqaError("E_USAGE", {"reason": "argparse_exit"})` when `status` is non-zero, or `SystemExit(0)` when `status == 0` (caught in `main` to return `0` without writing stdout, `vmem/cli.py:20–23, 105–106`).
- Store root resolution (`vmem/cli.py:60–61`): `root = a.store or os.environ.get("VMEM_ROOT")`; if `None`, `_need(root, "--store")` raises `VqaError("E_USAGE", {"reason": "missing_arg", "arg": "--store"})` (`vmem/cli.py:53–56, 61`).
- Signature parsing (`vmem/cli.py:62`): If `a.sig` is truthy, calls `router.parse_sig(a.sig)` before dispatching the verb (raising `E_USAGE` `{"reason": "bad_sig", ...}` if not 16 lowercase hex chars, `vmem/router.py:38–41`).
- Output wrapping (`vmem/cli.py:100–108`): On success, `main` wraps `_run(a)` via `result_obj("OK", 0, _run(a))` (`rc = 0`); on `VqaError as e`, sets `out = e.to_obj()`; writes `result_line(out)` to `sys.stdout` and returns `out["rc"]` (`vmem/cli.py:102–108`).

| CLI Argument | `argparse` Configuration | Default | Location |
|---|---|---|---|
| `verb` | Positional, `choices=VERBS` (14 verbs) | Required | `vmem/cli.py:12–13, 28` |
| `id` | Positional, `nargs="?"` | `None` | `vmem/cli.py:29` |
| `--store` | Optional `str` (falls back to `os.environ.get("VMEM_ROOT")`) | `None` | `vmem/cli.py:30, 60–61` |
| `--q` | Optional `str` | `None` | `vmem/cli.py:31` |
| `--scope` | Optional `str` | `None` | `vmem/cli.py:32` |
| `--sig` | Optional `str` (parsed via `router.parse_sig` if truthy) | `None` | `vmem/cli.py:33, 62` |
| `--tk` | Optional `str` | `None` | `vmem/cli.py:34` |
| `--subject` | Optional `str` | `None` | `vmem/cli.py:35` |
| `--class` | Optional `str`, `dest="cls"` | `None` | `vmem/cli.py:36` |
| `--grid` | Optional `str` | `None` | `vmem/cli.py:37` |
| `--build` | Optional `type=int` | `None` | `vmem/cli.py:38` |
| `--k` | Optional `type=int` | `5` | `vmem/cli.py:39` |
| `--scan` | Optional `action="store_true"` | `False` | `vmem/cli.py:40` |
| `--from-build` | Optional `type=int`, `dest="from_build"` | `None` | `vmem/cli.py:41` |
| `--to-build` | Optional `type=int`, `dest="to_build"` | `None` | `vmem/cli.py:42` |
| `--max-tok` | Optional `type=int`, `dest="max_tok"` | `2520` (`router.ANSWER_SOFT_TOK`) | `vmem/cli.py:43`, `vmem/router.py:17` |
| `--answer` | Optional `str` (file path) | `None` | `vmem/cli.py:44` |
| `--quick` | Optional `action="store_true"` | `False` | `vmem/cli.py:45` |
| `--gen` | Optional `type=int` | `None` | `vmem/cli.py:46` |
| `--to` | Optional `type=int` | `None` | `vmem/cli.py:47` |
| `--receipt` | Optional `str` (file path) | `None` | `vmem/cli.py:48` |
| `--spool` | Optional `str` (file path) | `None` | `vmem/cli.py:49` |

### 11.2 Subcommands, Arguments, and Exit Codes (`vmem/cli.py:59–97`)

| Subcommand (`verb`) | Required Arguments | Optional Arguments Used | Target Function (`vmem/cli.py` line) | Exit Codes (`rc`) & Error Codes |
|---|---|---|---|---|
| `route` | `--store` (or `VMEM_ROOT`), and at least one of `--q` or `--sig` (`vmem/router.py:261–262`) | `--scope`, `--tk`, `--k` (`1..5`, default `5`), `--scan`, `--grid`, `--build` | `router.route` (`vmem/cli.py:64–65`) | `0` (`OK`); `2` (`E_USAGE`: missing `--store`, neither `--q` nor `--sig`, `--k` outside `1..5`, bad `--sig`); `3` (`NOT_IN_VISUAL_MEMORY`: abstentions `a`, `c`, `d`, `e`, `f`; `E_ID`: missing generation file); `4` (`E_STALE`: `empty_store`, `grid_mismatch`, `current_changed`); `6` (`E_BUDGET`: `shortlist_bytes` > `2000` B) |
| `answer` | `--store` (or `VMEM_ROOT`), and at least one of `--q` or `--sig` (`vmem/router.py:261–262, 423`) | `--scope`, `--tk`, `--max-tok` (default `2520`), `--grid` | `router.answer` (`vmem/cli.py:66–67`) | `0` (`OK`); `2` (`E_USAGE`: missing `--store`, neither `--q` nor `--sig`, bad `--sig`); `3` (`NOT_IN_VISUAL_MEMORY`: abstentions `a`, `c`, `d`, `e`, `f`, or `g` when `check_answer` fails with `rc == 3`; `E_ID`: missing file/unit); `4` (`E_STALE`: `empty_store`, `grid_mismatch`, `current_changed`); `6` (`E_BUDGET`: `shortlist_bytes` > `2000` B or `answer_tokens` > `4000` tok) |
| `get` | `--store` (or `VMEM_ROOT`), positional `id` (`_need(a.id, "ID")`) | None | `router.get` (`vmem/cli.py:68–69`) | `0` (`OK`); `2` (`E_USAGE`: missing `--store` or `ID`, or bad `--sig` if passed); `3` (`E_ID`: `unit_unknown` or `file_missing`); `4` (`E_STALE`: `empty_store`, `current_changed`) |
| `evidence` | `--store` (or `VMEM_ROOT`), positional `id` (`_need(a.id, "ID")`) | None | `router.evidence` (`vmem/cli.py:70–71`) | `0` (`OK`); `2` (`E_USAGE`: missing `--store` or `ID`, bad `--sig` or `bad_blob_ref`); `3` (`E_ID`: `unit_unknown` or `file_missing`); `4` (`E_STALE`: `empty_store`, `current_changed`) |
| `last-good` | `--store` (or `VMEM_ROOT`), and exactly one of `--tk` or `--subject` (`tk_xor_subject`, `vmem/router.py:329–330`) | `--scope` | `router.last_good` (`vmem/cli.py:72–73`) | `0` (`OK`); `2` (`E_USAGE`: missing `--store`, `tk_xor_subject`, bad `--sig`); `3` (`NOT_IN_VISUAL_MEMORY`: abstentions `a` or `d`; `E_ID`: `file_missing`); `4` (`E_STALE`: `empty_store`, `current_changed`) |
| `diff-region` | `--store` (or `VMEM_ROOT`), `--tk` (`_need(a.tk, "--tk")`) | `--from-build`, `--to-build` | `router.diff_region` (`vmem/cli.py:74–75`) | `0` (`OK`); `2` (`E_USAGE`: missing `--store` or `--tk`, bad `--sig`); `3` (`NOT_IN_VISUAL_MEMORY`: abstentions `c` or `d`; `E_ID`: `file_missing` or `blob_missing`); `4` (`E_STALE`: `empty_store`, `grid_mismatch` in `look_eq`, `current_changed`) |
| `seen-defect` | `--store` (or `VMEM_ROOT`), `--sig` (`_need(sig, "--sig")`) | `--class` (`a.cls`), `--scope` | `router.seen_defect` (`vmem/cli.py:76–77`) | `0` (`OK`); `2` (`E_USAGE`: missing `--store` or `--sig`, `bad_sig`); `3` (`NOT_IN_VISUAL_MEMORY`: abstention `e`; `E_ID`: `file_missing` or `unit_unknown`); `4` (`E_STALE`: `empty_store`, `current_changed`) |
| `check-answer` | `--store` (or `VMEM_ROOT`), `--answer` (`_need(a.answer, "--answer")`) | None | `router.check_answer` (`vmem/cli.py:78–84`) | `0` (`OK`); `2` (`E_USAGE`: missing `--store` or `--answer`; `E_IO`: `answer_unreadable`; `E_SCHEMA`/`E_FLOAT` from `canon.loads`); `3` (`E_CITE`: `unit_unresolved`, `cite_unresolved`; `NOT_IN_VISUAL_MEMORY`: abstention `g` `check_failed`; `E_ID`: `file_missing`); `4` (`E_STALE`: `empty_store`, `current_changed`) |
| `verify` | `--store` (or `VMEM_ROOT`) | `--quick`, `--gen` | `gates.verify` + `gates.failed` (`vmem/cli.py:85–90`) | `0` (`OK`: all 13 gates in `GATES` returned `0`); `2` (`E_USAGE`: missing `--store`, bad `--sig`); `3` (`E_ID`: `gen_outside_window`, `file_missing`); `4` (`E_STALE`: `empty_store`, `current_changed`); `5` (`E_GATE`: `g_nofloat`/`g_schema` on unit load, or any gate in `GATES` returned `5` with `{"reason": bad[0], "failed": bad, "gates": res["gates"]}`) |
| `rollback` | `--store` (or `VMEM_ROOT`), `--to` (`_need(a.to, "--to")`) | None | `Store.rollback` (`vmem/cli.py:91–94`) | `0` (`OK`: returns `{"gen": to}`); `2` (`E_USAGE`: missing `--store` or `--to`; `E_IO`: `journal_short_write`); `3` (`E_ID`: `gen_outside_window`); `4` (`E_STALE`: `lock_held`); `6` (`E_BUDGET`: `journal_record_max`) |
| `ingest` | `--store` (or `VMEM_ROOT`), `--spool` (`need(a.spool, "--spool")`, `vmem/ingest.py:384–385`) | None | `ingest.ingest` via `ingest.run_verb` (`vmem/cli.py:95–96`, `vmem/ingest.py:181–333, 384–385`) | `0` (`OK`); `2` (`E_USAGE`, `E_SCHEMA`, `E_IO`: `spool_unreadable` or `journal_short_write`); `3` (`E_ID`, `E_VC_ID_RANGE`); `4` (`E_STALE`: `lock_held`, `grid_mismatch`); `5` (`E_GATE`: `g_blob_integrity`, `g_fg_monotone`, `g_grid`, `g_cite`, `g_catalogue`, or `gen_exhausted`); `6` (`E_BUDGET`: `fg_episode_budget`, `UNIT_MAX_B`, `journal_record_max`) |
| `compact` | `--store` (or `VMEM_ROOT`) | None | `ingest.compact_run` via `ingest.run_verb` (`vmem/cli.py:95–96`, `vmem/ingest.py:340–349, 386–387`) | `0` (`OK`); `2` (`E_USAGE`, `E_SCHEMA`, `E_IO`); `3` (`E_ID`); `4` (`E_STALE`: `lock_held`); `5` (`E_GATE`: `fg_cap` when `fg_state == "FG_BLOCK"`, or commit gates); `6` (`E_BUDGET`: `UNIT_MAX_B`, `journal_record_max`) |
| `gc` | `--store` (or `VMEM_ROOT`) | None | `ingest.gc_run` via `ingest.run_verb` (`vmem/cli.py:95–96`, `vmem/ingest.py:352–357, 388–389`) | `0` (`OK`); `2` (`E_USAGE`, `E_IO`); `4` (`E_STALE`: `lock_held`); `5` (`E_GATE`: `g_window` `flip_unacknowledged` or `malformed_unit`); `6` (`E_BUDGET`: `journal_record_max`) |
| `pin` | `--store` (or `VMEM_ROOT`), positional `id` (`need(a.id, "ID")`), `--receipt` (`need(a.receipt, "--receipt")`, `vmem/ingest.py:390`) | None | `ingest.pin_run` via `ingest.run_verb` (`vmem/cli.py:95–96`, `vmem/ingest.py:360–376, 390`) | `0` (`OK`); `2` (`E_USAGE`: missing `--store`, `ID`, or `--receipt`; `E_IO`: `receipt_unreadable`, `journal_short_write`; `E_SCHEMA`); `3` (`E_ID`: `unit_not_found`); `4` (`E_STALE`: `lock_held`); `5` (`E_GATE`: commit gates); `6` (`E_BUDGET`: `UNIT_MAX_B`, `journal_record_max`) |

## 12. Items Marked `ABSENT` in the Bound Source Files

The following modules, functions, constants, and behaviors are referenced by `vmem/*.py` or `AGENTS.md` but are not defined within the 11 bound source files at `bridge/integration`:
1. **`vmem/gen.py` internal implementation (`from . import gen as G` at `vmem/store.py:9`, `vmem/gates.py:8`):**
   - Exact implementation of `G.read_manifest(root, n)`, `G.manifest_sha(root, n)`, `G.gen_dir(root, n)`, `G.current_gen(root)`, `G.cleanup_unacked(root, acked)`, `G.list_gens(root)`, `G.gen_name(n)`, `G.publish_gen(root, n, cur, files, extra, point)`, `G.flip(root, n, point)`, `G.in_window(root, to)`, `G.delete_gen(root, n)`, `G.POINTS`, `G.VM_GEN_KEEP`, and `GEN_MAX` (`ABSENT`).
2. **`vmem/__init__.py`:** Not part of the bound source files (`ABSENT`).
3. **`visioncore` modules (`canon`, `errors`, `ids`, `layout`, `schemas`):**
   - Internal implementations of `canon.loads`, `canon.dumps`, `canon.sha256_bytes`, `canon.sha256_obj` (`ABSENT`).
   - Internal implementation of `VqaError`, `result_obj`, `result_line`, and the complete `code -> rc` mapping dictionary in `visioncore/errors.py` (`ABSENT`).
   - Exact regex definitions and binary/string parsers inside `visioncore/ids.py` (`parse_any`, `track_key`, `unit_id`, `region_id`, `cross_cite`, `global_id`) (`ABSENT`).
   - Exact constant definitions inside `visioncore/layout.py` (`UNIT_LAYERS`, `UNIT_PINS`, `CLS`, `VERDICTS`, `TILE_SIZE`, `TILE_FIELDS`, `TILE_FMT`, `unpack_tile`, `grid_params`, `unmorton`) (`ABSENT`).
4. **Tier `L0` implementation in `vmem/tiers.py`:** `TIERS = ("L0", "L1", "L2")` is declared at `vmem/tiers.py:9`, but no `L0` cap, path, or staging class is defined in `vmem/tiers.py` (`ABSENT`).
5. **Router abstention letter `"b"` and answer ladder level `"V2"` in `vmem/router.py`:**
   - `ABSTAIN` at `vmem/router.py:23–24` defines keys `"a"`, `"c"`, `"d"`, `"e"`, `"f"`, `"g"`; letter `"b"` is `ABSENT`.
   - `answer` at `vmem/router.py:431` defines `levels = [("V0", dtok), ("V1", stok), ("V3", utok), ("V4", ctok)]`; level `"V2"` is `ABSENT`.
6. **Verification gates `g_determinism`, `g_cache_stable`, and `g_privacy` enforcement (`vmem/gates.py`):**
   - `g_determinism` and `g_cache_stable` are listed in `ABSENT = ("g_determinism", "g_cache_stable")` (`vmem/gates.py:3, 22, 180`).
   - `_g_privacy(c)` is a no-op hook that unconditionally returns `True` (`# deferred (U-DEC-0005)`, `vmem/gates.py:3, 145–146`).
