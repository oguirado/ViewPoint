# Sculpt capability probe

From the asset-factory component root, run the host-only module inside the
assigned memory/CPU scope. Supply an existing external output directory:

```sh
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
  taskset -c 0-15 nice -n 15 env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
  timeout 360 python3 -B -m providers.blender_sculpt.probe.run \
  --blender /usr/bin/blender --bvqa-root ../blender-visual-qa \
  --output-dir "$PROBE_OUTPUT" --receipt "$PROBE_OUTPUT/receipt.json"
```

`bpy_probe.py` runs only inside Blender. The host runner isolates background
experiments and owns a separate Xvfb GUI using the actual vqa3d live adapter.
The GUI uses software OpenGL. No render or capture method is called, no frames
are written, and no existing GUI session or caller asset is opened.

The receipt separates RNA availability, headless execution and live execution.
RNA float defaults/bounds use tagged hexadecimal strings; mesh digests use exact
little-endian binary coordinates and connectivity. Qualification requires an
unchanged zero-strength control followed by an effective stroke on that same
mesh. A successful operator return by itself is insufficient.

The pinned runtime is Blender 4.0.2 [TARGET]. A fresh measurement directory keeps
diagnostics even on failure. `--receipt` is atomically published only on rc zero;
refusals preserve caller bytes. Process exit signals appear only as diagnostic
`process_rc`; stage codes use the component's allowed vocabulary.

Tests: `python3 -B -m unittest discover -s tests -p 'test_ws_s1_u1_*.py'` inside
the same resource scope. Set `TMPDIR` to an external scratch directory. Runtime
tests require Blender, Xvfb and the sibling vqa3d adapter; unavailable evidence
fails the gate. This probe qualifies only synthetic-mesh capability, without
claiming appearance, arbitrary-asset support, undo or production readiness.
