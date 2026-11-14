# ref_capsule/patches — OpenSuperficies Docker capsule audit (lane ct-audit)

Author: Claude Fable 5.2 (`claude-fable-5-2`), **Track P, declared non-Gemini**. Nothing here was
written by a Gemini model. These are **proposals**: `.patch` files that were never applied to
`$VP_OPS_ROOT/OpenSuperficies` (read-only for this lane, `git status` there is unchanged).

Audited tree: OpenSuperficies `0052e34` (`Merge branch 'mv/net-modes-advertise'`).
Papers: PapersHub router, corpora `ct` (PapersContainers) and `ku` (PapersKubernetes); the C1 decision
section of every cited paper was read.

## How the patches were produced and checked

1. `git archive 0052e34` into a scratch copy (no write to the audited repo).
2. For each patch: new test written first and run **red**, then the change, then **green**.
3. `cat 000*.patch | git -C OpenSuperficies apply --check -` passes on `0052e34` [MEASURED].
   0001-0004 stack (same files); 0005 and 0006 are independent.
4. Regression `go test ./src/... ./cmd/...` (offline, `GOPROXY=off`, in a
   `systemd-run --user --scope -p MemoryMax=8G` scope): only `src/surfacebook/pbx` and `src/w8muse`
   fail, and both fail identically on the untouched base (operator token / socket path length of
   the scratch directory), so they are not caused by the series [MEASURED].

No container was started (cell rule: no real containers). Everything about runtime behaviour of the
new flags is therefore argv-level evidence only.

## Findings, by priority

