# ref_rewind_ckpt: checkpoint store, mark/restore scripts, digest v2 (unit R1)

Track P, declared non-Gemini product work. Written by claude-fable-5-2. Stdlib Python only.
Plan: `rw-build.md` §3 "R1". Contract: `../ref_rewind/CONTRACT.md` (`rw1`, imported, not redefined). Base: `../ref_broker` @ `b255057`, not edited.

| File | Role |
|---|---|
| `digest.py` | `digest_v2(bpy)` → `col:<Collection>`, `mat:<Material>`, `render`, `ui:view3d`. Superset of the F1 digest: adds local transform, parent, visibility, mesh vertex coordinates, modifiers, material settings + node values + links, render settings, first 3D view. |
| `ops.py` | `mark_script(point_path)`, `restore_script(point_path, load_ui=True, sha256=None)`, `digest_script()`: bpy scripts as text, self-contained (digest.py embedded), last stdout line `RW_RESULT {json}`. They use the `bpy` of the bridge job namespace. |
| `store.py` | `Store(rt, slots=8, budget_bytes=None)`: ring under `RT/ckpt/`, `manifest.jsonl`, dedup, byte budget, stall guard, sha256 check in `verify`. Same interface as `fakes.FakeStore` plus `dedup_candidate`, `files`, `bytes_used`. |
| `test_ckpt.py` | 38 tests, no Blender (fake bpy; real F1 bridge core + F1 broker for the script round trips). |
| `ckpt_live.py` | Spike S1 + live proof against a real Blender 4.0.2 under a vision job. |
| `evidence/` | Output of the live run: `live_s1.json`, `live_s1.log`, `manifest.jsonl`, 4 frames. No `events.jsonl`: the run dispatches control jobs directly and appends no records (that is R2). |

## Use (what R2/R4 call)

```python
import ops; from store import Store; import schema as RW
st = Store(rt)
r = RW.dispatch_control(broker, RW.control_job_id("mark", "p3"), ops.mark_script(st.path_for("p3")))
rec = st.admit("p3", tick, scene_epoch, r["result"]["digest"], frame, pinned=(last_good, open_txn_base))  # rec:ckpt
path = st.verify("p3")                                    # CKPT_CORRUPT / CKPT_MISSING before every restore
r = RW.dispatch_control(broker, RW.control_job_id("restore", "p3", "e%d" % epoch_after),
                        ops.restore_script(path, True, sha256=st.get("p3")["blend_sha256"]))
ok = r["ok"] and RW.digest_match(r["result"]["digest"], st.get("p3")["digest"])
```

- `st.dedup_candidate(digest)` returns the newest point when its digest equals `digest`; `admit` then needs no file and returns a row with `dedup_of`.
- `pinned`: points that must survive (last_good, base of an open transaction). With `pinned=()` the newest older point is treated as last_good. Points newer than the newest pin are unconfirmed and never evicted; when no room can be made `admit` raises `CKPT_BUDGET` and deletes the new, unadmitted file.
- `load_ui=True` is required for a camera GUI turn to be inside the rollback domain (measured below).

## Run

```sh
cd product/ref_rewind_ckpt && python3 -m unittest -v test_ckpt
# live (fresh RT; XDG_CONFIG_HOME keeps Blender's recent-files list inside RT):
RT=/dev/shm/vqa-cu/ref-rw1; N=<nonce>; V="python3 -I tools/vis-skill/vision"
cd /tmp && $V start --job vprw1 --size 1920x1080 --mem 8G --wait-title Blender \
  --app "env VP_SWARM_RT=$RT VP_SWARM_NONCE=$N XDG_CONFIG_HOME=$RT/xdg blender --factory-startup --python <repo>/product/ref_broker/bridge.py"
VP_SWARM_NONCE=$N python3 ckpt_live.py --job vprw1 --rt $RT --evidence evidence ; $V stop --job vprw1
```

## Verified

Unit tests: 38/38 pass [MEASURED 2026-11-14]. Negative controls (same check body run on a mutant, must raise):

| Test | Check removed | Goes red |
|---|---|---|
| `test_nc_sha_check_removed_goes_red` | `Store(check_sha=False)` | 1-byte tamper no longer refused |
| `test_nc_stall_guard_removed_goes_red` | `Store(stall_guard=False)` | unconfirmed point overwritten instead of `CKPT_BUDGET` |
| `test_nc_unpinned_base_goes_red` | caller omits the open-transaction base from `pinned` | base evicted |
| `test_nc_noop_restore_goes_red` | restore script that loads nothing | digest ≠ point |
| `test_nc_blender_side_sha_removed_goes_red` | restore script without `sha256` | swapped file is loaded |
| `test_nc_non_persistent_bridge_goes_red` | bridge timer registered without `persistent=True` | no answer after the load (rw-build §7, L16) |
| `test_nc_view3d_removed_goes_red` | digest without `ui:view3d` | view change invisible |

