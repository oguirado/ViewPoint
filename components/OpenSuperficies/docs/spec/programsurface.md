# Program surface package

`src/programsurface` provides the compiled catalog and pure Docker launch and readiness helpers. It reads no runtime catalog files and imports no browser packages. The exported API in `api.go` is frozen by the P1 design. Frames are outside this package; no frame sink or disk writes are provided.

## Catalog

| Field | Contract |
| --- | --- |
| ID | `^[a-z][a-z0-9-]{1,23}$` [TARGET] |
| Image | `^opensuperficies-program-[a-z0-9-]+-arm64:[0-9]+\.[0-9]+\.[0-9]+$` [TARGET] |
| Network | `NetNone` or `NetProxy` |
| SeccompIdentity | Caller inserts the identity seccomp option when true |
| ClearSingleton | Caller clears browser singleton locks only when true |
| MinWidth, MinHeight | At least 200 pixels each, fitting the 1280 by 900 display [TARGET] |
| FixedWidth, FixedHeight | Both zero use tiling; otherwise each meets its corresponding MinWidth/MinHeight, fits the 1280 by 900 display, and requires MaxWindows = 1 [TARGET] |
| MaxWindows | Between 1 and 16 inclusive [TARGET] |
| ExchangeDir | Absolute child of `/profile/`, without `..` or NUL |
| A11y | Enable the accessibility bridge environment |
| Enabled | Disabled entries are refused by `Lookup` |

`notepad` uses `opensuperficies-program-gtk-arm64:0.1.0`, no network, no identity seccomp and no singleton cleanup. Its minimum tile is 400 by 300 pixels and its limit is 4 windows [TARGET]. Exchange uses `/profile/exchange`; accessibility and the entry are enabled.

`mousepad` is a disabled P1b stub with image `opensuperficies-program-office-arm64:0.1.0`; its other fields match notepad. `Lookup` returns a value copy. `Validate` checks entries regardless of whether they are enabled. Catalog tests validate every compiled entry.

Validation uses private `displayWidth`/`displayHeight` constants for both minimum and fixed geometry bounds. A package test parses the constants in `src/browserid/surface/surface.go` with `go/ast` and checks equality with the catalog constants, without importing the browser package. Missing or changed constants fail the drift check.

`metaviewer-nested` is enabled and uses `opensuperficies-program-electron-arm64:0.1.0` with no network and the default (empty) container runtime. It requests identity seccomp and singleton cleanup; the surface caller applies these options. Its minimum tile is 640 by 450 pixels, its fixed window is 1280 by 720 pixels at the display origin, and its limit is 1 window [TARGET]. Exchange uses `/profile/exchange` and accessibility is enabled. `RunArgs` preserves the container hardening and emits the supplied fixed tile in `SRF_TILES`; IP and seccomp insertion remain the caller's responsibility. The packaged application and image build are supplied by the Electron image lane.

Capsule names match `^srf-pg-[a-z0-9][a-z0-9-]{0,39}$` [TARGET].

## Launch argv

`RunArgs` returns argv beginning `docker run -d --name <container>`. The caller may insert IP and seccomp options immediately after the container name. Arguments follow the browser hardening order:

- `--user 10001:10001 --read-only --cap-drop ALL` [TARGET].
- `--network none`, or `--network srf-idnet` for proxy entries.
- CPU set `10-14`, memory and swap `4g`, PID limit `256`, shared memory `2g` [TARGET].
- Browser-identical tmpfs mounts for `/run/vqa/fb`, `/tmp`, `/run/user/10001`, `/run/newton-tenant`, `/home/worker`.
- Bind mounts from the supplied profile and view directories to `/profile` and `/srfview`.
- For proxy entries only, `SRF_EGRESS_PROXY` with the supplied nonempty proxy.
- `SRF_PROGRAM`, `SRF_WINDOWS`, then compact `SRF_TILES` JSON with lowercase keys `id,x,y,w,h` in that order.
- `NO_AT_BRIDGE=0` when accessibility is enabled [TARGET], followed by the image as the final argument.

