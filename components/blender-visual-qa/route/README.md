# Route index v2

Questions to code coordinates for this component. Rows are coordinates, not proof: read the
cited span before relying on it. Standard library only; queries read `route/index/` and write
nothing.

```sh
python3 -B route/query.py any "which function detects magenta pixels in a frame" --k 5
python3 -B route/query.py any "uv stretch" --type sym --env generic
python3 -B route/query.py find frame_magenta
python3 -B route/query.py where "atomic report write"
python3 -B route/query.py tests pack_orm
python3 -B route/query.py error E_STALE
python3 -B route/query.py env blender --prefix blender_visual_qa/assets/
python3 -B route/query.py bench
python3 -B route/query.py check
```

| rc | Meaning |
|---|---|
| 0 | answered (or `ROUTE_OK`) |
| 2 | usage |
| 3 | abstained: the question names terms this corpus does not contain, so answer AUSENTE |
| 4 | stale index or a frozen benchmark split changed |
| 5 | benchmark below a measured floor, or a negative question answered |

Row types: `sym` (Python def or class, with `env`), `doc` (Markdown section, hyperbook
aliases `hb:<book>#<id>`), `schema` (JSON schema), `test` (test file). Symbol ids keep the
older router's names (`bv.`, `bpy.`, `tools.`), and `# @anchor:` names are aliases.

After changing tracked files, rebuild and commit the index:

```sh
python3 -B route/build.py
python3 -B route/build.py --check
```

The benchmark lives in `bench/`. `FROZEN.json` pins the sha256 of the TEST and HOLD rows and
the measured recall@5 floors. Floors may only rise; a new held-out split is needed before
any tuning claim. The older router (`tools/route_code.py`, `catalogue/router`) stays as it was.
