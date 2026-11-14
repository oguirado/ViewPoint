# Identity browser image recipe

The recipe derives `opensuperficies-identity-browser-arm64:0.1.0` [TARGET] from
`opensuperficies-desktop-arm64:1.1.0` [CITED: conductor host measurement].
The local base image ID is
`sha256:a15b2c19d65f01f3f2f482fa62a06e1fee41bbd6e4af741892754a34d3b68382`
[CITED: conductor host measurement]. This is a local image ID, not a registry
RepoDigest. Registry digest: AUSENTE — no inventar; `base_digest` is null for
that reason. The base is Debian bookworm, linux/arm64 [CITED: conductor].
Installed Chromium is exactly `152.0.7977.82-1~deb12u1` [CITED: conductor dpkg
measurement]. The Dockerfile verifies that installed version and the sandbox
helper, without package installation, repository access or upgrades.

## Approval and offline build command

`sh scripts/identity/build_identity_image.sh` validates the operator approval,
recipe file hashes and local base image ID, then prints the shell-quoted build
command. It never builds. The only daemon request is a bounded read-only
`docker image inspect --format '{{.Id}}' opensuperficies-desktop-arm64:1.1.0`.
Missing or mismatching approval returns rc 3 [TARGET] before any Docker call.
A missing base or different local ID also returns rc 3 [TARGET], without printing
a build command. There is no approval path override.

Approval is `/etc/opensuperficies/builder_approval.json`, root:root regular mode
0644 [TARGET], opened without following symlinks. Its exact envelope is:

```json
{
  "schema": "os.cu.builder-approval/v1",
  "mode": "per_build",
  "image": "opensuperficies-identity-browser-arm64:0.1.0",
  "arch": "aarch64",
  "recipe_sha256": "SHA256_OF_FINAL_RECIPE_JSON",
  "approval_id": "b2-0000",
  "operator_note": "Identity browser recipe reviewed"
}
```

The approval ID is illustrative [TARGET]. Hash the finalized recipe bytes with
`sha256sum docker/identity/recipe.json`. The manifest seals every IMAGE source,
test and document; it excludes itself to avoid a recursive hash. The approval
binds its entire manifest. Identical rebuilds reuse that approval; any sealed
content change requires refreshing hashes and operator approval. Capsule starts
need no build approval. Existing worker-base approval does not approve this image.

The printed build uses the local base tag, `--pull=false` and `--network=none`
[TARGET]. Run it only after the base ID check. No registry digest is fabricated.
Changing the local tag between inspection and execution is outside this print-only
script's guarantee; re-run the validator immediately before executing the build.
Identity capsules use host Docker under operator ruling fifteen [CITED], separate
from the worker seat's network-none policy.

## Runtime contract

The launcher acquires the identity lease and mounts `/profile` with uid 10001 and
mode 0700 [TARGET]. The entrypoint checks metadata only; profile contents are
never read or logged by the entrypoint. Chromium retains its sandbox and exposes
no remote-debugging transport. The inherited generic Chromium launcher is unused.
Runtime package/daemon removal is unnecessary: this entrypoint starts only its
explicit display, window manager, browser and xpra processes.

Required tmpfs paths are `/run/vqa/fb`, `/run/newton-tenant` and `/tmp` [TARGET].
Xvfb runs `:100`, `1280x900x24`, `-fbdir /run/vqa/fb`, `-nocursor` [TARGET].
The base must provide executable `/usr/local/libexec/srf-fbread`; absence refuses
startup. Consumers capture exact framebuffer pixels through fbread into RAM.
No frame files, recording saves or xpra captures are produced by these scripts.

| Environment | Contract [TARGET] |
|---|---|
| SRF_EGRESS_PROXY | Required HTTP proxy URL with explicit port; no credentials, path, query or fragment |
| SRF_WINDOWS | Required count within 1..16 |
| SRF_TILES | Flat JSON array of unique `{id,x,y,w,h}`, non-overlapping integer rectangles within 1280x900 |
| SRF_LOGIN | Only exact `1` enables xpra input; default is readonly |

