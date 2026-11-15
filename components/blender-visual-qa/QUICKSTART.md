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
Use scratch paths, CPU Cycles and bounded timeouts. Gate failures are blocking; a partial build is never reported as a qualified run.
