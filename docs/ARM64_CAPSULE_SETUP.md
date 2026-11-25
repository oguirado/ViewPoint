# Local ARM64 capsule setup

## What does this setup do?

It registers operator-provided local assets so the ARM64 graphical VM and MicroVM can be offered in the normal app. It does not download or install anything, export images, run a guest, or grant distribution rights.

Answers: what setup does and does not do; what "VM" and "MicroVM" mean here; which commands run it.

- Graphical VM: the configured graphical guest adapter. MicroVM: bounded guest commands with visible output, not a graphical desktop or host shell.
- The app, adapter, native tools, image, kernel and guest artifacts must already exist with independently verified provenance.
- Run from the source tree that matches the packaged app (subcommands: `doctor`, `configure-local`, `status` [CITED: `tools/arm64_capsules.py`]):

```sh
python3 tools/arm64_capsules.py doctor --descriptor /absolute/private/setup-input.json
python3 tools/arm64_capsules.py configure-local --descriptor /absolute/private/setup-input.json
python3 tools/arm64_capsules.py status
```

The descriptor is a regular owner-private JSON file. Configuration verifies every pin, the real ARM64 KVM API and the local Docker image, then publishes an atomic, non-replacing registry at `$XDG_DATA_HOME/metaviewer/capsules/arm64/setup.json` (base `~/.local/share` when unset). An existing registry is never silently overwritten. A missing or invalid prerequisite refuses configuration.

## How do I build the broker for local registration?

Answers: which build produces the ARM64 command broker; which artifact to pin in the descriptor.

From the same reviewed source revision used to build the production app and adapter, run the broker build with an absolute output directory:

```sh
daemon-adapter/guest-command-broker/build-arm64.sh /absolute/broker-target
```

To build the package and its separately registered broker together, set `MV_ARM64_BROKER_TARGET_DIR` to that absolute directory when running `scripts/bootstrap.sh`. The broker build uses `-C target-feature=+crt-static -C relocation-model=static` and produces `/absolute/broker-target/aarch64-unknown-linux-gnu/release/guest-command-broker`. Use that file and its SHA-256 for `artifacts.broker`. Both runtime validators reject `PT_DYNAMIC` and `PT_INTERP` [CITED: `daemon-adapter/guest_command_runtime/native_checks.py`, `daemon-adapter/guest_command_runtime/image.py`].

## What does the descriptor contain?

A closed JSON object, format `metaviewer-arm64-setup/v1`. Paths are canonical absolute paths with no symlink components. A pin has exactly `path` and lowercase `sha256`.

Answers: which fields the descriptor requires; what `legal` must say; what optional Browser/Program recipes do.

| Field | Content |
|---|---|
| `schema` | `metaviewer-arm64-setup/v1` |
| `source_commit`, `source_root` | Reviewed commit and installed controller source root. A supplied commit is a provenance assertion, not a signature. |
| `package` | `app` and `asar` pins; the ASAR must be the app's sibling `resources/app.asar`. |
| `python`, `python_prefix` | Interpreter pin and canonical installed prefix directory. |
| `artifacts` | Pins `engine`, `kernel`, `broker`, `busybox` (ARM64 ELF, or raw ARM64 kernel). |
| `native` | Pins `srf-identity-admin`, `srf-identity-surface`, in one binary directory. |
| `runner` | Pin of `daemon-adapter/tools/guest_command_runner.py` under `source_root`. |
| `files` | The module closure below; each entry has `relative`, `path`, `sha256`. |
| `vm_image` | Exact local Docker image ID in `sha256:` form, never a mutable tag. |
| `legal` | Local-use statement, below. |
| `normal_profiles` | Optional Browser/Program recipes, below. |
| `producer_pins` | `setup` (packaged `tools/arm64_capsule_session.py`), `graphical` and `guestcommand` (both the packaged `resources/bin/mv-daemon-adapter`). |
| `package_manifest` | Pin of the external package `.sha256` manifest. |
| `reconciler` | Pin of packaged `tools/arm64_capsule_reconcile.py`. |
| `identity_provisioner` | Optional pin of packaged `tools/mv_identity.py`; required when a Browser recipe is set. |

Module closure, relative to `source_root/daemon-adapter`: `guest_command_runtime/` files `__init__.py`, `_schemas.py`, `wire.py`, `schema.py`, `config.py`, `image.py`, `native_checks.py`, `native.py`, `protocol.py`, `storage.py`, `controller.py`.