The CLI flattens tiling.Plan output. Geometry is applied and observed before
readiness; incompatible rectangles fail without shrinking the requested layout.
Browser output is suppressed; readiness contains geometry and XIDs only.
Chromium uses the proxy with loopback bypass and QUIC disabled. The capsule's
internal Docker network and egress proxy enforce permitted outbound traffic.

## Xpra operator view

`identity-xpra-layer.sh` adapts the measured base layer's xpra argv and starts
`xpra start-desktop :100 --use-display` [CITED: base layer; TARGET: adaptation].
It binds only `/srfview/xpra.sock`, with `--socket-permissions=666` [TARGET].
The launcher bind-mounts the per-capsule host view directory's inner `s/` directory
at `/srfview`. Its operator-owned parent stays mode 0700; the inner directory is
mode 0777 [TARGET], allowing the container UID to create the socket without
exposing it to other host users. Both entrypoint and xpra restart refuse a missing,
non-mounted, symlinked or unwritable `/srfview` with `view_mount_not_writable`
and rc 4 [TARGET]. Mount detection reads mountinfo so same-filesystem bind mounts
are accepted. The additional xpra session sockets remain under runtime tmpfs.
`XPRA_READONLY=yes` is the default; only `SRF_LOGIN=1` selects `no`.
Inherited XPRA_READONLY/XPRA_SOCKET values cannot widen these settings.
No encoding, quality, speed, compression or refresh policy is forced: xpra chooses
its default codecs, including the available video encoders [CITED: conductor].
`--start=/bin/true` overrides start-desktop's Xsession default without a dialog.
Clipboard, file transfer, printing, sound, microphone, URL opening, D-Bus control
and new commands remain disabled. Logs and PID metadata stay in runtime tmpfs.
No Docker port is published by the image.

Startup requires the socket to exist and an HTTP GET over AF_UNIX to return
status 200 [TARGET]; the isolated Python probe uses a bounded socket timeout and
bounded status line, with no dependency on curl Unix-socket support. It prints
`xpra layer up`, then emits:

```text
IDENTITY_READY {"windows":[{"id":"window-1","x":0,"y":0,"w":1280,"h":900,"xid":"0x..."}]}
```

Values in the example are illustrative [TARGET]. For the CLI's operator bridge:

```text
docker exec -e SRF_LOGIN=1 CONTAINER /usr/local/bin/identity-entrypoint.sh xpra-start
docker exec -e SRF_LOGIN=0 CONTAINER /usr/local/bin/identity-entrypoint.sh xpra-start
```

`xpra-start` always stops the existing layer before restarting it; `xpra-stop`
stops it. The CLI serves the HTML5 viewer on host loopback, proxies HTTP and
websocket traffic directly to the Unix socket, and supplies the host jquery asset.
Login restarts enable input and restore readonly on exit. No per-connection Docker
exec or container TCP listener is needed.
Wrapper `start|stop|restart` is also available directly. Startup failure and
signals trigger bounded display/browser and xpra cleanup. Configuration refusals
use rc 2; runtime failures use rc 4; wrapper readiness failures use rc 3 [TARGET].

## Verification

`python3 -B scripts/identity/test_image.py --selftest` checks the recipe and kills
scratch mutants for package installation, absent pin checking, TCP binding,
forced codecs, missing socket permissions, Xsession startup, mount preflight bypass,
writable default, proxy removal, framebuffer removal and unsafe browser flags.
`python3 -B scripts/identity/test_contract.py` checks configuration, metadata-free
refusals, recipe tampering, approval binding, fake-runner base inspection,
mount preconditions and the real readiness probe against a fake Unix HTTP server.
Fragmented successful status lines pass; malformed, missing, truncated and error
responses fail.
No Docker, X, browser or TCP networking is run by these tests. `python3 -B scripts/identity/test_ax_review.py` checks review regressions for
URL privacy, sensitive autocomplete, main-frame projection and pipe recovery.
Full CU gates must run
on a scratch copy because their build output exceeds this lane's write surface.
Built-image/runtime acceptance remains AUSENTE — no inventar in this offline lane.

This image-layer mandate is limited to the supplied repository and campaign corpus.
