# W8 Worker Bank v3: portable repository tasks in hardened RAM-only capsules (AMEND A3W8)

Status: amended design proposal and contract fixtures. No worker runtime, image build, orchestrator, firewall rule or VMM is implemented by this change. v3 supersedes v2 (`amend/W8_WORKER_BANK_v2.md`, sha256 `b0109f7d…`) and, through it, `docs/design/W8_WORKER_BANK.md` (merged at `60dc0b1`, PR #34). It folds the **OPERATOR RULING A4, the W8 security baseline** (`W8_AMENDMENTS.md` A4.1-A4.4, which supersedes A1 where they differ) and the judge fixes **JW8-01..05** (`review/judge/JUDGE_W8.md`). `AMEND3_W8.md` maps each item to its change. The contract is `os.cu.worker-task.v1.schema.json` plus `test_worker_task_schema.py` (stdlib, 78 tests). Both sit next to this file and drop into `contracts/worker/`.

Hypotheses declared:
- Repository citations use `60dc0b1` (the checkout); the prefix's `c0787db` is an ancestor without the W8 files, as AW8 and JW8 found.
- The guides are cited as **GC** and **GO**:
  - **GC** = `/home/user/Documentos/newton-shell/docs/plantillas/docker/GUIA_CONSTRUCCION_Y_REPRODUCCION_ENCLAVE_DOCKER.md` (329 lines).
  - **GO** = `/home/user/Documentos/newton-shell/docs/plantillas/docker/GUIA_OPERACION_DOCKER_PIPELINE_DESTILADO.md` (377 lines).
- Guide measurements describe `distil-box:1.1`; W8 numbers stay **targets** until a W8 card measures them.

## 0. What A4 changes, in one table

| Topic | v2 (A1) | v3 (A4 baseline) | Binding source |
|---|---|---|---|
| Images | One per-commit image `<repo>@<sha>` per bridge move; keep last N | **ONE hardened base image** `os-worker-base`. It is a rare build, gated by the operator approval file (JW8-02). No repository code goes into any image. Keep the current image plus 1 rollback. | A4.1; GC §2-§4 `GC:30-148` |
| Code in the capsule | RAM checkout over a per-commit image | **Per-task tmpfs clone** of the verified commit (`bridge/integration` after an operator merge). It is ingested as a `git bundle` over `docker exec` stdin and needs no network. | A4.1; GC §5 storage `GC:173-184`; GO §3 `GO:71-95` |
| Dependencies | Baked into the per-commit image | **Warm dependency cache volume** `os_worker_cache` at `/opt/cache`. It is **read-only to tasks** and written only by an operator-gated warm job. | A4.1; GC `GC:139`, `GC:181` |
| Workspace lifetime | `/dev/shm/os-worker/<run>` on the host | Capsule tmpfs `/workspace`, **purged at task end** (cleanup `remaining_workspace_bytes 0`). There is no host path and no bind. | A4.1; GO §9 `GO:200-221` (retention is overridden: purge on every task) |
| Results to the host | Receipt over the seat channel | **Tar-over-exec with assertions, text only**. Never `docker cp`. | A4.1; GO §5 `GO:126-149`; GO §14 `GO:332` |
| Inference | Go in-capsule agent; the seat on the host | **Never inside a capsule or VM.** Seats (Muse and others) run on the host in their own sandbox and drive the capsule through a **narrow exec allowlist**. | A4.2; GO §1 `GO:28-41`; GO §6 `GO:153-168`; GO §14 `GO:328` |
| Credentials and mounts | `no_host_mounts` with an approved RAM-root exception | **Zero** host bind mounts, no env-file, no docker.sock, no `~/.config` or `~/.ssh`, no key or token. | A4.2; GC §7 `GC:241-255`; GC §10 `GC:314` |
| Daemon and cgroup | Not specified | Rootless Docker or a dedicated daemon user; `read_only`, `cap_drop ALL`, `no-new-privileges`, memswap = mem, pids and nofile limits, cpuset off interactive cores, `oom_score_adj` ≥ 500 | A4.3; GC §5 `GC:156-171` |
| Devices | `/dev/kvm` on the microVM outer container | Same, and **only in the microVM phase**. Docker-phase grants and launch specs carry `devices: []`. | A4.3; A3.4 |
| Egress | Per-attempt UDS broker | **`none` by default.** A per-task allow-list exists only through **host rules on a dedicated bridge**, applied as a **separate operator action** and verified by probes. | A4.3; GC §6.2 `GC:206-237`; GO §12 `GO:282-302` |
| Proof | Phase-level acceptance | **Every card** carries the measured checks and the must-FAIL trio (§10). | A4.4 |

## 1. Decision and source boundary

The Worker Bank is a **Go control plane on the host**, and each task attempt gets one disposable capsule. The capsule is a hardened Docker container started from the single base image. Later, an existing VMM runs nested inside that outer container (A3). Go owns admission, scheduling, supervision, collection and reconciliation. Rust comes in only for a separately measured hot path.

**v3 removes every design in which an AI runs inside the capsule or the VM.** The v2 "Go in-capsule agent" is narrowed to a **non-inferential gate runner**: a static Go binary baked into the base image. It runs the gate argv, watches resources and writes structured progress to the tmpfs. It has no model client, no network and no credential. The host seat polls it through the `status` verb. The Python file is a dependency-free contract test, not the runtime.

The portable task is unchanged. Its nine shared fields are `task_id`, `repo`, `objective`, `sources`, `write_paths`, `gate`, `evidence`, `consumer` and `host_metric`. The bridge wire document stays `bridge-task-v1` / `bridge-result-v1`, embedded verbatim in `task.bridge` (v2 §4.1, corrected in §4.1 below by JW8-01).

### Bound repository evidence (at `60dc0b1`, unchanged from v2 unless marked)

| Repository observation | Design implication | Bound evidence |
|---|---|---|
| The entrypoint checks uid 10001, tenant ownership and framebuffer access | Keep the identity preflight. v3 makes `tini -s` PID 1 (GC `GC:104-112`) | `docker/cu/entrypoint-cu.sh:14-25` |
| With `XPRA_MODE` off, D-Bus, Xvfb, the WM and AT-SPI still start | A headless worker needs its own startup path | `docker/cu/entrypoint-cu.sh:10-11`, `:43-88`, `:90-97` |
| umask 027; the exec wrapper maps rc 1 to 5 only | Outer normalizer plus raw-status capture | `docker/cu/entrypoint-cu.sh:3`; `docker/cu/exec-umask.sh:2-11` |
| Container detection alone also accepts a host-network container | Verify the effective launch with `docker inspect` (§10 checks) | `docs/CU_KNOWN_LIMITATIONS.md:12` |
| Network-none, read-only root and uid 10001 form the capsule boundary | Policy stays outside task-controlled code | `docs/CU_KNOWN_LIMITATIONS.md:13`, `:27` |
| Kept-run TTL is enforced only on `--reap` | Supervisor timer plus an independent reconciler | `docs/CU_KNOWN_LIMITATIONS.md:21`, `:24` |
| Nonce fencing, bounded marker reads, harvest before removal | Bind receipts to attempts and harvest (extract) before purge | `docs/CU_W4.md:14-15`, `:29`; `docs/CU_KNOWN_LIMITATIONS.md:38-39` |
| Missing gitignored oracle shim, missing wheel path, image without numpy or the engine wheel | **v3:** satisfied by the base image toolchains plus the warm cache, **not** by per-commit images | `docs/CU_KNOWN_LIMITATIONS.md:34`, `:46`, `:52` |
| Bridge rule 5 lists **9** copied identifiers | Codec copies those 9 plus `role`. `surface` is never copied (JW8-01) | `.wavemind/bridge/PROTOCOL.md:23` |
| Existing Go lease and admission components | Reconcile before Phase 2 (JW8-03, §12.9) | `src/orchestratorbook/arrendador/arrendador.go:28-29`; `src/orchestratorbook/gobernador/gobernador.go:16`, `:24`; `cmd/srf-arriendo/main.go:1`; `cmd/srf-capsula-microvm/main.go:1` |
| The reference enclave exposes a `shell` verb | W8's allowlist **excludes** an interactive shell | `/home/user/Documentos/AaHerramientas/distil_box/distil-box.sh:233`, `:239-246` |

`AGENTS.md:14-16` still applies (frames never on disk; rc 0 and 2..7, never 1). `auto_ingest` stays AUSENTE in `bridge_core.py`. The guide's default retention (`GO:202`) is **not** inherited: A4.1 purges at task end.

## 2. Capsule profile and admission

### Default `worker` profile (A4 hardened)

| Property | Required behavior | Guide basis |
|---|---|---|
| Lifecycle | One attempt, one fresh capsule, one fresh tmpfs. Destroyed and purged after extract. | GO `GO:221` |
| Image | `runtime.base_image` = `{name: os-worker-base, digest, recipe_sha256, approval_sha256}`. The repository state is `runtime.commit` (40-hex). There is no per-commit tag. | A4.1 |
| PID 1 and user | `tini -s --` as PID 1 (`hardening.init: tini`) and user `10001:10001`. The gate runner is a child of tini. Every exec is re-parented under `tini -s` with `setsid --wait`, so status and timeout propagate. | GC `GC:92-112`; GO `GO:18` |
| Root filesystem | `read_only: true`. Writable paths are tmpfs only: `/workspace` (`rw,exec,nosuid,nodev,mode=0770,uid=10001,gid=10001`), `/tmp` (`noexec`), `/home/worker` and `/dev/shm` for UI frames. | GC `GC:171-184` |
| Privileges | `cap_drop ALL`, `no-new-privileges:true`, not privileged, an approved seccomp profile (`seccomp_profile_sha256`), private namespaces | GC `GC:169-170`, `GC:253` |
| Daemon | **Rootless Docker** (`daemon: rootless`), or a **dedicated daemon user** (`dedicated_user`). The operator's root daemon is refused. Measured by `docker info` security options. | A4.3 |
| Mounts | `bind_mounts: []` (const), `env_file: null` (const), `runtime_socket_mounted: false` (const). The only volume is `os_worker_cache:/opt/cache:ro`. | GC `GC:247-252`, `GC:314`; GO `GO:326-327` |
| Environment | Names come from the closed enum `hardening.env_names` (locale, tini, git-prompt, Go/Cargo/pip offline, `HOME`, `TMPDIR`). The seat fixes the values. Never `-e` pass-through. | GC `GC:248-249` |
| cgroup v2 | `memswap_bytes == memory_bytes` (swap 0), `pids_limit`, `ulimit nofile` (target 8192), `ulimit core=0`, `oom_score_adj` 500..1000 (target 800), a cpuset **disjoint from `interactive_cpus`**, `cpu.weight` reduced | GC `GC:156-171` |
| Devices | `[]` in the Docker phases. `["/dev/kvm"]` only on the microVM outer container (schema `seat_grant` rule plus `nesting.devices` const). | A4.3; A3.4 |
| Network | `none`. An allow-list is valid only with `hardening.egress = {mode: host_rules, bridge: br-osw…, rules_sha256, verify_sha256}` (§3). | GC `GC:188-237` |
| Lifetime | Absolute lease expiry plus a monotonic TTL from setup through extract and purge | v2 §2 |
| Output | Text only, over tar-exec (§3.4). No frame, video, binary or log-archive export path. | A4.1; GO `GO:126-149` |

The seat renders the closed **`launch_spec`** record (`os.cu.launch-spec/v1`) into a `docker run` argv **vector**. Task text never enters the argv. The record has no field that could express a bind, an env-file or a socket mount, so those three are refused by the schema before any argv exists (must-FAIL, §10). `launch_admission()` also refuses memswap ≠ mem, a cpuset on an interactive core, `/dev/kvm` outside the microVM phase and a workspace that is not tmpfs. Every refusal is rc 5 `containment`. A missing control is never compensated by adding privileges or a host path.

### Seat envelope and resource scheduling

The trusted seat on the host issues a grant (`os.cu.worker-seat/v1`) bound to the task digest, attempt, lease, nonce and expiry. `isolation.hardening` is the A4 block, and its constants are schema-enforced. `validate_bundle` also binds `runtime.commit` to the task commit, the tmpfs size to `limits.workspace_bytes`, cpuset ∩ `interactive_cpus` = ∅, egress `none` ⇔ network `none`, and, on success, the observed probes (`devices`, `nofile`, `oom_score_adj`, `daemon`) to the grant. Admission reserves memory including the tmpfs: a tmpfs is RAM, and its size is a ceiling, not free memory (GC `GC:184`). The `interactive_cpus` set is **discovered on the host**, never copied. The guide's `0-11` interactive / `12-19` enclave split (`GC:158`) is an example, not a grant. Planning targets: TTL 15 min, 2 CPUs, 4 GiB, a 2 GiB tmpfs, 256 PIDs, nofile 8192, `oom_score_adj` 800.

## 3. Base image, tmpfs clone, warm cache, egress and extract

### 3.1 One hardened base image (replaces v2 §3 and §A1)

- **Recipe.** `os-worker-base` is built from one recipe directory: a multi-stage Dockerfile on the GC pattern (`GC:30-38`).
  - Toolchains are pinned by version **and** sha256. `latest` is never used (`GC:62-73`, `GC:309`).
  - A slim runtime stage carries `tini`, `procps`, `git`, `jq` and the Go gate runner (`GC:83-90`).
  - User 10001 (`GC:95-100`).
  - Behavioural build assertions (`GC:116-128`, `GC:311`).
  - `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=/bin/true` (`GC:249`).
  - The recipe holds **no repository code**: `repo_code_in_image: 0`.
- **Build record.** It is `os.cu.base-image/v1`, with these fields fixed by the schema:
  - `arch: aarch64` and `build_context: recipe-dir`
  - `fetch_pins_verified: true` and `user_uid: 10001`
  - entrypoint `["/usr/bin/tini","-s","--"]`
  - `secret_paths_in_context: 0`, `frame_bytes_in_image: 0` and `repo_code_in_image: 0`
  - `approval.mode: per_build`
  - `retained_rank` 1..2, meaning the current image plus one rollback
- **Build egress.** Pinned fetches need egress (`GC:277`), so the build runs on the operator plane, outside `br-osw`.
- **Operator gate (JW8-02).** Each build is a **rare, operator-visible act**. The approval point is `/etc/opensuperficies/builder_approval.json`, which is AUSENTE on the host (re-measured: `ls: no se puede acceder`). The file is operator-owned, root-written and read-only to the builder. It pins the approved `recipe_sha256`.
  - **v3 change:** the mode is **`per_build` only**. There is no standing per-commit rebuild, because commits no longer produce images.
  - A recipe outside the file gives rc 7 `operator_recipe`.
  - A recipe that leaks `~/.config`, `~/.ssh`, `$HOME`, `operator.token`, `launch.token`, `*.key`, `auth.json`, `.beckett/token`, or copies from an absolute or `..` source, gives rc 5 (model `recipe_admission`).
- **Reproducibility.** Two `--no-cache` builds of the same recipe must give the same digest. Otherwise the result is rc 5 and nothing is published (model `image_digest_model(recipe, context)`; GC `GC:268`).
- **No agent path.** No agent lease, CDP or API path can edit the approval file, trigger a build or reach the OPERATOR-ONLY frame disk save (U-DEC-0040 unchanged).

### 3.2 Per-task tmpfs clone of the verified commit

**Eligibility.** Only a commit reachable from `bridge/integration` **after an operator merge** is eligible. The seat checks that `runtime.commit` is an ancestor-or-equal of the `bridge/integration` tip it observed. Workers never re-resolve branches.

**Ingest without task network.**
1. The host seat streams `git bundle create - <commit>` from its read-only mirror into `docker exec -i -u 10001:10001 <capsule> tini -s -- setsid --wait /opt/w8/ingest`, verb `ingest`.
2. Inside the capsule, `git clone --no-hardlinks --no-tags` runs from stdin into `/workspace/repo`. Hooks, credential helpers, filters, alternates, submodules and LFS are disabled, and the checkout is detached.
3. `git rev-parse HEAD` must equal `runtime.commit`, otherwise rc 3 `setup`.

There is no bind and no host file. The bundle never lands on host disk because it is piped. Clone guards follow GO `GO:82-91`, adapted: the capsule takes no URL, so the scheme and injection guards become "stdin bundle only".

### 3.3 Warm dependency cache volume

`os_worker_cache` is a named volume at `/opt/cache` (GC `GC:139`, `GC:181`). In v3 it is **mounted read-only in task capsules** (`workspace.cache.read_only: true`). Build caches (`GOCACHE`, pip, cargo target) are redirected into the tmpfs, so a task can never poison the cache that the next task reads.

The cache is filled only by a **warm job**: a separate operator-approved capsule from the same base image with a write mount and an egress allow-list (§3.5). It records a `manifest_sha256`, which is bound in every runtime record. Toolchains with offline modes (`GOFLAGS=-mod=mod` with `GOMODCACHE` read-only, `PIP_NO_INDEX`, `CARGO_NET_OFFLINE`) let tasks run with network `none`. A cache miss gives rc 4 `unavailable`, never an egress fallback.

### 3.4 Extract: tar-over-exec, text only (A4.1)

Results leave only through the guide's extract path (GO `GO:126-149`): `docker exec … tar -c` of `/workspace/out/` is piped into a host `tar -x --no-same-owner --no-same-permissions`, using a staging directory on the destination filesystem and an atomic `mv`. `docker cp` is never used (GO `GO:332`). The assertions are enforced in code (model `extract_admission`; schema `extract_record`):

- An empty artifact is refused (GO `GO:139`, rc 30 there; rc 5 `verification` in W8).
- Flat names only, with suffixes in `.md .json .txt .patch .diff`.
- No NUL byte, and the payload must be UTF-8. Frames and binaries are refused.
- Mode is forced to 0644, and the owner equals the invoking host user.
- `staging_residue 0` on both the success and the failure path (GO `GO:141`).

The receipt's `extract` is required (non-null) for docker/microvm receipts with rc 0, and it is `null` on the web route. A code delta travels as a `.patch` text file.

### 3.5 Egress: none by default, host rules as a separate operator action

The compose and launch configuration cannot restrict egress (GC `GC:17`, `GC:204`; GO `GO:22`). The default is `--network none`.

**Allow-list tasks.** A task whose policy has an allow-list, or the warm job, runs on the dedicated bridge `br-osw`. Host chains `OSW-EGRESS` (in `DOCKER-USER`) and `OSW-HOSTGUARD` (`INPUT -i br-osw … DROP`) follow GC §6.2 (`GC:206-237`). Rules are per-task `RETURN` lines for the exact `host:port` resolved by the operator.

**Operator action.** The rules are applied **by the operator** with a root-level, reversible script (`GC:237`; GO `GO:293-300`). The resulting `rules_sha256` and the probe report `verify_sha256` are entered in the grant. A grant whose `network.mode` is `allowlist` without the `host_rules` record, or `none` with one, is refused (`validate_bundle`).

**Probes.** The probe set always contains one **off-list target, expected BLOCKED**, plus the host gateway SSH probe, expected BLOCKED (GO `GO:286-291`). Until the probes pass, the task stays queued. Repository text and model output never widen a grant.

### 3.6 Storage law

Clone, `.git`, caches, tmp, the stdout/stderr rings and patch staging live in capsule tmpfs, as do UI frames under `/dev/shm` inside the capsule. With swap at 0 (memswap = mem, GC `GC:161`), untrusted code is never paged to NVMe. Frames never touch disk. Evidence is capped at 102400 bytes per attempt, receipt first. The base image carries no secrets, no frames and no repository code (schema constants).

## 4. One portable task contract

`os.cu.worker-task.v1.schema.json` (JSON Schema 2020-12 subset, every object closed). v3 `$defs` changes:

| `$defs` entry | v3 change | Cause |
|---|---|---|
| `image_tag`, `env_image` | **Removed.** | A4.1 supersedes A1 |
| `runtime` (docker, microvm) | Drops `image_sha256`, `image_tag` and `env_recipe_sha256`. Requires `base_image`, `commit`, `workspace` (`workspace_spec`), `extract: tar-exec` (const) and `inference: host` (const). | A4.1, A4.2 |
| `base_image` | `{name: os-worker-base, digest, recipe_sha256, approval_sha256}` | A4.1, JW8-02 |
| `workspace_spec` | Constants `kind: tmpfs`, `root: /workspace`, the options string, `purge: task_end` and `ingest: git-bundle-over-exec`; `size_bytes`; `cache {volume os_worker_cache, target /opt/cache, read_only true, manifest_sha256}` | A4.1 |
| `tmpfs_path` (new), `isolation.workspace_root` | The workspace root must be `/workspace/<…>`. A `/dev/shm` host root or a home path is refused. | A4.1, A4.2 |
| `hardening` (new, in `isolation`) | Values: `daemon` rootless\|dedicated_user, `init` tini, `read_only`, `memswap_equals_memory`, `nofile`, `oom_score_adj` 500..1000, `interactive_cpus`, `devices` `[]`\|`["/dev/kvm"]`, `bind_mounts []`, `env_file null`, `runtime_socket_mounted false`, `env_names` (closed enum), `egress` (none\|host_rules) and `exec_allowlist_sha256`. A `seat_grant` rule ties `devices` to the backend. | A4.2, A4.3 |
| `probes_observed` (new, in `observations`) | The guide's measured checks (§10): `inspect_sha256`, `etc_write`, `home_admin`, `egress_probe`, `orphans_after_task`, `memswap_equals_memory`, `oom_score_adj`, `nofile`, `devices`, `bind_mounts`, `env_file_used`, `env_secret_names`, `runtime_socket_visible`, `daemon` and `inference_processes`. | A4.4 |
| `safe_observations`, the last `receipt.allOf` rule | rc 0 requires safe probes and `orphans_after_task 0`. Any observed failure must report safe probes unless the reason is `containment` or `indeterminate`, and must report 0 orphans unless rc is 6. | A4.4 (extends ADV-W8-02) |
| `extract_record` (new), `receipt.extract` | `tar-exec`, `text_only`, `0644`, `owner_asserted`, `staging_residue 0` and a digest. Required on local rc 0; `null` on web. | A4.1 |
| `launch_spec` (new) | The closed `docker run` record. There is no field for a bind, env-file, socket mount, `privileged` (must be false) or host network. | A4.2-A4.4 |
| `capsule_exec` (new) | The exec allowlist: verbs `ingest`, `gate`, `status`, `extract` and `purge`; user `10001:10001` and tini. No shell and no inference verb. | A4.2 |
| `base_image_record` (new) | `os.cu.base-image/v1` (§3.1) | A4.1, JW8-02 |
| `nesting` | Unchanged. `outer_image_sha256` must equal `base_image.digest` (`validate_bundle`). | A3, A4.1 |

All v2 rules stay in force: the nested `.git` refusal, failure-observation hiding, verified cleanup unless rc 6, the rc 7 split, the bridge mirrors, `scheduler_lease` and `speech_act`. Canonical digest rules are unchanged.

### 4.1 Bridge codec (Phase 1; reference model in the test file), corrected by JW8-01

The task-side projection table is unchanged from v2 §4.1: ids, repo and commit, sources, write paths, the single-line gate, evidence kinds, `consumer` and the verbatim `task.bridge`.

**Receipt to `bridge-result-v1`.** The codec copies **the 9 PROTOCOL identifiers** (`task_id`, `thread`, `agent`, `worker_id`, `attempt_id`, `lease_id`, `key`, `base_seq`, `done_signal`; `.wavemind/bridge/PROTOCOL.md:23`) **plus `role`**. **`surface` is never copied.**
- `encode_bridge_result(…, live=True)` encodes only when `task.bridge.surface == "os-capsule"`, and it sets that value from a codec constant.
- **Any other surface is refused with rc 4 `unavailable`** (must-FAIL test `test_jw801_live_encode_of_non_os_surface_is_refused_rc4`).
- With `live=False`, the codec builds a **parity-only** envelope for comparison with `bridge_core.check`. The outbox writer refuses it, so it is never written to `outbox/`.
- The canon enum has no `os-capsule` value today (the mirror test asserts its absence), so no live OS result can be produced until the canon adds it (§12.2).

The rest of the mapping is unchanged from v2. Its artifacts are exactly the text files that crossed the tar-exec extract.

### Receipt and return transport

The host harness builds the receipt from its grant, `docker inspect` and probes; capsule bytes are untrusted and enter only through extract. Success needs every gate to succeed, an output matching the manifest, verified cleanup **including the tmpfs purge** (`remaining_workspace_bytes 0`), safe probes and a valid `extract` record. Web receipts carry `observations: null`, `extract: null` and `cleanup: not_applicable`.

## 5. Harness: one task into one capsule (host-driven)

The Go seat on the host owns admission, deadlines, extract and purge. The backend interface (`prepare/start/inspect/stop/collect`) is implemented by Docker and by the nested microVM. The v1 ten-step sequence (`docs/design/W8_WORKER_BANK.md:141-152` at `60dc0b1`) holds with these v3 steps:

1. **Preflight.**
   - The base image digest is in the approval file and the image's build record is green.
   - The daemon is rootless or dedicated.
   - `interactive_cpus` is discovered on the host.
   - The cache manifest is bound.
   - If the network is not `none`, the operator host-rule record has been verified.
2. **Launch.** Render the `launch_spec` into an argv vector and start the capsule (`sleep infinity` under tini).
3. **Measured checks** (§10, rows C1-C5), before any task code exists in the capsule.
4. **Ingest** the bundle (verb `ingest`) and verify HEAD.
5. **Gate** (verb `gate`). The gate runner runs `task.gate.argv` with a monotonic timeout. Raw status, signals and rc 124 are kept.
6. **Status** polling (verb `status`): structured progress from the tmpfs.
7. **Extract** (verb `extract`, tar-over-exec, §3.4).
8. **Purge** (verb `purge`, then `docker rm -f`). Re-measure 0 orphans, 0 RAM roots and 0 listeners, then write the receipt.

**Exec allowlist.** The seat may run only these five verbs, each as `docker exec -u 10001:10001 <capsule> tini -s -- setsid --wait /opt/w8/<verb>` with argv vectors and the env allowlist (GC `GC:248`; GO `GO:18`). Interactive `shell` (`distil-box.sh:233`), `cp`, model clients and arbitrary argv are refused, and `exec_allowlist_sha256` pins the set.

**Inference.** It runs only on the host. Seats (Muse, Fable, others) each run in their own host sandbox. They read extracted text and issue verbs, and they never place a key, token or session in the capsule (GO `GO:155`). The probe `inference_processes` must be 0.

### Lifecycle and recovery

States are unchanged (the `scheduler_lease.state` enum). A janitor reconciles leases, containers, tmpfs and host-rule chains; uncertain state gives rc 6 and quarantine; extract always precedes purge.

## 6. Exit codes and evidence semantics

| rc | W8 meaning |
|---:|---|
| 0 | Gates passed, extract valid, purge and cleanup verified, probes safe |
| 2 | Invalid input, schema, pin or command shape, including an unmappable bridge task |
| 3 | Known execution or setup failure (the clone's HEAD differs from the pin) |
| 4 | Prerequisite unavailable: base image absent or unapproved, cache miss, `/dev/kvm` missing, **live encode for a non-`os-capsule` surface** (JW8-01), or allow-list host rules not applied |
| 5 | Gate, timeout, OOM, verification (empty or binary extract), storage or **containment**. Containment covers a refused launch spec (bind, env-file, socket, swap, interactive core, device outside the phase) and an unsafe probe. Also a leaking or non-reproducible base recipe. |
| 6 | Indeterminate. Quarantine. |
| 7 | `operator_recipe` (base recipe not in the approval file) or `operator_capability` |

Normalization is unchanged from v2 §6.

## 7. Optional `worker_ui` profile

Unchanged from v2 §7 under A4: frames live in the capsule `/dev/shm` tmpfs, xpra is reachable only over a per-attempt UDS through an allowlisted exec relay (no TCP listener, no host socket bind), and any model watching the UI runs on the host.

## 8. MicroVM phases: Docker outside, existing VMM inside (A3 under A4)

1. **Outer container.** It is the same `os-worker-base` lineage (`nesting.outer_image_sha256 == base_image.digest`) under the same `launch_spec`. **`/dev/kvm` is the only device, and only in this phase:** the schema rule makes `devices` `["/dev/kvm"]` for microvm and `[]` for docker, and `launch_admission` refuses `/dev/kvm` on a docker launch. On this host the device is `crw-rw----+ root kvm`, so the outer container gets the `kvm` group and nothing else.
2. **Rootless consideration.** KVM under rootless Docker needs the daemon user in `kvm`, and cgroup v2 delegation for `cpuset`, `memory` and `pids` (systemd `Delegate=`). This is **measured, not assumed**. If delegation is missing, a dedicated daemon user is the declared fallback. There is never a root daemon.
3. **Guest.** It uses a pinned aarch64 kernel and a read-only rootfs from the **base recipe**. The guest's mutable state is guest RAM. The task commit enters as a bundle over the vsock-UDS control channel, never through a host share. The guest has no NIC by default.
4. **Control.** User-space vsock backed by a per-VM UDS in the outer tmpfs (`vsock_uds`), or `serial`. The guest runs the **same non-inferential gate runner**. No AI runs in the guest.
5. **Lease, parity and scale.** These are unchanged from v2 §8: the shared `scheduler_lease`, wender parity (WP1-06 AUSENTE), K swept to the knee, and failure injected at every transition.

## 9. Threat model and failure modes

Trusted: operator policy, approval file, host seat, collector, kernel and daemon. Distrusted: **everything inside the capsule or guest**, plus recipe diffs and model output. All v1 and v2 rows are kept (v2 §9). v3 adds or replaces:

| Threat/failure | Required defense and observable outcome |
|---|---|
| Per-commit image sprawl and repo code persisted on disk (A1 residue) | Superseded. One base image, `repo_code_in_image 0` (const), and code only in tmpfs, purged at task end. The checks are `docker images` showing ≤ 2 `os-worker-base` digests and no `<repo>@<sha>` tag. |
| Untrusted code reaches the host filesystem | Zero bind mounts (const), read-only root and tmpfs only. Probes `ls /home/user` → absent and `touch /etc/probe` → read-only (GC `GC:171`, `GC:247`). |
| Credential introduced by env or file | Env names come from a closed enum, `env_file` is const null, the scan `env_secret_names 0` (GC `GC:248`), and the seat never passes `-e` through. |
| Docker socket gives the capsule host control | No field can mount it. Probe `runtime_socket_visible false`. Rootless or dedicated daemon, so even a leaked socket is not host root. |
| Inference or model client inside the capsule | No inference verb, `runtime.inference: host` (const), probe `inference_processes 0`, and no network. |
| Swap pages untrusted code to NVMe | memswap = mem (`GC:161`). Probe `memswap_equals_memory`. |
| Capsule starves the operator's interactive work | cpuset disjoint from `interactive_cpus`, reduced `cpu.weight`, `oom_score_adj` ≥ 500 (`GC:158`, `GC:164`) |
| Fork bomb or descriptor exhaustion | `pids_limit`, `nofile`, `nproc` (`GC:163-166`) |
| Egress or lateral movement over the bridge | `none` by default. Allow-list only with operator host rules: `OSW-EGRESS` plus `OSW-HOSTGUARD` (`GC:206-227`) and a probe of an off-list target expecting BLOCKED. |
| Cache poisoning across tasks | The cache is read-only to tasks, written only by the operator-gated warm job, and its manifest digest is bound in every receipt. |
| Exfiltration or binary smuggling through results | Tar-exec, text only, 0644, owner asserted, no residue, never `docker cp` (`GO:137-141`, `GO:332`) |
| Orphans or zombies survive a task | tini subreaper. Probe `orphans_after_task 0` (`GC:112`; `GO:250`). Purge plus `docker rm`, then janitor reconciliation. |
| Codec relabels an OS run as another surface (JW8-01) | `surface` is never copied. Live encode is refused with rc 4 unless `os-capsule`, and parity envelopes are refused by the outbox writer. |
| Base recipe poisoning or unreviewed change | Recipe lint gives rc 5, a digest outside the approval file gives rc 7, and two builds must give the same digest |

## 10. Phased build plan, measured checks and must-FAIL (A4.4)

### 10.0 Measured checks required on **every** card that starts a capsule

| ID | Check (host-run, read-only) | Expect | Guide |
|---|---|---|---|
| C1 | `docker inspect <c>`: `HostConfig.ReadonlyRootfs=true`, `CapDrop=[ALL]`, `SecurityOpt` ∋ `no-new-privileges:true`, `Privileged=false`, `NetworkMode=none` (or `br-osw` with a rules record), `Binds=null`, `Mounts` = tmpfs plus `os_worker_cache` ro only, `Devices=[]` (microVM phase: exactly `/dev/kvm`), `Memory == MemorySwap`, `PidsLimit`, `Ulimits nofile`, `CpusetCpus` ∩ interactive = ∅, `OomScoreAdj` ≥ 500, `Config.User=10001:10001`, `Init`/tini, `Config.Env` names ⊆ allowlist | all hold; the inspect JSON is digested into `probes.inspect_sha256` | GC `GC:154`, `GC:253`; GO `GO:57-60` |
| C2 | `docker exec … touch /etc/probe` | `Read-only file system` → `etc_write: read_only` | GC `GC:171`, `GC:252` |
| C3 | `docker exec … ls /home/user` | `No such file or directory` → `home_admin: absent` | GC `GC:247`; GO `GO:251` |
| C4 | Egress probe to an off-list target and to the host gateway `:22` | `expect=BLOCKED got=BLOCKED` → `egress_probe: BLOCKED` | GC `GC:233-234`; GO `GO:286-291` |
| C5 | Env scan for `key\|token\|secret\|passwd\|anthropic\|gemini\|openai`; socket and daemon check (`docker info` security options ∋ rootless, or the daemon user ≠ root) | `env_secret_names 0`, `runtime_socket_visible false`, `daemon` rootless\|dedicated_user | GC `GC:248` |
| C6 | After purge: `ps -eo stat=` in the capsule before removal, then `docker ps -a --filter label=w8.lease=<id>` and the RAM roots | `orphans_after_task 0`, 0 containers, 0 tmpfs bytes | GC `GC:112`; GO `GO:250` |

### 10.0b Must-FAIL trio, required on **every** card that renders a launch

1. **Bind mount refused.** Both a `binds` key and a host-path `volumes.source` give rc 5 (test `test_must_fail_bind_mount_env_file_and_docker_sock_are_refused`).
2. **Env-file refused.** An `env_file` key in the launch or exec record gives rc 5, and so does a non-allowlisted env name such as `ANTHROPIC_API_KEY`.
3. **docker.sock refused.** Both a `/var/run/docker.sock` volume and a `mounts` bind give rc 5.

These are the stdlib model cases. Cards from Phase 2 on also prove them **live**: the Go launcher refuses before `docker run`, and an independent `docker inspect` of any accidentally started container fails C1.

### 10.1 Phases

| Phase | Scope | Operator dependency | Measurable acceptance |
|---|---|---|---|
| 0: contract (this change) | v3 doc, schema, stdlib tests | **none** | Direct entry rc 0 with 78 tests and **`--no-skips` rc 0 on the host (0 skips, JW8-04)**. Off-host, `OS_CU_CANON_BRIDGE` points at the canon; without it `--no-skips` gives rc 5. Wrapped discover gives rc 0 with the 3 `__init__.py` markers and rc 5 without them. `--selftest-fail` gives rc 5. The must-FAIL trio passes in the model. |
| 1: bridge parity (codec) | `src/workerbridge/**` Go codec plus adapters against `bridge_core.check` | **none** | At least 20 paired cases with identical verdicts and Reject classes. Byte-identical round trip. The 9 PROTOCOL ids plus `role` are copied and `surface` is not. Must-FAIL: a `jules` task live encode gives rc 4 `unavailable`, and a parity envelope is never written to the outbox (JW8-01). Plus the v2 must-FAILs: multi-line gate, stale lease, null or 64-hex commit, artifact outside the surface. |
| 1b: base image | Recipe dir, local aarch64 build of `os-worker-base`, build record, warm-job recipe | **approval file present, plus the operator's per-build OK** | Two `--no-cache` builds give the same digest. Behavioural build assertions are green (`GC:116-128`). `docker run --rm … id` gives `uid=10001`. The image has no repository code (layer scan). Must-FAIL: the 5 leak recipes give rc 5, and an unapproved recipe gives rc 7. ≤ 2 retained digests. |
| 2: Docker headless slice | Go admission, launcher (`launch_spec` → argv), exec allowlist, ingest, gate runner, extract, purge, rootless daemon | **entry = Phase 1b green AND `/etc/opensuperficies/builder_approval.json` present (JW8-02)**; daemon mode set by the operator | 20 attempts across 2 repos. HEAD equals the pin every time. C1-C6 hold on every attempt. The live must-FAIL trio is refused by the launcher. No gate runs on the host. `inference_processes 0`. Every result crosses only via tar-exec (no `docker cp` in the launcher source; grep gate). |
| 3: containment and lifecycle | Policy negatives, janitor | as Phase 2 | Failure injected at every transition. 100 attempts at concurrency 4 leave 0 orphans, containers, listeners, tmpfs bytes and host-rule chains. TTL-to-stop is ≤ 2000 ms. Must-FAIL extras: swap, interactive core, `/dev/kvm` in docker, a writable cache and a binary extract each give rc 5. |
| 4: A2 mini-orchestrator and egress | Wave scheduler, OTP supervision, OpenSwitchboard mailbox and aggregator, vigia, **host-rule egress** | **operator applies the `br-osw` rules (separate act, `GC:237`)**; JW8-03 reconciliation done | N swept 2..16 with the knee reported (start 8). Zero cross-capsule leakage. Dedupe by repro hash. Per-wave cost and wall time. The flood must-FAIL is throttled. Give-up after N. C4 shows the off-list probe BLOCKED and the allow-listed target OPEN, with all rows PASS before admission. |
| 5: optional UI | `worker_ui` | as Phase 2 | Two capsules cannot reach each other's display. No frame bytes on disk. C1-C6 hold. |
| 6: A3 nested microVM | Outer base-image container plus K microVMs | KVM group or delegation set by the operator | Same Phase-1 corpus and verdicts. C1 shows `Devices` = `/dev/kvm` **only here**. The guest sees no host processes or mounts. K swept to the knee. Must-FAIL: guest egress outside the allow-list is BLOCKED and reported as `containment`. No inference process in the guest. |

**Phases 0 and 1 are the only phases that can start without the operator** (JW8-02).

Never label chat-environment or guide numbers as W8 host evidence (`AGENTS.md:11`).

### Verification scope of this change

The stdlib unittest checks structure, bindings and reference models only (no Docker, VMM, firewall or build). The gate is:

```sh
sh -c 'python3 -B -m unittest discover -s contracts/worker/tests -t . ; rc=$?; [ "$rc" -eq 1 ] && rc=5; exit "$rc"' && python3 -B contracts/worker/tests/test_worker_task_schema.py --no-skips && test -s docs/design/W8_WORKER_BANK.md
```

It requires the empty `contracts/__init__.py`, `contracts/worker/__init__.py` and `contracts/worker/tests/__init__.py`, which are added at merge.

## 11. Evidence answers

Unchanged. E0 is `db4efe99984235b27cfb672fe45096247f64649daab8bad0f688fbcd0a116579` (`INVESTIGACION_MICROVM_ARQUITECTURA_PERPLEXITY.md`). E1 is `# Computer Use (cubook) — W4 operator page (campaign os-cu-0, wave 4)` (`docs/CU_W4.md:1`).

## A1. Per-commit images: superseded by A4

A1.1, the single recipe shared by Jules, the builder and W8, survives: it becomes the base recipe plus a per-repo warm-cache manifest, so a task that passes in Jules still passes in a capsule given the same toolchain pins. A1.2-A1.3 (a per-commit build on every bridge move, capsules booting from `<repo>@<sha>`) are **withdrawn**. A1.4 (no secrets, no frames, reproducibility, the leak must-FAIL) is kept and strengthened with `repo_code_in_image 0`.

## A2, A3, A4

- **A2** is unchanged from v2 §A2, except that pods are A4-hardened capsules, the in-capsule agent is the non-inferential gate runner, and allow-list admission needs the host-rule record.
- **A3** is unchanged from v2 §A3 and §8, except that the outer image is `os-worker-base`, `/dev/kvm` appears only in that phase, and no AI runs in the guest.
- **A4** is folded as follows: A4.1 in §0, §3.1-§3.4 and the `runtime`/`workspace_spec`/`base_image_record`/`extract_record` defs; A4.2 in §1, §5 and `hardening`/`launch_spec`; A4.3 in §2, §3.5 and §8.1-§8.2; A4.4 in §10.0 and §10.0b.

## 12. Open implementation decisions

1. Add the three `__init__.py` markers at merge (outside this lane).
2. Canon WEB_BRIDGE: add `os-capsule` to the surface enum and `WRITER_SURFACES` (`bridge_core.py:38`). Until then, live OS encoding stays at rc 4.
3. Extend or wrap `scr-sbd-vigia-enjambre` with a flood throttle.
4. Write the base recipe directory and the per-repo warm-cache manifest (`.wavemind/env/`, AUSENTE).
5. Operator: create `/etc/opensuperficies/builder_approval.json` (AUSENTE on the host) with mode `per_build`.
6. Operator: choose rootless Docker or a dedicated daemon user, and measure cgroup v2 delegation (cpuset, memory, pids) plus `kvm` group access for it.
7. Operator: author the `br-osw` host-rule script (`OSW-EGRESS`/`OSW-HOSTGUARD`, apply/verify/remove) on the GC §6.2 pattern. It is never applied automatically.
8. Qualify the VMM and hybrid vsock under the chosen daemon mode. Run wender lease parity.
9. **JW8-03, before Phase 2:** reconcile `scheduler_lease` with `src/orchestratorbook/arrendador` (`LeaseSession`, fencing epoch, `arrendador.go:28-29`, front end `cmd/srf-arriendo`), and admission with `src/orchestratorbook/gobernador` (`SrfSolicitudAdmision`, `gobernador.go:16-24`; memory estimator `estimador.go:3`). Also reconcile the microVM launcher with `cmd/srf-capsula-microvm`. Reuse them, or record why not. A duplicate lease service or governor is refused at review.
10. **JW8-05:** the v2 merge pinned the test digest `ce843df8bf1e8ca500df4f111804af77f0abf6cfbf4ad56f30395d447a17f056`. The v3 test digest is recorded in `AMEND3_W8.md` and `REPORT_A3W8.md`, and the merge must pin exactly that value.

### Recommended first card: W8-B1 (Phase 0 merge plus Phase 1 codec), scope updated for v3

- **Scope.** Drop in the **v3** contract. Add the Go task↔bridge codec and the receipt→`bridge-result-v1` encoder with JW8-01. Add the Go `launch_spec` validator **as a pure function** (no `docker run`): it is contract code, and Phase 2 reuses it. No runtime, Docker, VMM, image build or firewall.
- **Write paths (OpenSuperficies):**
  - `docs/design/W8_WORKER_BANK.md` (the v3 text)
  - `contracts/__init__.py`, `contracts/worker/__init__.py`, `contracts/worker/tests/__init__.py`
  - `contracts/worker/os.cu.worker-task.v1.schema.json`
  - `contracts/worker/tests/test_worker_task_schema.py`
  - `src/workerbridge/**` (new), `testdata/workerbridge/**`
- **Acceptance:**
  1. The §10 gate gives rc 0. `--no-skips` gives rc 0 on the host. `--selftest-fail` gives rc 5.
  2. `go test ./src/workerbridge/...` gives rc 0 under `taskset -c 10-14`.
  3. At least 20 paired cases give the same verdict and Reject class from the Go codec and the canon `bridge_core.check`.
  4. The byte-identical round trip holds, and changing any of the 9 fields refuses or rebinds.
  5. Must-FAIL: multi-line gate, stale lease, null or 64-hex commit, artifact outside the surface, a non-`os-capsule` live encode (rc 4), and the **launch-spec trio** (bind, env-file, docker.sock refused by the Go validator with the same verdicts as the stdlib model).
  6. The diff stays inside the write paths, with no CDN, credentials or frames.
- **Operator.** None needed.

STATUS: DONE