`legal` is closed: `scope: "operator-provided-local-only"`, `operator_local_use_confirmed: true`, `redistribute: false`, and `assets` with `engine`, `kernel`, `broker`, `busybox`, `vm_image`, `native`. Each asset has nonempty `license`, `source`, `evidence` strings stating actual facts; unknown rights stay explicitly unknown. Operator confirmation does not authenticate licences or authorize redistribution. Keep private source, credentials and personal descriptors out of the public repository. See [ARM64_CAPSULE_ASSET_NOTICES.md](ARM64_CAPSULE_ASSET_NOTICES.md).

`normal_profiles` holds only `browser` and `program`. `browser` has `image_id`, sorted unique `domains` and `legal` facts. `program` has `image_id` and `legal` facts and requires a Browser recipe. Image IDs must be exact, locally present ARM64 images. Each recipe creates a fresh owned identity and provisions only its own empty payload. Only the Browser template is published as a static native profile; the existing dynamic Program route uses it. Recipes create no Docker networks, and an image being present does not mean a browser or program runtime was tested.

File checks: the app, interpreter and native binaries must be protected executable ARM64 ELF files. Before registry publication or a held session, a bounded isolated interpreter probe checks the prefix and required standard-library imports. Helper and source files must not be group/world writable (for example mode 0644). Setup never chmods existing artifacts.

## Packaged launcher interface

The packaged wrapper starts `tools/arm64_capsule_session.py`; users do not write its arguments or the transport file.

Answers: how the launcher starts a session; which limits it enforces; which settings are internal wrapper data.

```sh
python3 tools/arm64_capsule_session.py --app /absolute/package/metaviewer --source-root /absolute/public-source --ui-transport /absolute/private/ui-transport.json --expected-commit <sealed-package-commit> --launcher-pid <parent-pid> --launcher-start <parent-kernel-start>
```

- The helper revalidates the registry and enters its own fresh guardian scope: MemoryHigh 6 GiB, MemoryMax 8 GiB, no swap, 200% CPU (two CPUs), TasksMax 256, and AllowedCPUs 0–15 [TARGET]. It checks the effective limits before provisioning [CITED: `tools/arm64_capsule_session.py`]. It requires 32 GiB of free memory (the combined-admission floor) [CITED: `tools/arm64_capsule_session.py:895`]. It does not raise native or daemon limits.
- The wrapper enters the helper with the canonical system Python, passes `--expected-commit`, and a mismatch with `source_commit` refuses before interpreter probing, held-marker publication or provisioning. The helper verifies the supplied launcher PID and start ticks identify its actual parent before creating state.
- The wrapper writes the owner-private transport JSON: `schema: "metaviewer-arm64-ui-transport/v1"`, `mode` (`"trusted-user-invocation"` or `"private-ui"`), `display`, `xauthority`, `dbus_session_bus_address`. No ambient display, auth or bus setting is inherited. Private UI needs an owned Xauthority file and a private owned bus socket; trusted invocation may use the normal user bus. Xauthority metadata is checked; credential bytes are never read or copied.
- Manager calls use the real UID's `/run/user/<uid>` bus. The app runs on the admitted UI bus with private RAM HOME/XDG/TMP directories. An inherited `MV_HOSTED=1` stays set.
- It creates a fresh private RAM directory, native identity and payload, then applies the CHOWN-only constrained helper to that exact empty payload. No existing identity is borrowed. Unknown outcomes keep the journal and refuse replay.
- A shared registered agent instance sets the guest-command owner. The host writes the protected guest-command profile and projects `MV_ARM64_CAPSULES=1`, `MV_GUEST_COMMAND_PROFILE`, `MV_GUEST_COMMAND_AGENT_INSTANCE` and the native VM profile. It never sets `MV_GUEST_COMMAND_ONLY` or `MV_HEADLESS_ONLY`. Renderer choices do not control host paths, owner, images or native commands.
- After real host checks and fresh provisioning, the child gets `MV_ARM64_CAPSULE_STATUS={"vm":{"available":true,"reason":""},"microvm":{"available":true,"reason":""}}`. The UI must intersect it with live profile checks; it does not replace backend admission or prove a guest is running. Setting these flags by hand is not a setup procedure.
- The helper does not create a graphical capsule. The UI waits for an actual Add capsule action. Launch without a registry is unchanged.

### Rewind in the normal wall

Pass `--rewind` to `arm64_capsule_session.py` to enable Rewind for MicroVMs in the normal wall. VM and MicroVM stay selectable in Add capsule; nothing is created automatically.

Answers: how to turn on Rewind in a normal session; what it still requires; how it differs from the developer mode.

