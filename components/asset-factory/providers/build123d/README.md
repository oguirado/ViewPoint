# CAD export and bounded native qualification

`cad_code` precise-part scripts define `part` and receive explicit `CAD_PARAMS`.
The worker exports STEP, binary STL and canonical integer B-Rep mass JSON,
then imports STEP and checks the physical properties again. An independent
nominal policy can additionally reject dimensions, volume, centre, inertia
or STL topology before publication. Scripts are reviewed trusted code; this
process boundary is not a security sandbox. Outputs publish through a staged
directory with `RENAME_NOREPLACE`; a refusal preserves caller bytes.

`tools.cad.native_qualify` qualifies analytic box and rectangular through-hole
fixtures, including an off-origin fixture. It measures repeat-export byte
identity, STEP physical-property round-trip, STL manifold topology and native
Blender import dimensions. Unit swap, missing hole and open-solid controls
must fail before their corrected versions pass. The original
`tools.cad.qualify` remains the pre-install diagnostic harness.

Run from the component root with a fresh output directory:

```sh
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
  taskset -c 0-15 nice -n 15 env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. timeout 300 \
  "${AF_CAD_VENV:?set the qualified CAD virtual environment}/bin/python" -B -m tools.cad.native_qualify \
  --freeze "${AF_CAD_FREEZE:?set the qualified CAD package freeze}" --out /absolute/fresh/output
```

The approved interpreter and freeze digest are pinned in `runtime.json`, with
installed API source digests. Runtime qualification checks distribution
versions, freeze identity, source identity and byte replay separately. It does
not install, download or rebuild dependencies. Wheel hashes and a reproducible
offline installation recipe are **AUSENTE**.

Mass JSON (`af.cad-brep-mass/v1`) uses volume in µm³, mass in ng, centre in µm
and centre-frame inertia in world axes in kg·m² multiplied by `10**18`.
Density is explicit kg/m³; CAD input is mm, Blender input converts to m.
OCCT computes floating-point B-Rep volume integrals. These are not mathematically
exact numbers. STL measurements remain separately labelled approximations.

Fixture policies use `10` µm for dimensions and `1` ppm for volume/inertia
**[TARGET]**. A zero reported error is at the serialized integer precision.
The measured planar fixture scope does not establish a general chord-error
bound for curved parts or arbitrary local features. New record contracts are
in `tools/cad/{native,runtime,qualification}.schema.json`.

The manifest stays **DRAFT**, `runnable:false`, with production readiness
**CANNOT_SEE**. Its peak RAM measurement is limited to the analytic fixtures.
Dependency/territory rights and production scheduler admission/release remain
**AUSENTE**. `commercial_use` is required per install, without a default.
The adapter preserves this refusal even if `runnable` is forged. Native fixture
PASS does not authorize arbitrary production CAD. The draft slot snapshot is
used until W3-CONTRACTS registers `cad_code`.

Applied pattern guidance: `VG-TOOL-0027` (bounded scripting), `SW-0067`
(observable mutation controls) and `VE-0022` (separate identity, replay and
runtime witnesses). No numerical limit is adopted from the unrelated math hits.
Measurements and commands are in the external `REPORT_W4-CAD.md`.
