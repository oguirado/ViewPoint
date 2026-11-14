# Rewind checkpoints and the container layer

Source: `ViewPoint/waves/w185-rewind/reports/` (rw-state ladder, rw-build R0 contract).
Short answer: a rewind point is a file on a volume, not a container snapshot.

## Why not a container snapshot

| Container mechanism | Verdict | Reason |
|---|---|---|
| `docker checkpoint` (CRIU) | refused, rule RW-01 rung C1/P | `criu` absent and the experimental daemon flag is off; both need root (rw-state section 3). ct:CT-0024 C3: runc reports `ErrNoCR` without support. C4: a resolved checkpoint does not establish that the process continues. A Blender dump would also need the X server in the same cut |
| `docker commit` | refused, rung C2 | the program rootfs is read-only and every writable path is tmpfs or a volume; there is no upper layer, so a commit captures nothing (rw-state rung C2) |
| named volume | used | holds the `.blend` ring; survives a program container restart |
| container `--tmpfs` | refused for checkpoints | dies with the container, which would remove the A3 fallback |

## Mapping

| Rewind object (rw-build R0) | Capsule object | Container operation |
|---|---|---|
| `rec:ckpt` `{point_id, tick, scene_epoch, blend_sha256, bytes}` | file `<point_id>.blend` on the `ckpt` volume, written by the program, hashed by the broker; ring manifest written by the broker | none |
| ring of N points | `rewind.ring` and `rewind.max_blend_mib`; RW-01 checks `(ring+1) x max_blend_mib <= size_mib` | eviction is a file delete by the broker |
| `control rewind` on rung A2 (reopen in process) | same program container, same X window; `scene_epoch` + 1 | none: the container is untouched |
| rung A3 (kill and relaunch on the last good point) | new program container from the same image digest and the same argv | `docker stop --time 30`, `docker rm`, `docker run` (plan argv), wait for `bridge_roundtrip` healthy, load the point, `scene_epoch` + 1 |
| `txn begin/commit/rollback` | records in `events.jsonl` on the `rt` volume | none |
| capsule state identity | `(image_digest, scene_epoch, ring_manifest_sha256)`, field `rewind.state_identity` | I-01 makes the image part checkable |
| capsule export [TARGET, not built] | archive of the `ckpt` volume + `events.jsonl` up to a committed tick + spec sha256 + image digests, taken by the broker with no open transaction | a copy out of the volumes |

Rules that follow from the mapping:

- The program's engine restart policy is `no` (R-01). An engine restart would produce a fresh
  Blender on an empty scene with the old `scene_epoch`, and agents would act on it as if it
  were the same incarnation. ku:KU-0026 C1 ties an authorization to the object incarnation.
- Agents never mount the `ckpt` volume (RW-01): a restore must be verified against bytes that
  only the program and the broker could have written.
- Sidecars survive A2 and A3 because they hold no X connection, only files. This is the main
  practical gain of keeping the X socket out of them.

## Declared exclusions

ct:CT-0024 C1 asks for explicit dispositions of external state. Outside the rewind domain:
undo history, interpreter state of the bridge, modal operator state, viewport state, agent
process memory, anything an agent sent over its egress network, and (with tmpfs-backed
volumes) everything after a host reboot or `docker volume rm`.