Live, Blender 4.0.2 under vision job `vprw1`, 21/21 checks [MEASURED 2026-11-14, `evidence/live_s1.json`]:

| Spike S1 question | Result |
|---|---|
| `save_as_mainfile(copy=True)` from the bridge timer job | works with a plain call (`via=direct`); 917,824 bytes, job 69 ms [MEASURED] |
| `open_mainfile` from the timer job returns, answer authenticated | yes, plain call; job 11 ms [MEASURED] |
| timer alive after the load | yes (next digest job answered, digest equal) |
| `load_ui=True` restores the view | yes: after `KP_1` the delta is `["ui:view3d"]`; `load_ui=False` leaves that delta, `load_ui=True` clears it |
| frame after restore equals base frame in `FOOTPRINT` | yes (`diff_regions` = `[]`); the edit frame differs |
| F4 | a material colour + a new modifier: F1 `changed=[]`, v2 delta `col:Collection, mat:Material`; rolled back |
| tamper | one flipped byte: `store.verify` → `CKPT_CORRUPT`; Blender-side sha check → `JOB_ERROR`, scene unchanged |

Rung A′ was not needed.

## Not verified / open issues

- After a restore `bpy.data.filepath` is the checkpoint file (title bar shows `p0.blend`). A later `save_mainfile` by an agent job would overwrite the point; the sha check then refuses it (`CKPT_CORRUPT`), it is not prevented.
- Without `XDG_CONFIG_HOME` under RT, `open_mainfile` writes Blender's recent-files list in the user config directory.
- Digest v2 is still a subset: node-group internals, colour ramps/curves, non-mesh object data (lights, cameras, curves), images, world nodes, animation, undo stack, add-on prefs and modal state are outside it. Edit-mode mesh data is read as last flushed.
- `ui:view3d` hashes the view sources (location, rotation, distance, perspective, camera zoom/offset, lens) of the first 3D region, not the raw `view_matrix` (a draw-time cache).
- The restore deadline (`RESTORE_CAP_S`) and the relaunch path A′ are the caller's (R2/R4); a job that holds Blender's main thread blocks the restore job too.
- Live run used one small scene; checkpoint size/latency on large scenes and the hung-main-thread case were not measured. The non-persistent-bridge control ran against the fake bpy only.
- The store has no cross-process lock: one broker per RT (the F1 `broker.lock`) is assumed.

## Digest coverage and stability fix (lane sw-agents-rewind, wave w186, claude-fable-5-2, Track P)

`test_digest_live.py` runs the digest in real `blender -b` 4.0.2 (no fake bpy): 8 tests, two
Blender processes. On the digest of commit 8622f9b, 6 of the 8 are red [MEASURED]; now 8 pass.

What was wrong and is fixed in `digest.py`:
- Blind state: light energy / color / size, camera lens / type, curve data, a deleted or
  flipped face with the vertices kept, shade smooth, custom properties, the viewport eye
  (`hide_get`), `display_type`. The judge rejects an empty delta (`NO_EFFECT`) and the store
  dedups on the digest, so a light-only or camera-only turn could not be committed, and a
  blind change committed next to a seen one could not be told from a failed restore. Now:
  non-mesh object data is hashed through its RNA properties (ID bookkeeping skipped), meshes
  through loop vertex indices, loop totals, smooth flags, material indices and edges.
- False mismatch on a correct restore: materials and collections that nothing uses were in
  the digest, but a `.blend` does not keep them (factory `Material` after the default cube is
  removed; `bpy.data.materials.new()` never assigned; a collection linked nowhere). Digest at
  mark time had the key, digest after restore did not. Now only collections reachable from a
  scene and materials used by an object of those collections (or with a fake user) are hashed.
- Two scenes: every master collection is named `Scene Collection`, the last one overwrote
  the first under one key. Now all are hashed under that key (one scene: same shape as before).
- Group-valued custom properties are skipped on purpose: Blender adds a `cycles` group to a
  camera on the first save -> open, which would break restore equality.

Still outside the digest: unused (orphan) datablocks, UV / colour attributes / vertex groups,
shape keys, animation data, node groups referenced only from modifiers, images, the inner
fields of pointer structs (camera `dof`), particle and physics settings. Python interpreter
state (module attributes rebound by a job) is not in a `.blend` at all and survives
`open_mainfile` [MEASURED]; `product/ref_agents/contract.py` (branch p-fable-f4) refuses such
scripts statically. `ckpt_live.py` (GUI, vision job) was NOT re-run after this change; the
38 tests of `test_ckpt.py` (fake bpy) pass unchanged.
