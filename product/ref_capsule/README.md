# ref_capsule: multi-agent capsule layout (vp-capsule/v1)

Track P reference work, written by Claude (claude-fable-5-2). Not Gemini code. Nothing here
starts a container: it is a spec format, a dry-run validator, a plan renderer and an
observed-state checker, with tests. `patches/` and `gates/` in this folder belong to the
ct-audit and ct-gates lanes.

## Layout

```
                    capsule vpref-blender (sibling containers, no nested docker)
 +--------------------+   xsock volume    +--------------------+
 | program            |<----------------->| broker             |  the ONLY input holder
 | Blender 4.0.2 + X  |  /tmp/.X11-unix   | turn queue, xdotool|  single writer of events.jsonl
 | bridge (nonce)     |                   | frames, ckpt ring  |
 +---------+----------+                   +---------+----------+
   rt/inbox, rt/outbox (rw)                   rt/ (rw), ckpt (rw)
   ckpt (rw)                                        |
                                 rt/intents/<agent_id> (rw, own dir only)
                                 rt/frames, events.jsonl, cursor.jsonl, claims.json (ro)
        +---------------+---------------+---------------+---------------+
        | modeler-agent | props-agent   | look-agent    | camera-agent  |  sidecars: no X socket,
        +---------------+---------------+---------------+---------------+  no DISPLAY, no ckpt
```

Sibling sidecars answer the "Docker inside Docker" question of `03_FACTS.md`: nesting needs
`--privileged` or a special runtime; rule H-01 refuses `privileged`, so the nested form cannot
pass. An agent has a cursor only as a record the broker draws; its only way to act is to drop
an intent file in its own directory (shared contract, `fable_units/COMMON.md`).

## Files

| File | What it does |
|---|---|
| `validate.py` | 13 rules over a spec; exit 0 green, 1 red, 2 unreadable; `--json`, `--skip RULE` |
| `render.py` | prints the launch plan (volume, start, stop, inspect argv) for a GREEN spec; refuses a RED one; cannot execute |
| `observe.py` | compares the spec with a saved `docker inspect` JSON (desired versus observed) |
| `specs/blender_4agents.json` | reference capsule: program + broker + 4 agent sidecars |
| `tests/test_capsule.py` | 16 tests, 60 spec mutations, 15 drift mutations |
| `REWIND_MAPPING.md` | how rewind checkpoints map to volumes and container operations |

```
cd product/ref_capsule
python3 -I validate.py specs/blender_4agents.json
python3 -I render.py   specs/blender_4agents.json > plan.json     # prints, never runs
python3 -I observe.py  specs/blender_4agents.json inspect.json    # after an operator launch
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
  python3 -I -m unittest discover -s tests -v
```

## Rules

