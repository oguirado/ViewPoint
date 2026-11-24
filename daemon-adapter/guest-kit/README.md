# AArch64 headless SUMSQ guest kit

The kit supplies the private controller closure for the `headless-sumsq` template and its `headless-sumsq-v1` profile. The H1-ENTRY baseline is `a5000a6a236ae32b12188d16313db74994966af2` [MEASURED].

## H1-ENTRY contract

| Boundary | Required behavior |
|---|---|
| Host profile | `mv-headless-host/v1` pins Python, `tools/headless_runner.py` and the exact source closure below. Its `mc06-host-config/v1` template supplies the same owner and runtime root, generation within the JSON safe integer range, explicit `runtime_admitted`, SHA-256/path pins for `engine`, `kernel` and `init`, `256` MiB and `1` vCPU, and distinct startup, job and stop deadlines of `1` to `30` seconds each [CITED]. [profile.go:158](../internal/headless/profile.go#L158) · [profile.go:195](../internal/headless/profile.go#L195) · [profile.go:260](../internal/headless/profile.go#L260) |
| Runner | The manager writes `headless-reservation.json`, snapshots the bundle, runner and interpreter, and records `.prepared` before startup. The isolated runner verifies the pins and private directories, replaces `external_cgroup` and `runtime_root`, loads `arm_backend` before `contract_guard`, and invokes `Controller(config, state_dir)`. It emits `mc09_ready`, calls `handle(request, owner_id)` and encodes events with `encoded` on stdout. `MAX_RECORD` is `65536` bytes [MEASURED]. `close()` runs on stream teardown. [manager.go:102](../internal/headless/manager.go#L102) · [headless_runner.py:53](../tools/headless_runner.py#L53) |
| Admission and identity | A false admission flag produces `UNAVAILABLE` before adapter process creation. Core correlates the authenticated owner, stable workload ID and generation to `headless-sumsq-v1`, `firecracker` and `aarch64`. The manager records `.bound` on the initial correlated `accepted`. [manager.go:190](../internal/headless/manager.go#L190) · [manager.go:257](../internal/headless/manager.go#L257) · [headless.rs:64](../../core/src/headless.rs#L64) · [headless.rs:186](../../core/src/headless.rs#L186) |
| Requests and events | `headless_request/v0.2` admits create for the fixed template, run for an owned workload/generation and one [CITED] `sumsq` job with integer `n` in `0..10000` [CITED], and stop for the same identity. `headless_event/v0.2` yields a correlated `refused` with `effect:not_dispatched` or starts with `accepted` and follows with a terminal event. `headless_session/v0.2` preserves the bound identity. [Request schema](../../contracts/v0.2/headless_request.schema.json#L6) · [Event schema](../../contracts/v0.2/headless_event.schema.json#L6) · [Session schema](../../contracts/v0.2/headless_session.schema.json#L37) · [manager.go:239](../internal/headless/manager.go#L239) |
| Outcome and teardown | Create moves `starting` to `ready`, run moves `running` to `completed` with the verified sum, and stop moves `stopping` to `stopped/released` after closure. An uncertain operation keeps ownership `held`, and an uncertain run records `job.status:unknown` and a null value. Only a distinct stop may proceed through an intact stream after uncertainty. The manager separately verifies service exit before writing `.closed`. [headless.rs:200](../../core/src/headless.rs#L200) · [headless.rs:265](../../core/src/headless.rs#L265) · [manager.go:198](../internal/headless/manager.go#L198) · [manager.go:292](../internal/headless/manager.go#L292) |

The bundle root contains these exact paths. The schemas are byte-identical to H1-ENTRY. The reference model uses the byte-pinned `mc05-contract-candidate/reference_model.py` from the x64 capsule kit. The native helpers derive from H1-ENTRY's `guest_command_runtime/native_checks.py`. [Pinned source loader](mc06-controller/contract_guard.py#L87) · [ARM native helpers](mc04-runtime/arm_harness.py#L1)

- `mc06-controller/controller.py`
- `mc06-controller/arm_backend.py`
- `mc06-controller/contract_guard.py`
- `mc04-runtime/arm_harness.py`
- `mc05-contract-candidate/reference_model.py`
- `mc05-contract-candidate/contracts/v0.2/headless_request.schema.json`
- `mc05-contract-candidate/contracts/v0.2/headless_event.schema.json`
- `mc05-contract-candidate/contracts/v0.2/headless_session.schema.json`

The controller exclusively creates `journal.jsonl` under the private attempt state directory. It synchronizes the chained intent and accepted event to disk before native dispatch. The backend preflights AArch64, KVM and dedicated cgroup custody, loads pinned bytes into private runtime files, and configures `/machine-config`, `/boot-source`, `/vsock` and `/actions` over the Unix API. It records Firecracker PID and start ticks and verifies the guest's nonce and AArch64 `UNAME`. Attempt state remains available for reconciliation. [Controller](mc06-controller/controller.py#L34) · [Backend](mc06-controller/arm_backend.py#L81)

The guest derives a per-boot nonce from `mv_nonce` and listens on AF_VSOCK port `19001` [MEASURED from source]. Each connection starts with `HELLO` and the nonce. The protocol also admits `PING`, `UNAME`, one `SUMSQ` per boot [MEASURED from source] and `STOP`. A valid stop acknowledges `STOPPING` and powers off the guest. The backend independently checks the sum, requires the reaped child's exit for a normal stop and records observed prior exit separately from forced owner shutdown. The protocol derives from `tools/x64_capsule_kit/runtime-prototype/init.c` on MetaViewer's `mv/codex-microvm-x64-1103` at `6b0f398fe23c74f320abf78794abcaa37a406c6c` [MEASURED]. [Guest protocol](guest/init.c#L93) · [Backend stop](mc06-controller/arm_backend.py#L142)

## Build and verification

From the worktree root, run the tests with the pinned H1-ENTRY baseline as the argument. Each Python test fails on that baseline's missing kit source and passes against this kit. The C test compilation fails against the baseline's missing init and passes against this kit with the socketpair protocol controls. The fake Firecracker supplies the Unix listeners and guest replies. The isolated runner fixture substitutes the service-cap check because the enclosing test scope uses the requested test caps.

```sh
systemd-run --user --scope -p MemoryHigh=6G -p MemoryMax=8G \
  unshare -Urn bash --noprofile --norc daemon-adapter/guest-kit/tests/verify.sh \
  "$H1_ENTRY_WORKTREE"
```

The build command inside that scope is shown below. The AArch64 GCC version was `13.3.0` [MEASURED]. Both builds produced static little-endian ELF64 AArch64 executables with no `PT_INTERP` or `PT_DYNAMIC` and SHA-256 `e72da722f5c4fe12b8c620d3482016dc04e869a72dff56cce4d04b378fe1f1e6` [MEASURED]. The binary and local verification output live under the ignored `.build/` directory. [Verification driver](tests/verify.sh) · [Guest init](guest/init.c)

```sh
kit=daemon-adapter/guest-kit
mkdir -p "$kit/.build"
aarch64-linux-gnu-gcc -std=c17 -Os -Wall -Wextra -Werror -static -no-pie \
  -Wl,--build-id=none -s "$kit/guest/init.c" -o "$kit/.build/init-aarch64"
sha256sum "$kit/.build/init-aarch64"
```

## Guest kernel and engine provenance

The preferred approval source is the published AArch64 guest configuration for the selected Firecracker release, with the corresponding Linux source revision selected for that configuration. The locally authored [required settings](guest/firecracker-arm64.config) make initrd, gzip, ELF, procfs, virtio-mmio and VSOCK built-in on a little-endian, 4 KiB-page [CITED from H1 native checks] ARM64 image. The build script overlays the exact fragment, runs `olddefconfig`, verifies every required setting survived, builds the raw `Image` and prints the hashes of the base, overlay, resolved `.config` and image. [Firecracker guest configs](https://github.com/firecracker-microvm/firecracker/tree/main/resources/guest_configs) · [Firecracker releases](https://github.com/firecracker-microvm/firecracker/releases) · [Upstream Linux stable source](https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/) · [Kernel build script](guest/build-kernel.sh)

```sh
daemon-adapter/guest-kit/guest/build-kernel.sh \
  "$LINUX_SOURCE" "$APPROVED_FIRECRACKER_ARM64_CONFIG" "$NEW_BUILD_DIR" ""
```

The approved Firecracker release and binary, immutable upstream config pin, exact Linux source revision and digest, generated kernel `.config` and `Image` are **AUSENTE — no inventar**. The kernel test exercises config acceptance and rejection using a fixture. Native VM boot and the protected profile binding require the operator-selected artifacts and approval.
