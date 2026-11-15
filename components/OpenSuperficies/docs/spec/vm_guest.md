# VM guest init and offline rootfs

The existing microvm path remains active when sysfs has no
`opensuperficies.agent` port. Its mounts, PID1 notification, signal handling,
zombie reaper and shutdown notifications are retained. No sealed guest artifact
is rebuilt by editing these sources.

With the agent port present, init requires all three named virtio ports. It
reads `/sys/class/virtio-ports/*/name` and maps each entry to `/dev/<entry>`;
`discoverPorts` accepts alternate sysfs/device roots for tests. Duplicate names,
missing companions and unreadable discovery data refuse boot. Init changes only
the agent/view device ownership to uid/gid 10001 and mode 0600.

The control device is switched to blocking reads before its scanner starts, so
an initially empty serial port cannot abort the boot wait with EAGAIN. The
control device supplies one bounded JSON line containing exactly the string
fields `SRF_PROGRAM`, `SRF_WINDOWS`, `SRF_TILES`, `NO_AT_BRIDGE`. The programs are
`notepad` and `notepad-vm`; windows are limited to 1–4 [CITED: program catalog].
Boot objects reject repeated keys. Tiles require exactly `id`, `x`, `y`, `w`,
`h`, with no repeated, unknown, case-folded or null fields; bounds and
non-overlap match the program entrypoint. An unavailable/invalid boot line refuses startup. The
boot-line limit is 64 KiB [TARGET], with a 30-second [TARGET] receive deadline.
Other environment variables are not inherited by guest children.

Init mounts tmpfs at `/run/vqa/fb` (64m, mode 0700, uid/gid 10001 [TARGET]),
`/run/newton-tenant`, `/tmp`, `/run/srfview` and `/run/user/10001`. It uses
`/sys/class/block/vd*` discovery over disks and partitions, reading each device's
ext4 superblock magic and exact `srf-profile` label. Exactly one match is
required; absent, duplicate, or unreadable candidates refuse boot and power off.
`/sbin/fsck -p <labelled-device>` runs before mounting ext4 at `/profile`. Corrected-filesystem
status is accepted; all other nonzero fsck statuses refuse the mount. The
profile root, exchange, cache and config directories are private to uid 10001.
The profile disk uses ext4 with a journal; its creation belongs to the capsule.
Frames remain in guest tmpfs and are never written into the root/profile disk.

Both child processes run in separate process groups as uid/gid 10001, with
supplementary groups cleared:

- `/usr/local/bin/srf-guest-agent --port <agent-device> --view-port <view-device>`
- `/usr/local/bin/identity-entrypoint.sh` with an empty argument list (the
  existing program entrypoint's boot command).

The agent owns exec and VIEW framing; each VIEW connection reaches
`/run/srfview/xpra.sock`. Complete program stdout lines are relayed as
`SRF1 OUT <line>`, preserving the readiness order and adding one newline. A
bounded line buffer prevents unbounded output. `SRF1 stop`, control EOF,
termination signals or a child exit end the runtime, terminate/kill its process
groups, sync and power off. Adopted children are reaped without racing the
`exec.Cmd` waits for direct children.

## Offline builder

`sh scripts/vm/build_guest_rootfs.sh` is an operator-run build; it is not invoked
by the offline unit tests. It requires the local images
`opensuperficies-program-gtk-arm64:0.1.0` and `srf-guest-builder`, the existing
`guest/Image`, host Python, Go and cached modules. It resolves local image IDs,
uses `--network none` for every container, disables Go module/toolchain fetching,
verifies helper freshness, and builds static Linux arm64 init/agent binaries.
It does not pull images, install packages online or modify `guest/`.

The host prepares adapted copies of the program entrypoint and xpra layer. Only
those guest copies replace `/srfview` with `/run/srfview`; the guest program map
aliases `notepad-vm` to `notepad`. The taken-XID rule is preserved. The offline
builder installs `/sbin/srf-guest-init`, `/init`, the agent, and an e2fsck-backed
`/sbin/fsck` wrapper with isolated builder libraries. It assembles a 3 GiB
[TARGET] ext4 filesystem, then wraps it with `root_disk.sh` into a GPT disk
with one Linux filesystem partition at LBA 2048 [TARGET], containing the entire
filesystem. Its fixed PARTUUID is `c0ffee00-5a1e-4d0e-9e00-0e5f0a70c001`;
the capsule selects it with native kernel `root=PARTUUID=` without an initramfs.
The builder requires util-linux `sfdisk`; it writes both GPT copies and copies
the filesystem at offset 1 MiB [TARGET]. It then publishes `build/vm_guest` atomically with read-only
`Image`, `root.raw`, `MANIFEST.sha256`, `PROVENANCE.json`. Provenance records
image IDs, binary/kernel hashes, source hashes, `root_partuuid`, filesystem size
and total disk size; the manifest lists exactly `Image` and `root.raw`. Existing output/scratch requires explicit removal
or relocation by the operator before a rebuild.

## Offline verification

Run scoped Go tests and vet under the room CPU/TMPDIR bounds. Tests inject fake
sysfs, mounts, fsck, process launches and a `net.Pipe` control device; they never
mount anything or start GUI/VM processes. A delayed serial fake first yields
EAGAIN and verifies blocking-mode normalization. Malformed boot-object tests
cover duplicate keys and the catalog window limit. The committed legacy golden
pins the mount sequence, stdout/control bytes, reaping and sync/power-off order
to `dfb68a6`, using injected operations with no host effects. `python3 -I scripts/vm/test_guest.py`
checks shell syntax, offline assembly constraints, isolated guest adaptations,
negative artifact manifests, and a tiny filesystem blob through the same GPT
assembly function. GPT tests validate partition identity, bounds, both headers,
entry checksums and exact payload bytes, and reject corrupted metadata. After an operator build,
`python3 -I scripts/vm/test_guest.py --artifacts build/vm_guest` verifies hashes,
provenance, GPT layout, root size and read-only root permissions. The rootfs and live boot
remain unmeasured until that operator phase.