| Id | Rule | Why |
|---|---|---|
| S-01 | names match `vpref-...`, unique | cleanup and ownership by prefix |
| T-01 | one program, one broker, at least one agent, unique `agent_id` | one input holder |
| T-02 | start order program, broker, agents; nothing depends on an agent | an agent crash must not block the capsule |
| I-01 | images pinned `@sha256:` | the image digest is part of the capsule state identity |
| H-01 | non-root numeric user, read-only rootfs, cap-drop ALL, no cap-add, no privileged, no devices, no credential-looking env keys | ct:CT-0009 C1: every shared object explicitly permitted |
| N-01 | program and broker on network `none`; agents on `none` or a declared egress network; never `host` or `container:<x>`; pid/ipc/userns private; no `volumes_from` | an X server also listens on an abstract socket that belongs to the network namespace, so hiding the socket file is not enough if a sidecar shares that namespace |
| V-01 | volumes declared; bind sources only under `/dev/shm/vqa-cu/ref*/`; the X socket directory is never a host bind | keeps display :1, `hybrid/`, `swarm/`, the docker socket and home out |
| X-01 | x11 volume only in program and broker; agents have no mount or tmpfs at `/tmp/.X11-unix`, no `DISPLAY`/`XAUTHORITY`/`WAYLAND_DISPLAY`/`XDG_RUNTIME_DIR`/`DOCKER_HOST`; `input` true only for the broker | only the broker holds input |
| A-01 | broker mounts all of RT rw; program only `inbox` and `outbox`; an agent only its own `intents/<agent_id>` rw plus four read-only views | `events.jsonl` has one writer; an agent cannot forge another agent's intent or reach the bridge inbox |
| L-01 | `memory_mib`, `memory_swap_mib == memory_mib`, `pids_limit`, `shm_mib`, sized tmpfs; tmpfs+shm fit inside memory; agent ceilings; containers + volumes fit `budget.memory_mib` (max 8192 MiB [TARGET]) | see "Limits that really apply" |
| HC-01 | health check per container with a role-specific kind; exec array of safe tokens; timeout below interval | ku:KU-0026 C1: a probe proves only the predicate it tests. A process-exists probe is refused for the program; it must be a nonce job round trip through the bridge |
| R-01 | stop with SIGTERM or SIGINT and a bounded grace; restart `no` or `on-failure:N` (N at most 5 [TARGET]); the program is never restarted by the engine; program grace covers the checkpoint save budget | a restarted program is a new scene incarnation that only the broker may admit |
| RW-01 | rewind rung A2 or A3; P, C1, C2, V, A1 refused with the reason; ckpt volume mounted rw in the program, present in the broker, absent from agents, not on a container tmpfs; `(ring+1) x max_blend_mib` fits the volume | `REWIND_MAPPING.md` |

## Limits that really apply

[MEASURED 2026-11-14, read-only] On this host docker is the system daemon (socket
`/var/run/docker.sock`, cgroup driver systemd, cgroup v2). The running container
`srf-egress-proxy` sits in `/system.slice/docker-<id>.scope` with `memory.max` 268435456 and
`pids.max` 256, equal to its `HostConfig.Memory` and `PidsLimit`. This session's shell sits in
`/user.slice/.../newton-ia.slice/run-*.scope`.

Consequences:

1. A `systemd-run --user --scope -p MemoryMax=8G` wrapped around `docker run` limits the docker
   client process, not the container. The wave rule keeps tests bounded; it does not bound a capsule.
2. The limits that apply are the per-container flags, so L-01 refuses any container without
   them, and refuses `memory_swap_mib` unset (the engine then allows swap up to twice the memory).
3. tmpfs and `/dev/shm` pages are charged to the container that fills them, so L-01 requires
   them to fit inside `memory_mib`. An unsized tmpfs is refused.
4. There is no aggregate cap for the capsule: that needs a slice with `MemoryMax` under
   `system.slice` and `--cgroup-parent`, which needs root. `budget.memory_mib` is therefore an
   arithmetic bound (sum of the parts), not a kernel bound.
5. A validated spec is a declaration. ku:KU-0007 C1 and ct:CT-0023 C2 both say a declaration is
   not evidence of enforcement; `observe.py` closes that gap from `docker inspect` (O-01 limits,
   O-02 hardening, O-03 mounts, O-04 X reachability, O-05 health, O-06 OOM and restarts,
   O-07 stray labelled containers). Reading `memory.max` and `pids.max` from the container's
   cgroup directory is the stronger witness and is left to the gate lane.

## Health checks

| Role | Kind | Predicate |
|---|---|---|
| program | `bridge_roundtrip` | a nonce job placed in `rt/inbox` is answered `OK` in `rt/outbox` within the deadline: the Blender main thread is alive and the bridge timer is registered |
| broker | `events_heartbeat` | the last record of `events.jsonl` is younger than the bound |
| agent | `heartbeat_file` | the agent loop touched its heartbeat file within the bound |

The helper programs named in the spec (`/opt/vp/health_*`) are not written yet.

## Graceful restart