| # | Pri | Finding | Where | Papers | Patch |
|---|-----|---------|-------|--------|-------|
| F1 | P0 | **Capsules escape the caller's systemd scope.** A running capsule sits in `/system.slice/docker-<id>.scope` while the caller is in `user.slice/.../newton-ia.slice/run-*.scope` [MEASURED, `/proc/<pid>/cgroup` of `srf-egress-proxy`; cgroup driver `systemd`, v2]. `systemd-run -p MemoryMax=8G` around `srf`/`docker run` limits the CLI only. The per-container `--memory 4g` is the sole ceiling and there is no aggregate: N capsules = N x 4 GiB. | `src/programsurface/launch.go:47-48`, `src/browserid/surface/surface.go:489-491`, `src/cubook/capture/runargs.go:139`, `src/holdingscap/holdingscap.go:143-144` | KU-0007, CT-0023 | 0004 |
| F2 | P0 | **Images are named by mutable tag, never by content.** The catalog admits `name:semver` by regex; the browser and Holdings images are tag constants; the recipe records `base_digest: null`; the base Dockerfile starts `FROM debian:12-slim` with unpinned `apt-get install`. Nothing compares the image id before `docker run`, so a re-tag changes what a certified capsule executes. | `src/programsurface/catalog.go:17,25-28`, `src/browserid/surface/surface.go:29,495`, `docker/program/recipe.json:5-7`, `docker/program/Dockerfile:1`, `docker/Dockerfile.browser-arm64:2,10` | KU-0006, CT-0026, KU-0069 | 0003 (program lane only) |
| F3 | P0 | **Rootful daemon, no user-namespace remap.** `docker info` shows no `rootless`/`userns` security option and `HostConfig.UsernsMode` is empty [MEASURED]: container uid 10001 is host uid 10001 and membership of the `docker` group is root-equivalent. The boundary is caps + seccomp + read-only root only. | daemon configuration (not in repo) | KU-0046, KU-0029 | none (needs root) |
| F4 | P1 | **`no-new-privileges` missing on the container program vector** (the VM vector has it). The browser vector also lacks it, deliberately: the image asserts a setuid `chrome-sandbox`. | `src/programsurface/launch.go:47` vs `:56`; `src/browserid/surface/surface.go:489`; `docker/identity/Dockerfile:6` | KU-0046, CT-0009 | 0001 |
| F5 | P1 | **Holdings capsule keeps the default capability set**: no `--cap-drop`, no `no-new-privileges`, no `--pids-limit`, no `--read-only`, while the app runs `--no-sandbox`. | `src/holdingscap/holdingscap.go:143-148,159` | KU-0046, CT-0009 | 0006 |
| F6 | P1 | **CU vector under-bounds memory.** `--memory 4g` without `--memory-swap` (swap allowed up to the same amount again) and an unsized `/tmp` tmpfs (kernel default: half of host RAM, charged to the memcg). | `src/cubook/capture/runargs.go:139-140` | KU-0007, CT-0023 | 0005 |
| F7 | P1 | **X authority is all-or-nothing inside a capsule.** CU Xvfb runs with `-ac` (access control off); it is safe from outside (`-nolisten tcp -nolisten unix`, `--network none`, and `Validate` refuses `/tmp/.X11-unix`, `docker.sock`, `/dev/dri` mounts), but every process that can reach the display can inject input and read every window. For the multi-agent capsule this means agents must never be X clients: only the broker is. Sidecars sharing the X socket would share full authority. | `docker/cu/entrypoint-cu.sh:59-60`, `docker/program/entrypoint.sh:268-269`, `src/cubook/capture/runargs.go:124` | KU-0029, CT-0009, KU-0027 | none (design rule) |
| F8 | P2 | **View socket is world-writable by construction**: host dir `chmod 0777`, Xpra socket `--socket-permissions=666`. Only the two `0700` parent directories protect it. A parent-permission regression exposes an input-capable Xpra endpoint to every host uid. | `src/browserid/surface/view.go:36-47`, `docker/identity/xpra-layer.sh:33-34` | KU-0027, KU-0077 | none (add a gate that stats the parents) |
| F9 | P2 | **No liveness after start.** No `HEALTHCHECK` in any Dockerfile [MEASURED, 0 hits] and no `--health-cmd`; readiness is a one-shot scan of `docker logs` for a token. A dead Xvfb is discovered only when a frame read fails. Restart policy is the implicit default `no` (correct, but undeclared); no `--init`. | `src/browserid/surface/surface.go:782-848`, `src/programsurface/launch.go:47-54` | KU-0026 | 0002 |
| F10 | P2 | **Unbounded container logs.** Driver `json-file` with empty options [MEASURED]: the log grows on the host disk outside every cgroup limit, and it is the readiness channel. | same vectors as F9 | KU-0007, CT-0025 | 0002 |
| F11 | P2 | **Cleanup is fail-closed but orphans are found by name only.** `Stop` removes, verifies with `docker ps -a`, and retains the lease on any doubt (conforms). Program and browser containers carry no label, unlike the CU and Holdings vectors, so a sweep after a crashed `srf` depends on the name regex. No `--rm` is right: evidence is harvested before removal. | `src/browserid/surface/surface.go:887-927`, `src/programsurface/launch.go:47`, `src/cubook/capture/runargs.go:137`, `docker/cu/diag/harvest.sh:2` | KU-0013, CT-0004 | 0003 adds `srf.image`; a `srf.lease` label is recommended |
| F12 | P2 | **`docker commit`/`checkpoint` cannot carry rewind.** The root filesystem is `--read-only` and all mutable state lives in tmpfs and bind mounts, which `commit` does not capture; `docker checkpoint` is unavailable (`ExperimentalBuild=false`, no `criu` on PATH) [MEASURED]. | all vectors | CT-0024, KU-0028, CT-0014 | none; see "Rewind" |

Conforming and worth keeping: `--network none` by default, `--cap-drop ALL`, read-only root, non-root
uid, `--pids-limit`, sized `nosuid,nodev` tmpfs, lexical mount-path validation
(`launch.go:88-98`), closed `ExtraArgs` allow-list (`runargs.go:53-72`), seccomp profile pinned by
content hash for worker seats (`src/w8seat/launch.go:32-38`), loopback-only published ports
(`scripts/docker-xpra-surface.sh:80-81`).

