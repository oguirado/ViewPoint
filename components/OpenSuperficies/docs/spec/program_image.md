# Program image

The program image is `opensuperficies-program-gtk-arm64:0.1.0` [TARGET], based on
`opensuperficies-desktop-arm64:1.1.0` [CITED: approved design]. The recipe pins the
local base image ID, installed Debian package versions and every build/check source.
Registry base digest: AUSENTE — no inventar. The Dockerfile checks installed packages;
it performs no downloads. Image builds require the operator's approval.

`identity-entrypoint.sh` supports empty boot, `xpra-start` and `xpra-stop`. The xpra
commands exec the shared identity layer, which reads `SRF_LOGIN`. Other commands and
unknown `SRF_PROGRAM` values are refused before starting children. Boot validates
`SRF_WINDOWS` and `SRF_TILES`, private profile ownership, writable view mount and RAM
runtime mounts. It starts the display, openbox and one notepad per tile, waits for
unique mapped windows, and verifies their placement. It prints `xpra layer up`, then
`PROGRAM_READY {"windows":[{"id":"w1","x":0,"y":0,"w":400,"h":300,"xid":"0xabc"}]}`
(example geometry [TARGET]). Readiness uses compact JSON and lowercase hexadecimal
XIDs. The taken-XID set includes previously assigned windows even during remapping.
Child exit stops the capsule; signal shutdown terminates and reaps child groups.
Frames remain in RAM; no frame disk sink is provided.

Notepad uses GTK, a text editor, a file-name entry and accessible Open/Save/New
buttons. Ctrl+O opens and Ctrl+S saves. The status displays `saved <name>` after
successful saving. Documents live only in `/profile/exchange`, mode `0700` [TARGET].
Names are single components without `..`; absolute paths, symlinks, non-regular files,
hard links and files exceeding `1 MiB` [TARGET] are refused. Descriptor-relative opens
use no-follow and nonblocking flags, preventing symlink traversal and FIFO hangs.
No typed document contents are logged. Missing GTK returns unavailable code `4` [TARGET];
configuration refusals use `2` [TARGET], builder refusals `3` [TARGET], contract failures
`6` [TARGET].

## Operator build procedure

Run the offline contract check first. Review the recipe SHA and provide a root-owned,
root-group, regular `0644` [TARGET] `/etc/opensuperficies/builder_approval.json`, schema
`os.cu.builder-approval/v1`, mode `per_build`, arch `aarch64`, image equal to the program
image, and matching `recipe_sha256`; include `approval_id` and `operator_note` as for
the identity builder. This replaces the identity image approval; rebuilding the identity
image requires a fresh approval naming that image.

`scripts/program/build_program_image.sh` accepts no arguments. It only prints the
approved `docker build --pull=false --platform linux/arm64 --network=none -f
<repo>/docker/program/Dockerfile -t opensuperficies-program-gtk-arm64:0.1.0 <repo>` command.
The Dockerfile and context paths are absolute. Execute the printed command only after operator approval.
The printer rechecks approval binding, all recipe hashes and the local base image ID.
Production always reads `/etc/opensuperficies/builder_approval.json`; environment
variables cannot override that path. Offline tests pass a scratch approval path to
the internal printer and exercise the same ownership, mode, schema and recipe checks.

`python3 -I scripts/program/test_contract.py` runs offline with no Docker or display.
`python3 -I scripts/program/test_image.py` is an operator post-build check and starts
only package-query containers; it never starts the graphical entrypoint.