No proxy environment is emitted for no-network entries, even if a proxy argument is supplied. Invalid catalog fields, unsafe bind paths or a missing required proxy return nil. Both profile and view paths must be absolute, valid UTF-8, equal to their filepath.Clean form, contain no `..` element, and contain none of comma, newline, carriage return or NUL. All other characters, including non-ASCII letters, spaces, braces and equals signs, are allowed; each mount value is passed as one argv element. This rule applies to both runtimes. The helper performs no process execution or filesystem I/O; VM launches read `SRF_KVM_GID` from the process environment. Golden tests compare the entire argv for both networks.

## VM runtime

The frozen W3 fields select `RuntimeVM`; an empty runtime or `RuntimeContainer` preserves the existing container behavior and argv. `notepad-vm` is enabled and uses `opensuperficies-vm-arm64:0.1.0`, no network, KVM, accessibility, `/profile/exchange`, and the same geometry and window limit as `notepad`.

| VM field | Validation / catalog value |
| --- | --- |
| MemoryMiB | At most 8192 MiB; catalog 8192 MiB [TARGET] |
| GuestCPUs | Between 1 and 5; catalog 4 [TARGET] |
| GuestMemMiB | At least 512 MiB and at most MemoryMiB minus 1024 MiB; catalog 6144 MiB [TARGET] |
| ProfileDiskMiB | Between 64 and 4096 MiB; catalog 1024 MiB [TARGET] |
| KVM / Network | Requires true / `NetNone` |
| Image | `opensuperficies-vm-arm64:<major>.<minor>.<patch>` |

Container entries require all W3 numeric fields zero and KVM false. Unknown runtimes and images from the other runtime are refused. `ValidCapsule` continues to accept only program names. VM `RunArgs` separately requires `^srf-vm-[a-z0-9][a-z0-9-]{0,39}$` [TARGET] and a canonical unsigned decimal `SRF_KVM_GID` in the range 1 through 4294967294 [TARGET], parsed with 32-bit `strconv.ParseUint`; zero, leading zeros, signs and overflow are refused; otherwise it returns nil. The caller resolves the host kvm group before invoking it.

VM argv retains user, read-only, cap-drop, network, CPU-set, PID limit and profile/view mounts, and adds `--security-opt no-new-privileges`, exactly `--device /dev/kvm`, and `--group-add` with the supplied gid. Memory/swap use the validated MemoryMiB value with an `m` suffix. Shared memory is 64m; tmpfs mounts are `/tmp` 64m, `/run/user/10001` 16m, `/run/newton-tenant` 16m and `/home/worker` 16m [TARGET]. There is no container `/run/vqa/fb` mount: guest frames remain in RAM.

After `SRF_PROGRAM`, `SRF_WINDOWS`, and `SRF_TILES`, VM launches emit `SRF_GUEST_CPUS`, `SRF_GUEST_MEM_MIB`, and `SRF_PROFILE_DISK_MIB`, then the accessibility environment and final image. `SRF_PROGRAM=notepad-vm` identifies the catalog entry; guest program settings must map that entry to the notepad application. No guest host-directory mount is added.

## Readiness

`ParseReady` requires the literal prefix `PROGRAM_READY ` and an object with exactly one `windows` member. Each window must contain exactly `id,x,y,w,h,xid`. Missing, extra, duplicate or null members are refused, as are trailing JSON values. Numeric geometry must decode as Go integers.

Window count, ordering, IDs and geometry must exactly match the supplied planned tiles. XIDs match `^0x[0-9a-f]{1,8}$` [TARGET] and must identify distinct numeric resources, including when leading zeroes are used. Lines longer than 64 KiB are refused [TARGET]. Live geometry verification remains the surface caller's responsibility.

## Errors

| Code | Meaning |
| --- | --- |
| 2 [TARGET] | Unknown or disabled program; detail begins `AUSENTE —` |
| 3 [TARGET] | Invalid catalog entry |
| 6 [TARGET] | Invalid readiness line or tile mismatch |

Errors are `*programsurface.Error` with `Code` and `Detail`. `Error()` returns the detail unchanged. No error uses exit code 1 [TARGET].

This specification scopes the mandate to the supplied P1/W3 designs and the programsurface package contract.
