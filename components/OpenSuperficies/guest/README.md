# guest/ — microVM guest artefacts (not shipped in source)

The guest kernel image (`Image`, Linux 6.18.52, GPL-2.0-only) and the static
init binary (`srf-guest-init.bin`) are build outputs and are **not** part of
this source release.

Build them locally with `docker/ng/guest/construir.sh`. The script downloads
the pinned kernel tarball (its sha256 is recorded in `PROCEDENCIA.json`),
builds it with `kernel.config`, and compiles `./cmd/srf-guest-init`.
`MANIFIESTO.sha256` lists the expected hashes of the reference build.

If a prebuilt kernel is ever distributed (for example as a release asset), it
must be accompanied by the exact kernel source tarball and `kernel.config`
(GPL-2.0 §3(a)).