- It still needs the full protected ARM64 registry, the exact package commit and the UI transport above. It is not the exclusive developer `MV_REWIND_ONLY` mode.
- The helper takes the Rewind runner and module hashes from the verified package manifest, reuses the admitted artifact pins and records the mode in the private session plan. The resource inventory and held-session fence stay active.
- A shorter, exclusively created runtime path avoids Unix socket path limits; a colliding directory is refused.
- What Rewind restores and what it cannot undo: [ARM64_MICROVM_REWIND.md](ARM64_MICROVM_REWIND.md).

## When can a second session start?

The packaged desktop launcher waits for proven closure. Its durable
`held-session.json` is the admission fence. App exit alone never releases it.
The explicit [Medicion ramp](#capacity-ramp-for-medicion) admits concurrent
sessions under their own custody. While the ramp has an unresolved reservation,
the packaged desktop launcher refuses admission.

Answers: what blocks a new session; how teardown works; what is never deleted or signalled.

- A MicroVM cannot be created while any surface capsule, including a browser or an unresolved creation, is held. A missing VM window does not show that surface resources closed.
- PLAN records the launcher and outer-helper process, start and cgroup identities. The outer helper catches TERM, INT and HUP and publishes private `STOP_REQUESTED` instead of abandoning the scope. The guardian checks both identities and that record, including loss by SIGKILL, and rechecks custody before each native or helper command.
- The app is ended only through its owned `Popen` after matching PID, start and cgroup. Teardown waits five seconds, then records unknown ownership [CITED: `tools/arm64_capsule_session.py`]. A bounded fallback may stop only the freshly matching own guardian invocation, cgroup and inode. No foreign or reused process, broad process group, Docker name, shared directory or guessed unit is signalled or removed.
- Release needs complete producer seals, complete allocation bindings and fresh source-bound resource-absence proofs. Legacy event files, missing producers, partial records, changed pins or unknown outcomes keep the marker. Deleting it by hand is not supported cleanup. Reconciliation deletes no roots or payloads.

## How are allocations tracked?

Each session snapshots the verified registry bytes to a private `setup.json` and creates `inventory-config.json`, `allocations.lock` and an append-only, hash-chained `allocations.jsonl`.

Answers: which files record allocations; who writes them; what happens on write failure.

- The protected config binds registry, package manifest, ASAR, helpers and all producer pins. It reaches the adapter as `MV_ARM64_INVENTORY_CONFIG`. Native and guest-command producers must use the same journal.
- The setup producer records the session header, guardian invocation and cgroup, CHOWN helper pending/bound/removed transitions, and app intent before `Popen`, then its PID/start/cgroup. A helper counts as removed only after verified exit, payload ownership, removal and current absence.
- Write failures keep responsibility and are never replayed automatically.
- The setup producer seals after app exit. The outer helper releases its lock, requests the coordinator seal and runs the trusted reconciler. The marker stays during lock reacquisition. For normal and Rewind desktop launches, the outer helper removes its private RAM root after the reconciler saves the proof and releases custody. Cleanup errors produce a failure status and the proof remains in the registry directory.
- Dynamic identity provisioning through `tools/mv_identity.py` also creates Docker payload helpers. In normal mode the pinned hook writes pending before Docker, binds the protected CID file and verifies absence before `removed`. It uses only the protected browser-image ID, never a mutable tag. A sealed or invalid inventory refuses before helper execution.
- Known limitation: direct identity-tool surface login/release outside the Go manager write a permanent global unknown before their effect, and direct profile deletion also keeps unknown. These block automatic session release. Metadata-only admin create/show is exempt. This is not exhaustive native lifecycle coverage.

## What is not proved by this setup?

Configuration checks hashes, custody and recognizable ISA headers. It does not check kernel compatibility or an authenticated guest hello, and source and package pins still need independent release verification.

Answers: which checks are missing; what happens to Browser/Program without recipes.

- These tools and their synthetic tests show no new guest execution, installation, runtime acceptance or distribution approval.
- Existing Browser/Program profiles are never borrowed from another native store. Without explicit recipes, combined-session Browser/Program stays blocked. Their real runtime regression is unproved by these tests.

## Which desktop session bus does a normal ARM64 session need?

A root-owned, non-writable `xdg-dbus-proxy` in `/usr/bin`. The helper refuses before admission if it is missing or unsafe, and never installs it [CITED: `tools/arm64_capsule_session.py`].

Answers: which host prerequisite the desktop session needs; what the app can reach through it.

The app receives a filtered bus endpoint. The allowlist includes notifications, accessibility, settings and GTK file services. It excludes the user manager and desktop portal [CITED: `tools/arm64_capsule_session.py:770`]. This is the configured access policy, not proof that each desktop service works on every host. The proxy is tied to app cleanup, and release still requires evidence that the original guardian cgroup is absent.

## How does a host agent work with a MicroVM tile?

The tile shows its workload Capsule ID and opens a paired terminal on the host, not in the guest. Guest commands go through the authenticated SDK ([ARM64_GUEST_COMMANDS.md](ARM64_GUEST_COMMANDS.md)).

Answers: what the paired terminal is; who submits the task; what happens when an agent joins late.

- Direct task lists installed host agent profiles and prepares the current binding plus the shipped `docs/AGENT_ENTRY.md` MicroVM instructions. The person enters the objective and submits it; the app never sends Enter. Automation creation and workflow replay are not offered for this target. Back stays with the person.
- Host agent authentication stays in the OS account home; the app and guest keep private homes.
- A late agent gets a bounded replay of authenticated observations for the current guest, not a guessed state or a second guest. Missing or exhausted coverage refuses adoption.
- Closing the agent channel does not close the guest. Stop capsule and app shutdown use the normal reconciliation. Each admitted app can hold one MicroVM at a time. Graphical and command workloads exclude each other within that app [CITED: `core/src/serve/guest_command.rs:74`].

## Capacity ramp for Medicion

Run `tools/mv_capsule_ramp.py open --n N --scope-unit U --profile-root DIR --display :K --ui-transport F --launcher-grant G`
in the named user scope. Use `MemoryHigh=6G`, `MemoryMax=8G`, and an existing
owner-private directory on the root-owned sticky `/dev/shm` mount. The display
must be at least `:90` [TARGET]. Leave ambient `DISPLAY` unset or set it
to the exact `--display` string. The owner-private UI descriptor must use
the same local X server and may specify its screen.

Live launch requires the operator registry at
`$XDG_DATA_HOME/metaviewer/capsules/arm64/setup.json`, with `~/.local/share`
as the default data home. Register a package with the matching M7 helper and
reconciler. The operator supplies the owner-private grant with schema
`metaviewer-arm64-launch-grant/v1`, `launcher_pid`, `launcher_start` in
kernel ticks, `launcher_cgroup` from the unified cgroup record and
`registry_sha256` for the exact registry bytes. Use the process that starts
the ramp as its actual parent and keep that launcher alive through the run.
The ramp and M7 both verify the grant and kernel identity. The operator
publishes the grant from the live coordinator’s `launcher-request.json`
after independently checking its identity. Approval names the workloads
permitted across the admitted apps.

For C01, invoke `tools/mv_c01_launcher.py --coordinator /absolute/C01/harness/live.py
--approval /absolute/C01/approval.json --out /absolute/C01/new-phase` in the approved
coordinator scope with its memory and CPU limits. The launcher exclusively
creates the approved `/dev/shm/<run-id>` root and owner-private block directories.
After successful completion it archives settled ramp ledgers to the phase
output and records directory removal in the sibling `<out>.runtime-removal.json`.
A failed coordinator leaves the runtime available for recovery.

Admission preflights the full request and takes each reservation under a
short registry lock before launching. Every slot reserves the guardian's
`8 GiB` cap, and host `MemAvailable` minus all reservations must be at
least `20 GiB` [TARGET]. M7 also retains the `32 GiB` host minimum
[CITED: `tools/arm64_capsule_session.py`]. Each run has its own guardian,
RAM root, inventory and journal. The registry stores its
`multi-launch/RUN/held-session.json` and `multi-launch/RUN/session.lock`.
A reservation remains charged while closure is unproven.

The ramp records the outer helper's PID, kernel start time and process group
in `ramp-state.json` before allowing the helper to launch. Success returns
`session_id`, `pid`, `starttime`, `pgid`, `rss_kib`, `ready_ms` and
`ready_witness` for each session, plus `peak_reservation_kib` and
`stop_owner`. With `m7_app_bound`, `app_pid` and `app_rss_kib` identify
the application. The witness establishes app binding. Use graphical Add
capsule to create a capsule in that app.

`status --state F` reads the owner-private state.
`close --all-recorded --state F` signals each bound M7 helper through its
pidfd after checking PID, kernel start time, process group and cgroup. The
helper receives TERM and carries out the existing M7 stop and reconciliation
protocol. An unresolved session keeps its state and reservation. The close
witness links to the reconciliation proof and distinguishes a ramp profile
directory removed during close from one already absent. M7 proves owned
process, guardian and container absence before removing the held marker.
It retains the session root and journal for inspection.