## Patches

| File | Change | New test (red before, green after) | Existing tests touched |
|------|--------|------------------------------------|------------------------|
| 0001 | `--security-opt no-new-privileges` on the container program vector unless `SeccompIdentity` | `TestContainerVectorNoNewPrivileges` (+ negative control for the Electron class) | golden in `launch_test.go`; `surface_test.go:720` now asserts "no `seccomp=`" instead of "no `--security-opt`" |
| 0002 | `--restart no`, `--log-opt max-size=8m/max-file=2`, `--health-cmd "xdpyinfo -display :100"` (10s/3s/3), container runtime only | `TestContainerVectorHealthAndLogBounds` | two goldens |
| 0003 | `Program.ImageID` (validated `sha256:<64 hex>`); when set the vector ends with the id and labels the tag; ids of the two built images pinned from `docker image inspect` | `TestContainerVectorRunsPinnedImageID` (4 malformed pins refused; unpinned keeps the tag) | catalog and Metaviewer goldens |
| 0004 | `SRF_CGROUP_PARENT` (opt-in, `^[a-z0-9][a-z0-9-]{0,62}\.slice$`, `system.slice`/`user.slice` refused) rendered as `--cgroup-parent`; a refused value blocks the launch | `TestCgroupParentArgs` (10 refusals), `TestContainerVectorCgroupParent` | none |
| 0005 | CU vector: `--memory-swap 4g`, `/tmp` tmpfs `size=1g` | `TestRunArgsBoundsSwapAndTmp` | W2 and W3 `RunArgsSHA256` goldens re-derived |
| 0006 | Holdings: `--cap-drop ALL`, `no-new-privileges`, `--pids-limit 512` | `TestRunArgsDropsAuthority` | W-J1 golden |

### Before anyone applies them

- **0005 moves a frozen contract value** (`run_args_sha256` in `capsule.json`). It needs an operator
  ruling, and `size=1g` for `/tmp` is a guess: the real peak of the CU desktop was not measured.
- **0003 pins ids measured on this host today.** A rebuild changes the id, and the catalog must then
  change in the same commit. That coupling is the point, but it is a workflow change.
- **0004 does nothing until an operator creates and limits the slice** (root: for example
  `systemctl set-property srf-capsules.slice MemoryMax=24G TasksMax=2048`). How this daemon places a
  container under `--cgroup-parent` was not exercised: AUSENTE — no inventar.
- **0002**: `xdpyinfo` is in the base image (`x11-utils`, `docker/Dockerfile.browser-arm64:14`) and the
  entrypoint already calls it, but the probe was not run in a live capsule. Its claim is only "the
  X server of this incarnation answers"; it says nothing about the program (KU-0026).
- **0001**: whether the Electron image truly needs a setuid helper is AUSENTE; the patch leaves that
  class unchanged rather than guess.
- Not patched: the browser vector (F2, F4), F3, F7, F8.

## Rewind: what the container layer can and cannot give

A capsule checkpoint that respects CT-0024/KU-0028 has to name every component and give every
external dependency a disposition. With the vectors as built, the restorable identity is:

1. image **id** (0003) and `RunArgsSHA256` of the vector (`runargs.go:162`): the executable inputs;
2. a copy of the `/profile` bind mount taken at a turn boundary (the only durable capsule state);
3. the application checkpoint (`.blend` per committed turn), restored by relaunch.

tmpfs contents (`/tmp`, `/run/vqa/fb`, the X server state) are deliberately not restorable: a rewind
is a **new incarnation** started from 1+2+3, and every lease, frame reference and health observation
of the old incarnation is void (KU-0026, KU-0013). This matches rungs A2/A3 of
`waves/w185-rewind/reports/rw-state.md`. `docker commit` adds nothing to this and CRIU is not
available without root.