- Stop order is the reverse of start: agents, broker, program (`render.py`, key `stop`).
- Agent sidecar: `on-failure:3`. It holds no state except its intent directory on the RT
  volume. A restart is invisible to the program; a queued intent whose `based_on_frame` is old
  is refused by the broker as `STALE_FRAME`.
- Broker: `on-failure:3`. `events.jsonl` and the ring manifest are on volumes, so a new broker
  resumes from the log. While it is down nobody holds input and agents can only queue intents.
- Program: restart policy `no`. The broker restarts it through rung A3 (see
  `REWIND_MAPPING.md`) and bumps `scene_epoch`. Its grace (30 s) is at least
  `rewind.ckpt_save_budget_s` (20 s [TARGET]), so SIGKILL does not land inside a final save.

## Not verified

- No container was started. Docker here is the rootful system daemon reached through the
  `docker` group, not rootless docker, so the lane rule forbids a live run.
- The images do not exist: the reference spec carries a placeholder digest of 64 zeros.
- Whether `volume-subpath` accepts a single file, whether an X client can connect through a
  read-only mount of the socket directory, and the uid options of a tmpfs-backed named volume
  are untested on this engine (29.6.2).
- Which cgroup is charged for pages of a tmpfs-backed named volume shared by two containers: AUSENTE.
- N-01 rests on general Linux and X behaviour (abstract unix sockets are scoped by network
  namespace; X servers listen on one unless told not to). It is not from the paper corpus and
  was not probed here.
- The limits in `POLICY` are targets, not measurements: an agent CLI may not fit in 512 MiB.

## Cross-review status (xr-capsule-auction, 2026-11-14)

Fixed, each with a regression test that fails at 8408bbf (`tests/test_xr_fixes.py`, `gates/tests/test_xr_gates.py`):

| Id | Fix |
|---|---|
| C1, C2, C11 | Mount `dst`, volume keys and tmpfs `dst` are allow-listed per field (no `,` `=` `:` quotes, whitespace); `render.py` refuses such a field on its own as well. |
| C3 | Networks are `none` or the single declared `vpref-*` egress network; `container:<x>`, `host`, `ns:<path>` are refused. |
| C4 | Bind sources must be pairwise disjoint (not equal, not nested). |
| C5 | Env keys: wider credential patterns; env values are scanned for token shapes; `DOCKER_HOST` is refused on every role. |
| C6 (schema part), C12 | Unknown spec, volume, container, mount and tmpfs keys are a shape error (S-00); non-string networks are S-00, not a crash. |
| C7 | A declared proof log that does not exist is `LOG_MISSING` (UNOBSERVED) in G1, G3 and G4, as it already was in G2. |
| C8 | G4 scans array-form commands, unwraps `bash -lc`/`sh -ec`, `sudo -u`, `timeout -s`, `eval`, `busybox`, shell heredocs; flags `xargs docker kill|stop|rm` and `systemctl stop|kill` of any non-`vpref-` unit. |
| C15 (waiver part) | A waiver `where` must be the whole place or a path suffix with a file name. |
| A3 | Each agent mounts `rt/bids/<own agent_id>` rw and nothing else under `bids`. The broker must create these directories before the agents start. |

Known limits, still open:

| Id | Effect |
|---|---|
| C6 (cgroup part) | No `--cgroup-parent`: containers run under docker's slice, not `newton-ia.slice`; there is no CPU cap. Per-container memory and pids caps are the only containment. |
| C9 | G2 misses several token families (`sk-svcacct-`, `glpat-`, `hf_`, `sk_live_`), credential reads through `<` redirects and some env-dump forms. |
| C10 | `cursor.jsonl` and `claims.json` are single-file binds: after a broker restart rewrites them through `os.replace`, running capsules keep the old inode until restarted. |
| C13 | Bind sources are checked by regex, not by realpath; a planted symlink under `/dev/shm/vqa-cu/ref*/` is not detected. |
| C14 | G5 has no replay/reuse probe. |
| C8 (rest) | G4 does not follow variables (`K=pkill; $K -f x`), `python -c` bodies or scripts fetched at run time. |
