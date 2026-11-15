# VM capsule image and relay

The VM image is `opensuperficies-vm-arm64:0.1.0`. Its static, Linux arm64
`cmd/srf-vm-entry` binary also serves as `identity-entrypoint.sh`, `xdotool`,
`python3` and `srf-fbread` through image symlinks. Empty entrypoint argv boots;
`xpra-start` and `xpra-stop` relay to the guest. Other entrypoint arguments fail.
The shims send exact argv and only `proto.ForwardEnv` values. The guest remains
the authority for the generated helper allow-list, stdin caps and deadlines.

PID 1 verifies baked `Image` and `root.raw` against `MANIFEST.sha256`, validates
the program/tiles/resources, atomically creates a journaled ext4 profile using
`mke2fs -L srf-profile`, and runs `e2fsck -p` before QEMU. Existing disk symlinks, wrong sizes,
failed formatting and uncorrected fsck errors stop boot. Creation uses a same-dir
exclusive temporary, fsync and rename under a profile lock. No disk frame sink
exists; Xvfb and frame storage belong to guest RAM.

Before fsck and again before launch, the host requires bare ext4 magic and the
exact `srf-profile` label. GPT signatures at byte offsets 512 or 4096 [CITED:
W3 boot room contract], or an MBR signature with any nonzero partition type,
refuse boot with exit code 6 [CITED: W3 boot room contract]. New images receive
the same validation before atomic publication. This prevents a writable profile
from supplying a competing root PARTUUID.

`src/vmsurface/launch.Args` is pure and pins virt-8.2/KVM, host CPU without PMU,
read-only baked root, writable profile, the sandbox flags, and the control/agent/
view serial ports. The kernel command line is exactly
`console=ttyAMA0 root=PARTUUID=c0ffee00-5a1e-4d0e-9e00-0e5f0a70c001 ro init=/init`
[CITED: W3 boot room frozen constant]. Profile-first device ordering remains as
defence in depth; root selection does not depend on device enumeration.
There is no network device or host guest-directory mount.
The profile M environment requests 4 vCPUs and 6144 MiB [CITED: W3 §10.4].
The container owns all serial connections. Local shim requests allocate distinct
agent stream IDs. The recorder is launched by the unchanged surface without
interactive stdin, so its initial local stdin EOF is suppressed; shim termination
or peer loss cancels it. Other commands forward stdin EOF normally.
Each view connection allocates a view stream; the concurrent
view limit is 32 [CITED: W3 §10.2]. Slow local consumers are disconnected rather
than blocking unrelated stream reads. Agent writes have bounded deadlines.
Guest-agent serves the view endpoint at `/run/srfview/xpra.sock`.

The control port receives one JSON line containing `SRF_PROGRAM`, `SRF_WINDOWS`,
`SRF_TILES`, `NO_AT_BRIDGE`. Only after both `SRF1 OUT xpra layer up` and
`SRF1 OUT PROGRAM_READY ...` arrive does PID 1 emit the two original lines, in
that order, each with one newline-terminated write. TERM/INT sends `SRF1 stop`,
allows a bounded shutdown, and kills/reaps QEMU if needed.

## Operator build preparation

No build, download, Docker invocation or VM runs as part of offline unit tests.
AUSENTE — Ubuntu digest, vendor packages and guest artifacts are not fabricated.
The checked-in Dockerfile contains `OPERATOR_DIGEST` until an explicit operator
fetch, and the recipe is deliberately unsealed until all artifacts exist.

1. Run `sh scripts/vm/vendor_fetch.sh --operator-run`. This network action prints
   its effect, pulls arm64 Ubuntu, resolves absent dependencies against that base,
   downloads QEMU/e2fsprogs packages, records `vendor/DIGEST` and `SHA256SUMS`, and
   pins the Dockerfile. Existing artifacts are verified or refused, never replaced.
2. Produce the guest artifacts with the GUEST lane build script.
3. Build the relay with `CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath
   -o build/room_CAPSULE/srf-vm-entry ./cmd/srf-vm-entry`.
4. Run `python3 -I scripts/vm/print_build.py --seal`. This binds every source,
   vendor file, guest artifact and static relay hash into `docker/vm/recipe.json`.
5. Install a root:root, regular, non-symlink approval file with mode 0644 [TARGET]
   at `/etc/opensuperficies/builder_approval.json`. Its schema is
   `os.cu.builder-approval/v1`, mode `per_build`, image the VM tag, arch `aarch64`,
   recipe_sha256 the exact recipe digest, approval_id matching `b2-[0-9]{4}` and
   operator_note a plain nonempty note. The printer uses the program builder's
   approval shape, with a separate VM approval path.
6. `sh scripts/vm/build_vm_image.sh` only prints the approved Docker command.
   The printer also inspects the pinned local base without pulling and requires
   Linux arm64. The operator executes the command. Docker uses pull=false and network=none, verifies
   vendor hashes before offline `dpkg -i`, and verifies baked guest hashes.

Any missing input, changed input hash, unpinned base, dynamic/non-arm64 relay or
nonmatching approval refuses with a nonzero code. Sealing grants no approval.
The binary and guest files are baked; no host `/guest` bind is needed.

## Relay deadlines and guest manifest bounds

The shim derives its deadline from `proto.Allowed`: policy plus one second
[CITED: W3 fix-round timeout table]. The policy/shim/wrapper seconds are:

| Operation | Policy [CITED] | Shim [CITED] | Wrapper [CITED] |
|---|---:|---:|---:|
| xdotool / srf-fbread / AX | 3 | 4 | 5 |
| paste | 10 | 11 | 12 |
| type | 28 | 29 | 30 |
| downloads | 18 | 19 | 20 |
| xpra-start | 110 | 111 | 120 |
| xpra-stop | 13 | 14 | 15 |
| record | unbounded | CANCEL | stream context |

The committed regression parses the real surface argv builders and helper
constants, and rejects deadlines equal to or above their wrappers. AX's direct
helper invocation has no timeout argv; its enclosing context currently permits
10 seconds [CITED: src/browserid/surface/ax.go], while the relay also satisfies
the stricter table bound. No surface builder is changed.

The baked manifest must fit within 4096 bytes [TARGET] and contain exactly one
`Image` and one `root.raw` entry. Verification reads the limit plus one byte and
rejects overflow before parsing or hashing, including a valid prefix padded with
whitespace that conceals a trailing entry. Duplicate, missing and extra entries
are refused.
