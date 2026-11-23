# Quickstart

From this checkout, run bounded host verification:

```sh
PYTHONDONTWRITEBYTECODE=1 timeout 300 python3 -m unittest discover -s tests -p 'test_b0*.py'
python3 tools/gates.py --all
python3 tools/refresh_catalogue.py --check
python3 tools/route_code.py --root catalogue/router --q "report reconcile"
```

Install with `python3 -m pip install -e .` when an editable environment is desired.
`python3 -m blender_visual_qa.cli --help` lists stage dispatch.
After the DRIVE lane passes its own gates:

```sh
python3 -m blender_visual_qa.cli drive --blend build/scene.blend --journal examples/cube3.journal.json --profile cycles-cpu-det --out build/capture
```

Journal/schema names and fixtures are owned by DRIVE; consult its package before choosing them.
Managed DRIVE needs Linux user namespaces, `unshare`, `bwrap`, `libseccomp.so.2`, and an existing private RAM workspace root owned by the caller. See `bpy_side.canon.RAMWS_ROOT` for the default. Set `VQA_RAMWS_ROOT` to select another approved root.
Blender renders with read-only inputs and runtime files. The host validates the capture and publishes it to `--out` after Blender exits.
Use scratch paths, CPU Cycles and bounded timeouts. Gate failures are blocking; a partial build is never reported as a qualified run.

The same RAM admission gate covers owned Blender invocations in VQA3D, the truth benchmark, the example scene, the asset renderers and Blender-backed mesh tools. Each launch uses an explicit set of read-only inputs and runtime files. The host reads worker artifacts after validation and publishes the deliverables allowed by each command. A refused launch preserves existing published artifacts.

VQA3D seeds a declared Cycles kernel cache into private RAM. The host publishes a new kernel only after validating its filename, bytes and successful render evidence. A CUDA probe uses an individually identified local CUDA device, the associated driver control nodes and a RAM cache. Device enumeration alone cannot qualify a GPU render. The one-shot CUDA probe reports a refusal when Cycles cannot select and render on that device. Persistent CPU sessions have their own admission evidence.

The private display path starts Xvfb and Blender together in an isolated namespace at a private display number. The software renderer can qualify display attachment. A live bridge attaches to an externally owned GUI and reports its containment admission as absent.
