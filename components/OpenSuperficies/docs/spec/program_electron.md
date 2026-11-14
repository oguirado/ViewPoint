# Electron program capsule

The generic image is `opensuperficies-program-electron-arm64:0.1.0`, based on
`opensuperficies-desktop-arm64:1.1.0` at
`sha256:a15b2c19d65f01f3f2f482fa62a06e1fee41bbd6e4af741892754a34d3b68382` [CITED: campaign design].
The base package assertions, environment, container user and working directory
match the GTK program image. No packages are downloaded. The shared program
entrypoint and identity xpra layer retain their established installation paths.
The recipe pins every build input and the app manifest separately.

## App input and pinning

Packaged MetaViewer input is AUSENTE — no inventar. `app.manifest` is empty and
build-command printing refuses it. The placeholder `app.json` is **TO CONFIRM
with metaviewer-0**: executable `metaviewer`, arguments `["--nested-depth=1"]`,
window class `metaviewer`. Electron version `44.9.2` and `linux-arm64` are supplied
campaign targets [TARGET]; pinning proves bytes, not the binary's vendor/version.

From the repository root, provide an unpacked app directory:

```sh
python3 -I -B scripts/program-electron/pin_app.py --app /operator/supplied/app
```

The command never executes the app. It refuses an existing `build/electron_app/`,
escaping/broken/cyclic symlinks, special files, ambiguous manifest filenames,
and privileged mode bits. Internal symlinks are materialized as regular copies;
directories and executable files become read-only executable, other files become
read-only. A setuid `chrome-sandbox` is reported and its setuid bit stripped;
no sandbox-disabling environment variable or launch flag is introduced.
The container uses the Chromium namespace sandbox through the identity seccomp
profile supplied by the caller. Live sandbox compatibility remains unverified.

Pinning writes sorted `sha256  relpath` lines to
`docker/program-electron/app.manifest` and refreshes the recipe's
`app_manifest_sha256`. Changing any recipe-pinned file, including `app.json`,
requires recomputing its entry in `recipe.json.files` before approval. App pinning
changes the recipe hash, invalidating any previous approval. Do not remove or
replace a staged app without reviewing the replacement input.

## Build review and operator approval

`build_electron_image.sh` takes no arguments and only prints a shell-quoted
`docker build --network=none` command. The printer validates the exact image,
base image ID, package pins, recipe-file hashes, nonempty manifest, staged app
file set, every app hash, modes and executable, then checks local base metadata.
It never builds or fetches. The Dockerfile copies only the staged app and
read-only metadata in addition to the shared entrypoint and xpra layer.

Before using the printer, the operator writes the root-owned regular
`/etc/opensuperficies/builder_approval.json` with the established
`os.cu.builder-approval/v1` schema, `mode=per_build`, this image,
`arch=aarch64`, and the current recipe SHA-256. The approval identifier and note
must satisfy the same constraints as the GTK builder. There is one shared
approval file: approving Electron replaces any identity or GTK build approval;
those images require fresh approval before being rebuilt. No approval was
requested, read or written during offline implementation.

## Boot and surface

`SRF_PROGRAM=metaviewer-nested` loads only the baked
`/usr/local/share/srf-app/app.json` before starting runtime processes. The shape
is exactly `exe`, `args`, `wm_class`, with duplicate JSON keys refused. The
executable must be a regular executable beneath `/opt/srf-app`, with no absolute
path or parent components. Arguments must be strings without NUL or line breaks;
the window class is restricted to the campaign's safe identifier grammar.
Malformed or unavailable metadata is `REFUSED` with exit code `2` [CITED: entrypoint].

The launch enforces `--user-data-dir=/profile/app` and `--ozone-platform=x11`,
adding each if absent and refusing conflicting/duplicate values and sandbox
bypass flags. Before any runtime spawn, argument names (the part before `=`)
containing `sandbox`, `zygote`, `single-process`, `in-process`, `remote-debugging`,
`inspect`, or `dev-shm` are refused, as are `--js-flags`, `--disable-web-security`
and `--allow-file-access-from-files`, including their `=value` forms.
An inherited `ELECTRON_DISABLE_SANDBOX` is refused. Each program process receives
only `DISPLAY`, `HOME`, `XDG_*`, `LANG`, `LC_*`, `PATH`, `NO_AT_BRIDGE`,
`GTK_USE_PORTAL`, `GIO_USE_VFS`, and the forced
`DBUS_SESSION_BUS_ADDRESS=disabled:`. All other inherited variables, including
`ELECTRON_*`, `NODE_*` (including `NODE_OPTIONS`) and `CHROME_*`, are dropped.
Runtime XDG state uses `/run/newton-tenant`; app persistence uses the private profile.
Electron receives its app arguments without notepad's `--tile` argument.
The capsule accepts a single window [TARGET: campaign catalog]; xdotool places
it using the existing readiness and taken-XID rule. The app must present exactly
one main window [TARGET]. Electron candidates must be visible class matches,
managed top-level clients (ICCCM `WM_STATE` present), with
`_NET_WM_WINDOW_TYPE_NORMAL` or an absent window-type property. Splash, dialog,
utility, tooltip, notification and other non-normal windows are ignored.
Selection waits within the existing readiness budget [CITED: entrypoint]; multiple
eligible new windows cause `REFUSED: ambiguous_program_window`.
`PROGRAM_READY` retains the existing JSON shape. `xpra-start` and `xpra-stop` retain their existing dispatch.
Notepad keeps its original argv and tile behavior.

Frames remain RAM-only in the existing tmpfs paths; no frame sink is added.

## Verification

```sh
env -u TMPDIR python3 -I -B scripts/program/test_contract.py
env -u TMPDIR python3 -I -B scripts/program-electron/test_contract.py
sh -n docker/program/entrypoint.sh
sh -n scripts/program-electron/build_electron_image.sh
```

The Electron suite uses fake app bytes under `build/room_IMG/`; it never executes
them, starts Docker/displays, reads approval data, or uses the network.
`test_image.py` is an operator-only post-build inspection with an overridden
entrypoint; it checks installed inputs, app hashes, permissions, architecture,
user, environment and package versions without launching Electron. A Python stand-in
uses the entrypoint's program-spawn boundary to verify the running child
environment, including removal of deliberately injected Electron/Node/Chrome
variables. It was not run in the implementation room. Live readiness, packaged-app compatibility and
restart persistence are AUSENTE — no inventar until operator validation.
Exit codes are success `0`, input refusal `2`, build-printer refusal `3`, and
contract failure `6` [CITED: scripts]; runtime retains the existing codes.
