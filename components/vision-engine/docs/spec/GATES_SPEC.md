# Vision-Engine Gate Runner and Gate Checks Specification (GATES_SPEC)

This document specifies the implementation of the vision-engine static and dynamic verification gate runner (tools/gates.py:1-797), its heavier gate bodies (tools/gate_checks.py:1-245), and its unit test suite (tests/test_gates.py:1-47) at branch bridge/integration. It serves as the reference specification for a native port and for HyperBooks.

## 1. Architecture and Core Invariants

1. **Pure, Fail-Closed, Stdlib-Only Runner**:
   - The runner module header declares `vision-engine gate runner (stdlib only, fail-closed, pure: no clock, no randomness)` (`tools/gates.py:2-7`).
   - The companion module header declares `Heavier gate bodies split out of gates.py (size gate): catalogue staleness, golden replay, route budget. Stdlib only.` (`tools/gate_checks.py:2-5`).
   - Each gate function takes `root` (tools/gates.py:650-659) and returns [(code, detail)] finding tuples (tools/gate_checks.py:4), where an empty list indicates that the gate passed.
2. **Bitmask Evaluation Without Short-Circuiting**:
   - `run_gates(root, names=None)` iterates through the ordered `GATES` list (`tools/gates.py:650-659`).
   - Every selected gate is executed unconditionally without short-circuiting (`tools/gates.py:6`).
   - If any gate raises an `Exception`, `run_gates` catches it and records a single fail-closed finding `("E_GATE", f"gate crashed: {type(e).__name__}: {e}")` (`tools/gates.py:650-659`).
3. **Canonical JSON Output**:
   - All CLI output is serialized as a single-line canonical JSON object terminated by a newline via canon(o) (tools/gates.py:46-47) and written to sys.stdout via emit(obj) (tools/gates.py:721-723):
```python
def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
```
4. **Shared AST and Filesystem Helpers in `tools/gates.py`**:
   - `pyfiles(root, pkg)` returns sorted `*.py` paths under `root / pkg` excluding any path with `"__pycache__"` in `p.parts`, or `[]` if `root / pkg` is not a directory (`tools/gates.py:50-54`).
   - `rel(root, p)` formats `p.relative_to(root).as_posix()` (`tools/gates.py:57-58`).
   - `parse(p)` parses `p.read_text(encoding="utf-8")` via `ast.parse` with `filename=str(p)` (`tools/gates.py:61-62`).
   - `stem_of(root, p)` extracts the top-level module/subpackage stem under `visioncore` via `p.relative_to(root / "visioncore").with_suffix("").parts[0]` (`tools/gates.py:65-66`).
   - `imports_of(tree, level0_pkg="visioncore")` walks an AST and yields `(top, sub|None, lineno)` tuples for every `ast.Import` and `ast.ImportFrom` node, resolving relative imports (`n.level > 0`) against `level0_pkg` (`tools/gates.py:69-92`).
   - `is_stdlib(top)` checks `top in sys.stdlib_module_names` (`tools/gates.py:94-95`).
   - `load_py(path, name)` loads a Python module from an explicit filesystem path using `importlib.util.spec_from_file_location` and `spec.loader.exec_module` (tools/gates.py:668-672, and identically in tools/gate_checks.py:13-17).
   - `_checks()` loads `gate_checks.py` adjacent to `tools/gates.py` under module name `"_gate_checks"` (`tools/gates.py:613-614`).

## 2. Command-Line Interface, Modes, and Exit Codes

### 2.1 Entry Point and Argument Parsing

The CLI entry point is main(argv) (tools/gates.py:726-762), invoked at module exit via sys.exit(main(sys.argv[1:])) (tools/gates.py:796-797).

```python
# @anchor:tools.gates.main
def main(argv):
    root = Path(__file__).resolve().parent.parent
    mode, gate_id, detail = None, None, False
```

`main(argv)` initializes `root` to two levels above `tools/gates.py` (`Path(__file__).resolve().parent.parent`) and consumes `a = list(argv)` left-to-right in a `while a:` loop (`tools/gates.py:727-744`):

| Flag / Option | Arguments | Parser Effect | Citation |
| :--- | :--- | :--- | :--- |
| `--root` | DIR (required next token) | Sets `root = Path(a.pop(0)).resolve()` | `tools/gates.py:733-734` |
| `--all` | None | Sets `mode = "all"` | `tools/gates.py:735-736` |
| `--mutants` | None | Sets `mode = "mutants"` | `tools/gates.py:737-738` |
| `--gate` | id (required next token) | Sets `mode, gate_id = "gate", a.pop(0)` | `tools/gates.py:739-740` |
| `--json` | None | Sets `detail = True` | `tools/gates.py:741-742` |

- **Multiple Mode Flags**: Because `while a:` assigns `mode` sequentially (`tools/gates.py:731-740`), passing more than one of `--all`, `--mutants`, or `--gate` overwrites `mode` with the last flag seen. Explicit mutual-exclusion validation between mode flags is ABSENT.
- **Unrecognized or Incomplete Arguments**: Any token not matching the table above (including `--root` or `--gate` at the end of `argv` when `a` is empty) immediately emits `E_USAGE` with `rc` `2` (`tools/gates.py:727-744`).
- **Missing Mode**: If `mode is None` after parsing all tokens, `main` emits `E_USAGE` with `rc` `2` (`tools/gates.py:727-746`).
- **Unknown Gate Identifier**: In `mode == "gate"`, if `gate_id not in dict(GATES)`, `main` emits `E_USAGE` with `rc` `2` (`tools/gates.py:727-755`).

### 2.2 Modes, Exit Codes, and Output Schemas

`tools/gates.py` uses three integer exit codes (`rc` in `0`, `2`, `5`) across all modes (tools/gates.py:5, tools/gates.py:727-762):

| Mode | Condition | Exit Code (`rc`) | `"code"` Field | Output JSON Keys | Citation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Any | Unknown flag or missing flag argument | `2` | `"E_USAGE"` | `code`, `detail`, `rc` | `tools/gates.py:743-744` |
| Any | No mode flag supplied (`mode is None`) | `2` | `"E_USAGE"` | `code`, `detail`, `rc` | `tools/gates.py:745-746` |
| `--gate` | `gate_id not in dict(GATES)` | `2` | `"E_USAGE"` | `code`, `detail`, `rc` | `tools/gates.py:739-755` |
| `--all` or `--gate` | All evaluated gates returned empty findings, without `--json` | `0` | `"OK"` | `code`, `failed`, `rc`, `receipt` | `tools/gates.py:735-762` |
| `--all` or `--gate` | All evaluated gates returned empty findings, with `--json` (`detail = True`) | `0` | `"OK"` | `code`, `failed`, `findings`, `rc`, `receipt` | `tools/gates.py:735-762` |
| `--all` or `--gate` | At least one gate returned non-empty findings (`failed` non-empty) | `5` | `"E_GATE"` | `code`, `failed`, `findings`, `rc`, `receipt` | `tools/gates.py:735-762` |
| `--mutants` | `survived` is empty, without `--json` (`detail` is `False`) | `0` | `"OK"` | `code`, `killed`, `rc`, `results` (`[]`) | `tools/gates.py:729-751` |
| `--mutants` | `survived` is empty, with `--json` (`detail = True`) | `0` | `"OK"` | `code`, `killed`, `rc`, `results` (full list) | `tools/gates.py:729-751` |
| `--mutants` | `survived` is non-empty (mutant survived, clean base failed, or `no-mutants`) | `5` | `"E_MUTANT_SURVIVED"` | `code`, `rc`, `results`, `survived` | `tools/gates.py:681-750` |

Exact detail strings for `rc` `2` (`E_USAGE`, tested in `tests/test_gates.py:29-31`):
- Unrecognized argument or missing value for `--root` / `--gate`: `"gates.py --all|--gate <id>|--mutants [--root D] [--json]"` (`tools/gates.py:733-744`).
- No mode flag provided: `"mode required"` (`tools/gates.py:745-746`).
- Unknown gate name in `--gate`: `"unknown gate " + gate_id` (`tools/gates.py:739-755`).

In --all and --gate modes, when `failed` is non-empty or `detail` (--json) is True, `"findings"` maps each failing gate name `k` to `{k: [list(x) for x in v] for k, v in res.items() if v}` (tools/gates.py:735-762). When all gates pass and --json is passed, `"findings"` is empty (tools/gates.py:759-761).

## 3. Receipt Format

Receipts are computed by receipt(res) (tools/gates.py:662-665) using `NA_GATES` (tools/gates.py:641):

```python
NA_GATES = ("g_hint_neutral",)
```

```python
def receipt(res):
    failed = [k for k, v in res.items() if v]
    na = ",".join(g for g in NA_GATES if g in res and not res[g])
    return failed, ("VG:PASS" if not failed else "VG:FAIL:" + ",".join(failed)) + (" NA:" + na if na else "")
```

1. **Failed List (`failed`)**:
   - Preserves the insertion order of `res`, which matches the execution order in `GATES` (`tools/gates.py:650-663`).
   - A gate `k` is in `failed` if and only if its findings list `v` in `res.items()` is non-empty (`tools/gates.py:663`).
2. **NA List (`na`)**:
   - Iterates over `NA_GATES = ("g_hint_neutral",)` and includes `g` if `g in res and not res[g]` (`tools/gates.py:641-665`).
3. **Receipt String Grammar**:
   - **Pass without NA gates evaluated** (for example, single-gate mode on a non-NA gate): `"VG:PASS"` (`tools/gates.py:665`).
   - **Pass with `g_hint_neutral` evaluated** (for example, --all or --gate `g_hint_neutral`): "VG:PASS" followed by " NA:" + na, yielding NA:g_hint_neutral (tools/gates.py:641-665, verified in tests/test_gates_fix.py:69-72).
   - **Fail without NA gates evaluated**: `"VG:FAIL:" + ",".join(failed)` (`tools/gates.py:665`).
   - **Fail with g_hint_neutral evaluated and passing** (for example, --all on an empty root): starts with `"VG:FAIL:"` followed by comma-joined failed gate names and " NA:g_hint_neutral" (tools/gates.py:641-665, verified in tests/test_gates.py:33-38).

## 4. Full List of Gates in Run Order

The gate registry `GATES` is initialized with 18 gates (tools/gates.py:643-647) and extended with a 19th gate g_native_scope after main (tools/gates.py:793) so earlier anchor line numbers in the catalogue do not move:

```python
GATES = [("g_tier1", g_tier1), ("g_anchor", g_anchor), ("g_header", g_header), ("g_layer", g_layer),
         ("g_threshold", g_threshold), ("g_nofloat", g_nofloat), ("g_engine_free", g_engine_free),
         ("g_ids", g_ids), ("g_hint_neutral", g_hint_neutral), ("g_golden", g_golden), ("g_catalogue", g_catalogue), ("g_schema", g_schema),
         ("g_route_budget", g_route_budget), ("g_size", g_size), ("g_canon", g_canon), ("g_pure", g_pure),
         ("g_sealed_untouched", g_sealed_untouched), ("g_disk_frames", g_disk_frames)]
```

### Summary Table of Gates in Run Order

| # | Gate ID | Runner Definition | Check Body | Finding Codes Emitted | Primary Scope |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | `g_tier1` | `tools/gates.py:31-120` | Inline | `E_GATE_TIER1` | `llms.txt`, `AGENTS.md` |
| 2 | `g_anchor` | `tools/gates.py:29-175` | Inline | `E_GATE_ANCHOR`, `E_DRIFT` | `SRC_PKGS`, `symbols.jsonl`, `layers.json`, `routes.tsv` |
| 3 | `g_header` | `tools/gates.py:17-225` | Inline | `E_GATE_HEADER` | `visioncore` Python files |
| 4 | `g_layer` | tools/gates.py:17-280 | Inline + tools/gates.py:765-767 | `E_GATE_LAYER_PKG`, E_GATE_LAYER_UP | `visioncore`, vadapt, vmem, tools, `vnative` |
| 5 | `g_threshold` | `tools/gates.py:35-376` | Inline | `E_GATE_THRESHOLD` | `params.py`, `thresholds.tsv`, `layers.json`, `visioncore` |
| 6 | `g_nofloat` | `tools/gates.py:43-462` | Inline | `E_GATE_NOFLOAT` | `visioncore` layers 0–3 |
| 7 | `g_engine_free` | `tools/gates.py:37-489` | Inline | `E_GATE_ENGINE` | `visioncore` Python files |
| 8 | `g_ids` | `tools/gates.py:38-522` | Inline | `E_GATE_IDS` | `visioncore` and `vmem` (except `ids.py`) |
| 9 | g_hint_neutral | tools/gates.py:629-641 | tools/gate_checks.py:132-135 | None (reported in NA_GATES) | None |
| 10 | g_golden | tools/gates.py:621-622 | tools/gate_checks.py:56-99 | E_GATE_GOLDEN | tests/golden (synth.json, frames.sha256, classes.json) |
| 11 | g_catalogue | tools/gates.py:617-618 | tools/gate_checks.py:28-41 | E_GATE_CATALOGUE, E_STALE | `catalogue` (layers.src and generated outputs) |
| 12 | `g_schema` | `tools/gates.py:34-569` | Inline | `E_GATE_SCHEMA` | `*.schema.json` in `visioncore/schemas`, `visioncore/ids.py` |
| 13 | g_route_budget | tools/gates.py:625-626 | tools/gate_checks.py:101-129 | E_GATE_ROUTE | route_queries.tsv, routes.tsv |
| 14 | `g_size` | `tools/gates.py:29-532` | Inline | `E_GATE_SIZE` | `SRC_PKGS` (`visioncore`, `vmem`, `vadapt`, `tools`, `vnative`) |
| 15 | `g_canon` | `tools/gates.py:46-588` | Inline | `E_GATE_CANON` | `*.json`, `expect/*.json`, `*.jsonl` |
| 16 | `g_pure` | `tools/gates.py:36-610` | Inline | `E_GATE_PURE` | `tools` and `vnative` Python files |
| 17 | g_sealed_untouched | tools/gates.py:633-634 | tools/gate_checks.py:142-200 | E_GATE_SEALED | visioncore, vmem, EQUIV_RECEIPT.json |
| 18 | g_disk_frames | tools/gates.py:637-638 | tools/gate_checks.py:203-245 | E_GATE_DISK | x1_determinism.py, ramws.py, build, /proc |
| 19 | `g_native_scope` | `tools/gates.py:770-793` | Inline | `E_GATE_NATIVE_SCOPE` | Full repository tree (excluding `NATIVE_SKIP`) |

### 4.1 Gate 1: `g_tier1` (`tools/gates.py:99-120`)

- **Rule**:
  - Iterates over `TIER1 = (("llms.txt", 600), ("AGENTS.md", 6000))` (`tools/gates.py:31-102`).
  - Verifies each file exists (`p.is_file()`), that its byte size satisfies len(data) <= cap (`600` B for `llms.txt`, `6000` B for `AGENTS.md`), and that every extracted local link path `c` exists under `root` (`(root / c).exists()`) (`tools/gates.py:31-118`).
  - Candidate paths `cands` are extracted from UTF-8 decoded text (`data.decode("utf-8", "replace")`) via two regexes (`tools/gates.py:41-114`):
    1. Markdown links matching `r"\]\(([^)#\s]+)\)"` (`tools/gates.py:110`).
    2. Backticked tokens matching `([A-Za-z0-9_./-]+)` that start with `PATH_TOPS = ("tools/", "catalogue/", "visioncore/", "tests/", "vmem/", "vadapt/", "hyperbooks/")` and end with `PATH_EXTS = (".md", ".py", ".json", ".jsonl", ".tsv", ".txt", ".toml")` (`tools/gates.py:41-113`).
- **Files Scanned**: `llms.txt` and `AGENTS.md` under `root` (`tools/gates.py:31-102`).
- **Finding Codes Emitted**:
  - `("E_GATE_TIER1", f"{name} missing")` (`tools/gates.py:104`).
  - `("E_GATE_TIER1", f"{name} {len(data)} B > {cap}")` (`tools/gates.py:108`).
  - `("E_GATE_TIER1", f"{name} links missing path {c}")` (`tools/gates.py:118`).
- **Exemptions**:
  - URLs starting with `("http:", "https:", "mailto:")` are skipped (`tools/gates.py:115-116`).
  - Backticked tokens not matching both `PATH_TOPS` and `PATH_EXTS` are ignored (`tools/gates.py:112`).

### 4.2 Gate 2: `g_anchor` (`tools/gates.py:122-175`)

- **Rule**:
  - Helper `anchors_in(root)` scans every line of every Python file across `SRC_PKGS = ("visioncore", "vmem", "vadapt", "tools", "vnative")` for `INLINE_ANCHOR_RE = re.compile(r"(?:^|\s)# @anchor:(\S+)\s*$")` (`tools/gates.py:29-139`).
  - Every matched anchor comment `# @anchor:` must satisfy `ANCHOR_RE = re.compile(r"^# @anchor:(vc|vm|va|tools)\.[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+$")` (at least 3 dot-separated segments starting with `vc`, `vm`, `va`, or `tools`) and must not duplicate any previously seen anchor `a in found` (`tools/gates.py:32-139`).
  - If `symbols.jsonl` exists under `catalogue`, each non-blank JSONL row (`path`, `line`, `anchor`, optional `symbol`) is checked to ensure `1 <= ln <= len(lines)` and want in lines[ln - 1] (where `want = "@anchor:" + row["anchor"]`), and if `row.get("symbol")` is truthy, that `row["symbol"]` appears in at least one line of `lines[ln - 1:ln + 3]` (`tools/gates.py:144-157`).
  - If `layers.json` exists under `catalogue`, parses `pending_anchors` into `pending` (`tools/gates.py:160-165`).
  - If `routes.tsv` exists under `catalogue`, checks every line with `len(cols) >= 2` (tab-separated) to ensure target `cols[1]` is in `found` or in `pending` (`tools/gates.py:166-171`).
  - Verifies that no anchor in `pending` already exists in `found` (`pending & set(found)` must be empty) (`tools/gates.py:172-173`).
- **Files Scanned**:
  - `*.py` under `SRC_PKGS = ("visioncore", "vmem", "vadapt", "tools", "vnative")` (`tools/gates.py:29-126`).
  - `symbols.jsonl`, `layers.json`, and `routes.tsv` under `catalogue` (`tools/gates.py:144-167`).
- **Finding Codes Emitted**:
  - `("E_GATE_ANCHOR", f"{rel(root, p)}:{i} bad anchor grammar")` (`tools/gates.py:132`).
  - `("E_GATE_ANCHOR", f"duplicate anchor {a} ({found[a]} and {rel(root, p)}:{i})")` (`tools/gates.py:136`).
  - `("E_DRIFT", f"symbols.jsonl:{n} anchor {row['anchor']} not at {row['path']}:{ln}")` (`tools/gates.py:155`).
  - `("E_GATE_ANCHOR", f"symbols.jsonl:{n} symbol {row['symbol']} not near anchor")` (`tools/gates.py:157`).
  - `("E_GATE_ANCHOR", "layers.json invalid JSON")` (`tools/gates.py:165`).
  - `("E_GATE_ANCHOR", f"routes.tsv:{n} target {cols[1]} has no code anchor (and is not in layers.json pending_anchors)")` (`tools/gates.py:171`).
  - `("E_GATE_ANCHOR", f"layers.json pending_anchors lists {a} but it exists in code: remove it")` (`tools/gates.py:173`).
- **Exemptions**:
  - Checks on `symbols.jsonl`, `layers.json`, and `routes.tsv` are skipped if the respective file is absent (`tools/gates.py:144-167`).
  - Route targets listed in `pending_anchors` in `layers.json` are exempt from existing in source code (`tools/gates.py:160-171`).

### 4.3 Gate 3: `g_header` (`tools/gates.py:177-225`)

- **Rule**:
  - Helper `header_of(text)` searches lines for `# @module `, concatenates that line's remainder with any immediately following `#   ` continuation lines, and returns the resulting string or `None` (`tools/gates.py:177-188`).
  - For each file in `pyfiles(root, "visioncore")` that has a header (raw is not None), `g_header` parses `h = json.loads(raw)` (`tools/gates.py:191-203`).
  - Checks `h.get("layer") != LAYER.get(st)` where `st = stem_of(root, p)` (`tools/gates.py:204-206`).
  - Checks `not isinstance(h.get("deps"), list)` (`tools/gates.py:207-209`).
  - Computes `want`: the set of `visioncore` submodules `sub` imported by `p` via `imports_of(parse(p))` where `top == "visioncore" and sub in LAYER and sub != st and sub != "schemas"` (`tools/gates.py:210-213`).
  - Computes `have`: for each entry `d` in `h["deps"]`, strips a leading `"vc."` prefix (`d.split(".")[-1] if d.startswith("vc.") else d`) and maps stage aliases via STAGE_ALIAS = {"p": "params", "s0": "grid", "s1": "desc", "s2": "change", "s3": "pyramid", "s4": "regions", "s5": "gop", "s6": "pack"} (`tools/gates.py:24-217`).
  - Applies symmetric `pack`/`cite` equivalence (if "pack" in have and "cite" in want and "pack" not in want: have.add("cite") and if "cite" in have and "pack" in want and "cite" not in want: have.add("pack")) and checks `want != have` (`tools/gates.py:218-223`).
- **Files Scanned**: All Python files in `visioncore` via `pyfiles(root, "visioncore")` (`tools/gates.py:193`).
- **Finding Codes Emitted**:
  - `("E_GATE_HEADER", f"{r} header is not JSON")` (`tools/gates.py:202`).
  - `("E_GATE_HEADER", f"{r} layer {h.get('layer')} != {LAYER.get(st)}")` (`tools/gates.py:206`).
  - `("E_GATE_HEADER", f"{r} deps missing")` (`tools/gates.py:208`).
  - `("E_GATE_HEADER", f"{r} deps {sorted(have)} != imports {sorted(want)}")` (`tools/gates.py:223`).
- **Exemptions**:
  - Any `visioncore` file without `# @module ` (`raw is None`) is skipped (`tools/gates.py:177-197`).
  - Self-imports (`sub != st`) and `schemas` imports (`sub != "schemas"`) are excluded from want (`tools/gates.py:212`).
  - `pack` and `cite` satisfy each other in `have` vs. `want` when only one of them is imported (`tools/gates.py:218-221`).

### 4.4 Gate 4: `g_layer` (tools/gates.py:227-280, tools/gates.py:765-767)

- **Rule**:
  - **1. `visioncore` package** (`tools/gates.py:17-256`):
    - Maps st = stem_of(root, p) through `LAYER` (tools/gates.py:17-23, tools/gates.py:230-234):
      - Layer `0`: `ids`, `params`, `layout`, `errors`, `canon`, `frame`, `schemas`, `topology`
      - Layer `1`: `decode`, `grid`, `desc`
      - Layer `2`: `change`, `pyramid`, `regions`
      - Layer `3`: `gop`, `pack`, `cite`
      - Layer `4`: `pipeline`, `cli`, `__init__`, `parallel`
    - Unknown stem `st not in LAYER` emits `E_GATE_LAYER_PKG` (`tools/gates.py:231-233`).
    - Deduplicates `(top, sub)` pairs per file in `seen` (`tools/gates.py:236-241`):
      - Upward import (`top == "visioncore"` and `sub in LAYER and LAYER[sub] > me`) emits `E_GATE_LAYER_UP` (`tools/gates.py:243-245`).
      - Forbidden package (`top in FORBID_VC or top.startswith("godot")`, where `FORBID_VC = {"vmem", "vadapt", "jsonschema", "bpy", "random"}`) emits `E_GATE_LAYER_PKG` (`tools/gates.py:28-247`).
      - `top == "numpy"` when `me == 0` emits `E_GATE_LAYER_PKG` (`tools/gates.py:248-250`).
      - Any other non-stdlib import (`not is_stdlib(top)`) emits `E_GATE_LAYER_PKG` (`tools/gates.py:251-252`).
    - In any file with `p.name != "cli.py"`, every AST attribute node matching `isinstance(n, ast.Attribute) and n.attr == "time" and isinstance(n.value, ast.Name) and n.value.id == "time"` emits `E_GATE_LAYER_PKG` (`tools/gates.py:253-256`).
  - **2. `vadapt` package** (`tools/gates.py:26-265`):
    - Importing `top == "visioncore"` with `sub not in VADAPT_OK` (`VADAPT_OK = {"frame", "ids", "errors", "canon", "params", "decode"}`) emits `E_GATE_LAYER_PKG` (`tools/gates.py:26-261`).
    - Importing `top == "vmem"` emits `E_GATE_LAYER_PKG` (`tools/gates.py:262-263`).
    - In `p.name == "ramws.py"`, importing `top == "visioncore" or not is_stdlib(top)` emits `E_GATE_LAYER_PKG` (`tools/gates.py:264-265`).
  - **3. `vmem` package** (`tools/gates.py:27-274`):
    - Importing `top == "visioncore"` with `sub not in VMEM_OK` (`VMEM_OK = {"ids", "layout", "errors", "canon", "schemas"}`) emits `E_GATE_LAYER_PKG` (`tools/gates.py:27-270`).
    - Importing `top in ("vadapt", "numpy")` emits `E_GATE_LAYER_PKG` (`tools/gates.py:271-272`).
    - Importing `top not in ("vmem", "visioncore") and not is_stdlib(top)` emits `E_GATE_LAYER_PKG` (`tools/gates.py:273-274`).
  - **4. `tools` package** (`tools/gates.py:275-278`):
    - Importing `top not in ("tools",) and not is_stdlib(top)` emits `E_GATE_LAYER_PKG` (`tools/gates.py:275-278`).
  - **5. `vnative` package** (`_vnative_layer(root)` at tools/gates.py:279 and tools/gates.py:765-767):
    - Importing `top not in ("vnative", "visioncore", "numpy") and not is_stdlib(top)` emits `E_GATE_LAYER_PKG` (`tools/gates.py:765-767`).
- **Files Scanned**: Python files via pyfiles(root, pkg) across `visioncore`, vadapt, vmem, tools, and `vnative` (tools/gates.py:229-279, tools/gates.py:765-767).
- **Finding Codes Emitted**:
  - `("E_GATE_LAYER_PKG", f"{rel(root, p)} unknown module {st}")` (`tools/gates.py:232`).
  - `("E_GATE_LAYER_UP", f"{r} imports visioncore.{sub} (L{LAYER[sub]} > L{me})")` (`tools/gates.py:245`).
  - `("E_GATE_LAYER_PKG", f"{r} forbidden import {top}")` (`tools/gates.py:247`).
  - `("E_GATE_LAYER_PKG", f"{r} numpy in L0")` (`tools/gates.py:250`).
  - `("E_GATE_LAYER_PKG", f"{r} third-party import {top}")` (`tools/gates.py:252`).
  - `("E_GATE_LAYER_PKG", f"{rel(root, p)}:{n.lineno} time.time outside cli.py")` (`tools/gates.py:256`).
  - `("E_GATE_LAYER_PKG", f"{r} vadapt imports visioncore.{sub}")` (`tools/gates.py:261`).
  - `("E_GATE_LAYER_PKG", f"{r} vadapt imports vmem")` (`tools/gates.py:263`).
  - `("E_GATE_LAYER_PKG", f"{r} ramws imports {top} (stdlib only)")` (`tools/gates.py:265`).
  - `("E_GATE_LAYER_PKG", f"{r} vmem imports visioncore.{sub}")` (`tools/gates.py:270`).
  - `("E_GATE_LAYER_PKG", f"{r} vmem imports {top}")` (`tools/gates.py:272`).
  - `("E_GATE_LAYER_PKG", f"{r} vmem third-party import {top}")` (`tools/gates.py:274`).
  - `("E_GATE_LAYER_PKG", f"{rel(root, p)}:{ln} tools imports non-stdlib {top}")` (`tools/gates.py:278`).
  - `("E_GATE_LAYER_PKG", f"{rel(root, p)}:{ln} vnative imports {top}")` (`tools/gates.py:766`).
- **Exemptions**:
  - `cli.py` inside visioncore is exempt from the `time.time` check (`tools/gates.py:253-256`).
  - `numpy` is permitted in visioncore layers 1–4 (me != 0) and in vnative (tools/gates.py:248-250, tools/gates.py:765-767).
  - `vadapt` modules other than `ramws.py` have no stdlib-only restriction in g_layer (`tools/gates.py:257-265`).

### 4.5 Gate 5: `g_threshold` (`tools/gates.py:295-376`)

- **Rule**:
  - Helper `load_params(root)` loads `PARAMS` from `root / "visioncore" / "params.py"`, first attempting `ast.literal_eval` on top-level `PARAMS` assignments (unwrapping single-argument calls `tgt.args[0]`), and falling back to executing `visioncore.params` via `importlib.util.spec_from_file_location` (`tools/gates.py:295-329`).
  - Reads tsv = root / "catalogue" / "thresholds.tsv" (tools/gates.py:337-339). Each non-empty line not starting with "#" or "threshold_id" is split via line.split("\t") if "\t" in line else line.split() (tools/gates.py:341-344).
  - Each row must have at least 3 fields (`len(f) < 3` fails), a non-duplicate ID `f[0]`, and `f[0]` matching `TID_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,40}$")` (`tools/gates.py:35-352`).
  - Enforces exact key-set equality and string-value equality (`str(params[k]) != rows[k]`) between `params` and `rows` (`tools/gates.py:353-359`).
  - Loads `allow = set(json.loads(lj.read_text(encoding="utf-8")).get("structural_constants", []))` from catalogue/layers.json if present (`tools/gates.py:360-366`).
  - Scans top-level statements `parse(p).body` of every `visioncore` file with `LAYER.get(stem_of(root, p), 0)` in `(1, 2, 3)`: any `ast.Assign` whose value is an integer constant (`isinstance(n.value, ast.Constant) and type(n.value.value) is int`) assigned to an uppercase `ast.Name` (`t.id.lstrip("_").isupper()`) must satisfy t.id in params or t.id in allow (`tools/gates.py:367-375`).
- **Files Scanned**: `params.py` in `visioncore`, `thresholds.tsv` and `layers.json` in `catalogue`, and `visioncore` Python files in layers `(1, 2, 3)` (`tools/gates.py:296-368`).
- **Finding Codes Emitted**:
  - ("E_GATE_THRESHOLD", "params.py missing") or ("E_GATE_THRESHOLD", f"params.py not loadable: {e}") (`tools/gates.py:298-336`).
  - `("E_GATE_THRESHOLD", "catalogue/thresholds.tsv missing")` (`tools/gates.py:339`).
  - `("E_GATE_THRESHOLD", f"thresholds.tsv:{n} fewer than 3 fields")` (`tools/gates.py:346`).
  - `("E_GATE_THRESHOLD", f"thresholds.tsv:{n} duplicate {f[0]}")` (`tools/gates.py:349`).
  - `("E_GATE_THRESHOLD", f"thresholds.tsv:{n} bad id {f[0]}")` (`tools/gates.py:351`).
  - `("E_GATE_THRESHOLD", f"PARAMS key {k} has no thresholds.tsv row")` (`tools/gates.py:354`).
  - `("E_GATE_THRESHOLD", f"thresholds.tsv row {k} not in PARAMS")` (`tools/gates.py:356`).
  - `("E_GATE_THRESHOLD", f"{k}: PARAMS {params[k]} != tsv {rows[k]}")` (`tools/gates.py:359`).
  - ("E_GATE_THRESHOLD", f"{rel(root, p)}:{n.lineno} constant {t.id} is not a PARAMS row (add it to params.py + thresholds.src, or list it in layers.src structural_constants)") (`tools/gates.py:374-375`).
- **Exemptions**:
  - visioncore files with `LAYER.get(stem_of(root, p), 0) not in (1, 2, 3)` (layers 0 and 4) are skipped during the constant scan (`tools/gates.py:368`).
  - Constants listed in `structural_constants` in layers.json are exempt (`tools/gates.py:364-373`).
  - Non-uppercase names, bool values (`type(n.value.value) is int` is False for booleans), ast.AnnAssign, and function-local assignments are not flagged (`tools/gates.py:370-373`).

### 4.6 Gate 6: `g_nofloat` (`tools/gates.py:379-462`)

- **Rule**:
  - Scans AST nodes of all `visioncore` files except those with `LAYER.get(stem_of(root, p), 9) > 3` (`tools/gates.py:416-418`).
  - Rejects 15 floating-point or rational constructs (`tools/gates.py:398-460`):
    1. Float literal: `isinstance(n, ast.Constant) and isinstance(n.value, float)` -> `"float literal"` (`tools/gates.py:427-428`).
    2. `float()` call: `isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "float"` -> `"float()"` (`tools/gates.py:429-430`).
    3. Binary true division: `isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)` when `not any(_strish(o) for o in (n.left, n.right))` -> `"true division"` (`tools/gates.py:431-433`).
    4. Augmented division: `isinstance(n, ast.AugAssign) and isinstance(n.op, ast.Div)` -> `"true division"` (`tools/gates.py:434-435`).
    5. `float` attribute access: `n.value.id == "float"` -> `"float." + n.attr` (`tools/gates.py:436-437`).
    6. NumPy float dtype/attribute: `n.attr.startswith("float")` on `n.value.id in ("np", "numpy")` -> `n.value.id + "." + n.attr` (`tools/gates.py:438-439`).
    7. Non-whitelisted `math` attribute: `n.value.id == "math" and n.attr not in MATH_OK` where `MATH_OK = {"isqrt", "gcd", "comb", "perm", "lcm", "factorial"}` -> `"math." + n.attr` (`tools/gates.py:398-441`).
    8. Operator true division: `n.value.id == "operator" and n.attr in ("truediv", "itruediv")` -> `"operator." + n.attr` (`tools/gates.py:442-443`).
    9. Float-producing NumPy functions: `n.value.id in ("np", "numpy") and n.attr in NP_FLOATY` where `NP_FLOATY = {"mean", "average", "median", "std", "var", "divide", "true_divide", "sqrt", "linspace", "power", "log", "exp"}` -> `n.value.id + "." + n.attr` (`tools/gates.py:400-445`).
    10. Statistical method calls: `ast.Call` with `n.func.attr in ("mean", "std", "var", "median")` -> `"." + n.func.attr + "()"` (`tools/gates.py:446-447`).
    11. Unsafe exponentiation operator: `(ast.BinOp, ast.AugAssign)` with `ast.Pow` failing `_safe_pow` -> `"power with a non-constant or negative exponent"` (`tools/gates.py:448-450`).
    12. Unsafe `pow` call: `n.func.id == "pow" and len(n.args) >= 2 and not _safe_pow(n.args[0], n.args[1])` -> `"pow() with a non-constant or negative exponent"` (`tools/gates.py:451-452`).
    13. Disallowed module import: `ast.Import` with `a.name.split(".")[0] in BAD_MODULES` where `BAD_MODULES = {"statistics", "fractions", "decimal", "cmath"}` -> `"import of a float/rational module"` (`tools/gates.py:399-454`).
    14. Disallowed from ... import: `ast.ImportFrom` from `BAD_MODULES | {"operator"}` (when importing `"truediv"` or `"itruediv"` from `"operator"`, or anything from `BAD_MODULES`) -> `"import from " + n.module` (`tools/gates.py:455-457`).
    15. Disallowed `from math import`: `ast.ImportFrom` with `n.module == "math"` and any `a.name not in MATH_OK` -> `"from math import " + ",".join(a.name for a in n.names if a.name not in MATH_OK)` (`tools/gates.py:458-459`).
- **Files Scanned**: `visioncore` Python files in layers 0–3 (LAYER.get(stem_of(root, p), 9) <= 3) (`tools/gates.py:416-418`).
- **Finding Codes Emitted**:
  - `("E_GATE_NOFLOAT", f"{rel(root, p)}:{ln} {bad}")` (`tools/gates.py:461`).
- **Exemptions**:
  - Layer 4 modules (`LAYER.get(stem_of(root, p), 9) > 3`) are skipped (`tools/gates.py:417-418`).
  - Functions preceded within 4 lines by @anchor: + EXEMPT_ANCHOR (EXEMPT_ANCHOR = "vc.s1.isqrt") have their line span (n.lineno, n.end_lineno) exempted by exempt_ranges(text, tree) (tools/gates.py:43, tools/gates.py:379-387, tools/gates.py:421-425).
  - Binary / where either operand satisfies `_strish(o)` (ast.JoinedStr, string ast.Constant, or ast.Mod whose left operand is a string ast.Constant, such as path / "x") is exempt (tools/gates.py:390-395, tools/gates.py:431-433).
  - Integer math functions in `MATH_OK = {"isqrt", "gcd", "comb", "perm", "lcm", "factorial"}` are allowed (`tools/gates.py:398-459`).
  - Powers satisfying `_safe_pow(base, exp)` — either `_nonneg_int(exp)` (`isinstance(o, ast.Constant) and type(o.value) is int and o.value >= 0`) or `base.value in (2, 4)` (with `type(base.value) is int`) raised to an `ast.Name` exponent — are allowed (`tools/gates.py:403-411`).

### 4.7 Gate 7: `g_engine_free` (`tools/gates.py:465-489`)

- **Rule**:
  - Tokenizes every file in `pyfiles(root, "visioncore")` using `tokenize.generate_tokens(io.StringIO(text).readline)` (`tools/gates.py:467-473`).
  - Checks every token `t` against ENGINE_NAMES = {"adapter_id", "bpy", "blender", "opensuperficies"} (tools/gates.py:37, tools/gates.py:474-488):
    - For `t.type == tokenize.NAME`: if `low in ENGINE_NAMES or low.startswith("godot")` (where `low = t.string.lower()`), sets `hit = t.string` (`tools/gates.py:476-479`).
    - For `t.type == tokenize.STRING`: evaluates `v = ast.literal_eval(t.string)` and if `isinstance(v, str) and (v.lower() in ENGINE_NAMES or v.lower().startswith("godot") and " " not in v)`, sets `hit = v` (`tools/gates.py:480-486`).
- **Files Scanned**: `visioncore` Python files via `pyfiles(root, "visioncore")` (`tools/gates.py:467`).
- **Finding Codes Emitted**:
  - `("E_GATE_ENGINE", f"{rel(root, p)} untokenizable: {e}")` (`tools/gates.py:472`).
  - `("E_GATE_ENGINE", f"{rel(root, p)}:{t.start[0]} engine token {hit}")` (`tools/gates.py:488`).
- **Exemptions**:
  - Comments (tokenize.COMMENT) are not inspected (`tools/gates.py:476-480`).
  - String literals starting with `"godot"` that contain a space (`" " not in v` is False) or string tokens that raise `(ValueError, SyntaxError)` in `ast.literal_eval` are ignored (`tools/gates.py:481-486`).

### 4.8 Gate 8: `g_ids` (`tools/gates.py:492-522`)

- **Rule**:
  - Walks AST nodes of every file in `pyfiles(root, pkg)` for `pkg in ("visioncore", "vmem")` except `p.name == "ids.py"` (`tools/gates.py:500-504`).
  - Detects four ID-formatting patterns (tools/gates.py:38-40, tools/gates.py:492-521):
    1. **f-strings (ast.JoinedStr)**: Replaces constant string segments with v.value and formatted values with "{}" and tests ID_TEMPLATES.match(tpl) or ID_FMT_TPL.match(tpl) (tools/gates.py:38-40, tools/gates.py:506-508), where:
       - `ID_TEMPLATES = re.compile(r"^([frpk]\{\}:(t\{\}|all|\{\})|p\{\}:L\{\}:\{\}|g\{\})$")` (`tools/gates.py:38`)
       - `ID_FMT_TPL = re.compile(r"^([frpkg]\{[^}]*\}:(t\{[^}]*\}|all|\{[^}]*\})|p\{[^}]*\}:L\{[^}]*\}:\{[^}]*\}|g\{[^}]*\}|r_\{[^}]*\}_\{[^}]*\}|V-\{[^}]*\}-\{[^}]*\}|T:\{[^}]*\})")` (`tools/gates.py:40`)
    2. **%-format strings**: isinstance(n, ast.Constant) and isinstance(n.value, str) and ID_PCT.match(n.value) where ID_PCT = re.compile(r"^([frpk]%[0-9]*[dsx]:|g%[0-9]*[dsx]$|r_%0?6d|V-%s-%0?6d|T:%s)") (tools/gates.py:39, tools/gates.py:509-510).
    3. **.format calls**: ast.Call with n.func.attr == "format" on a string constant n.func.value.value matching `ID_FMT_TPL` (tools/gates.py:40, tools/gates.py:511-513).
    4. **String concatenation (`ast.Add`)**: Flattens `+` operands via `_add_leaves(n)` and checks whether the first leaf is a string constant in `("f", "r", "p", "k", "g")` and any string constant leaf in `cs` satisfies `c.startswith((":t", ":L", ":all"))` (`tools/gates.py:492-519`).
- **Files Scanned**: Python files in `("visioncore", "vmem")` with p.name != "ids.py" (`tools/gates.py:500-503`).
- **Finding Codes Emitted**:
  - `("E_GATE_IDS", f"{rel(root, p)}:{n.lineno} formats an id outside ids.py")` (`tools/gates.py:521`).
- **Exemptions**:
  - Any file named `ids.py` (`p.name == "ids.py"`) is skipped (`tools/gates.py:502-503`).
  - Packages outside `("visioncore", "vmem")` are not scanned (`tools/gates.py:500`).

### 4.9 Gate 9: g_hint_neutral (tools/gates.py:629-630, tools/gate_checks.py:132-135)

- **Rule**: Calls _checks().hint_neutral(root) (tools/gates.py:629-630), which returns [] (tools/gate_checks.py:132-134). Listed in NA_GATES = ("g_hint_neutral",) so receipt(res) appends NA:g_hint_neutral when evaluated (tools/gates.py:641-665).
- **Files Scanned**: None (`tools/gate_checks.py:132-134`).
- **Finding Codes Emitted**: None (`tools/gate_checks.py:134`).
- **Exemptions**: Unconditionally returns [] and is reported as NA:g_hint_neutral (`tools/gates.py:641-665`). See Section 6.4.

### 4.10 Gate 10: g_golden (tools/gates.py:621-622, tools/gate_checks.py:56-99)

- **Rule**: Calls _checks().golden(root) (tools/gates.py:621-622) to regenerate every synthetic golden fixture in tests/golden and compare frame_sha256, frames_sha256, frames.sha256, and S0–S2 tile class_hist in classes.json (tools/gate_checks.py:56-98). See Section 6.2.
- **Files Scanned**: `synth.json`, `frames.sha256`, and `classes.json` under tests/golden/*/expect (`tools/gate_checks.py:58-96`).
- **Finding Codes Emitted**: `E_GATE_GOLDEN` (`tools/gate_checks.py:75-96`).
- **Exemptions**: Returns [] if root / "tests" / "golden" is not a directory (tools/gate_checks.py:58-60), and skips entries in gd.iterdir() where (x / "expect").is_dir() is False (tools/gate_checks.py:70).

### 4.11 Gate 11: g_catalogue (tools/gates.py:617-618, tools/gate_checks.py:28-41)

- **Rule**: Calls _checks().catalogue(root) (tools/gates.py:617-618), which invokes bc.build(str(root)) from build_catalogue and verifies that every generated file in `catalogue` exists and equals the freshly generated text (tools/gate_checks.py:28-40). See Section 6.1.
- **Files Scanned**: `layers.src` and generated files in `catalogue` (`tools/gate_checks.py:30-40`).
- **Finding Codes Emitted**: `E_GATE_CATALOGUE` and `E_STALE` (`tools/gate_checks.py:37-38`).
- **Exemptions**: Returns `[]` if `not (cat / "layers.src").is_file()` (`tools/gate_checks.py:31-32`).

### 4.12 Gate 12: `g_schema` (`tools/gates.py:535-569`)

- **Rule**:
  - **1. JSON Schemas** (tools/gates.py:34, tools/gates.py:537-553):
    - Requires `sd = root / "visioncore" / "schemas"` to be a directory with at least one `*.schema.json` file (`tools/gates.py:537-539`).
    - Parses each `p` in `sorted(sd.glob("*.schema.json"))` via `json.loads` (`tools/gates.py:540-545`).
    - Reads `sid = o.get("$id")`, falling back to `(o.get("properties", {}).get("schema") or {}).get("const")` when `sid is None` (`tools/gates.py:546-548`).
    - Verifies isinstance(sid, str) and SCHEMA_ID_RE.match(sid) where SCHEMA_ID_RE = re.compile(r"^(vqa|vmem)\.[a-z0-9-]+/v[1-9][0-9]*$") (tools/gates.py:34, tools/gates.py:549-551).
    - Verifies p.name == sid.replace("/v", ".v") + ".schema.json" (`tools/gates.py:552-553`).
  - **2. ids.py Smoke Verification** (tools/gates.py:282-292, tools/gates.py:554-568):
    - Requires (root / "visioncore" / "ids.py").is_file() and imports visioncore.ids via import_vc(root, "ids") (tools/gates.py:282-292, tools/gates.py:554-557).
    - Checks `callable(getattr(mod, fn, None))` for `fn in ("field_id", "region_id")` (`tools/gates.py:558-560`).
    - Checks mod.field_id(fr, m) == want for `((84, 0x0A40, "f84:t0a40"), (0, 0, "f0:t0000"), (7, 0x1A3, "f7:t01a3"))` (`tools/gates.py:561-564`).
- **Files Scanned**: `*.schema.json` in `visioncore/schemas` and `ids.py` in `visioncore` (`tools/gates.py:537-555`).
- **Finding Codes Emitted**:
  - `("E_GATE_SCHEMA", "visioncore/schemas has no *.schema.json")` (`tools/gates.py:539`).
  - `("E_GATE_SCHEMA", f"{p.name} invalid JSON: {e}")` (`tools/gates.py:544`).
  - `("E_GATE_SCHEMA", f"{p.name} id {sid!r} violates vqa.<name>/v<N>")` (`tools/gates.py:550`).
  - `("E_GATE_SCHEMA", f"{p.name} does not match id {sid}")` (`tools/gates.py:553`).
  - `("E_GATE_SCHEMA", f"ids.{fn} missing")` (`tools/gates.py:560`).
  - `("E_GATE_SCHEMA", f"field_id({fr},{m}) != {want}")` (`tools/gates.py:564`).
  - `("E_GATE_SCHEMA", f"ids.py not loadable: {e}")` (`tools/gates.py:566`).
  - `("E_GATE_SCHEMA", "visioncore/ids.py missing")` (`tools/gates.py:568`).
- **Exemptions**: None (`tools/gates.py:535-569`).

### 4.13 Gate 13: g_route_budget (tools/gates.py:625-626, tools/gate_checks.py:101-129)

- **Rule**: Calls _checks().route_budget(root) (tools/gates.py:625-626) to verify that route_queries.tsv has at least 20 queries and every query routes within rc_mod.MAX_B bytes to its target anchor in the top 3 (with path and line > 0) or abstains when want == "ABSTAIN" (tools/gate_checks.py:101-129). See Section 6.3.
- **Files Scanned**: `route_queries.tsv` and `routes.tsv` under `catalogue`, plus `route_code` via `load_tool` (`tools/gate_checks.py:104-107`).
- **Finding Codes Emitted**: `E_GATE_ROUTE` (`tools/gate_checks.py:106-128`).
- **Exemptions**: Returns [] when both route_queries.tsv and routes.tsv are absent (tools/gate_checks.py:105-106), and skips blank or #-prefixed lines (tools/gate_checks.py:111-112).

### 4.14 Gate 14: `g_size` (`tools/gates.py:525-532`)

- **Rule**:
  - For every pkg in SRC_PKGS = ("visioncore", "vmem", "vadapt", "tools", "vnative") and every file `p` in pyfiles(root, pkg), computes n = len(p.read_text(encoding="utf-8").splitlines()) and checks n > MAX_LINES where MAX_LINES = 800 (tools/gates.py:29-30, tools/gates.py:525-532).
- **Files Scanned**: Python files in SRC_PKGS = ("visioncore", "vmem", "vadapt", "tools", "vnative") (tools/gates.py:29-30, tools/gates.py:527-528).
- **Finding Codes Emitted**:
  - `("E_GATE_SIZE", f"{rel(root, p)} {n} lines > {MAX_LINES}")` (`tools/gates.py:531`).
- **Exemptions**: Directories outside `SRC_PKGS` are not scanned (tools/gates.py:29, tools/gates.py:527).

### 4.15 Gate 15: `g_canon` (`tools/gates.py:572-588`)

- **Rule**:
  - Collects *.json in root / "catalogue" and expect/*.json recursively under root / "tests" / "golden", and checks canon(json.loads(t)) != t (tools/gates.py:46-47, tools/gates.py:574-582).
  - Collects `*.jsonl` in `root / "catalogue"` and checks each non-blank line (`line.strip()`) for `canon(json.loads(line)) != line + "\n"` (`tools/gates.py:583-588`).
- **Files Scanned**: `*.json` and `*.jsonl` in `catalogue`, and `expect/*.json` under tests/golden (`tools/gates.py:574-584`).
- **Finding Codes Emitted**:
  - `("E_GATE_CANON", f"{rel(root, p)} not canonical")` (`tools/gates.py:580`).
  - `("E_GATE_CANON", f"{rel(root, p)} invalid JSON")` (`tools/gates.py:582`).
  - `("E_GATE_CANON", f"{rel(root, p)}:{n} not canonical")` (`tools/gates.py:587`).
- **Exemptions**: Missing catalogue or tests/golden directories are skipped (tools/gates.py:574-583), and blank lines in *.jsonl are ignored (tools/gates.py:586).

### 4.16 Gate 16: `g_pure` (`tools/gates.py:591-610`)

- **Rule**:
  - Checks `(root / "tools" / "gates.py").is_file()` (`tools/gates.py:593-594`).
  - Scans every file in pyfiles(root, "tools") + pyfiles(root, "vnative") (tools/gates.py:36, tools/gates.py:596-610):
    1. Any import via imports_of(tree, "tools") with top in IMPURE_MODS (IMPURE_MODS = {"time", "random", "datetime", "secrets", "uuid"}) emits E_GATE_PURE (tools/gates.py:36, tools/gates.py:599-601).
    2. Any AST attribute `n.value.id == "os" and n.attr in ("urandom", "getpid", "getrandom", "times")` emits `E_GATE_PURE` (`tools/gates.py:603-604`).
    3. Any AST name `n.id == "__import__"` emits `E_GATE_PURE` (`tools/gates.py:605-606`).
    4. Any call `n.func.attr == "import_module"` whose first constant argument has `str(n.args[0].value).split(".")[0] in IMPURE_MODS` emits `E_GATE_PURE` (`tools/gates.py:607-609`).
- **Files Scanned**: `tools/gates.py` (existence check) and `pyfiles(root, "tools") + pyfiles(root, "vnative")` (`tools/gates.py:593-596`).
- **Finding Codes Emitted**:
  - `("E_GATE_PURE", "tools/gates.py missing")` (`tools/gates.py:594`).
  - `("E_GATE_PURE", f"{r}:{ln} imports {top}")` (`tools/gates.py:601`).
  - `("E_GATE_PURE", f"{r}:{n.lineno} os.{n.attr}")` (`tools/gates.py:604`).
  - `("E_GATE_PURE", f"{r}:{n.lineno} __import__")` (`tools/gates.py:606`).
  - `("E_GATE_PURE", f"{r}:{n.lineno} import_module of {n.args[0].value}")` (`tools/gates.py:609`).
- **Exemptions**: Packages outside `tools` and `vnative` are not scanned (`tools/gates.py:596`).

### 4.17 Gate 17: g_sealed_untouched (tools/gates.py:633-634, tools/gate_checks.py:142-200)

- **Rule**: Calls _checks().sealed_untouched(root) (tools/gates.py:633-634) to verify that visioncore and vmem are byte-identical to Git tag ve-sealed-1 (no tracked diffs and no untracked files), except for files covered by a verified RT0D01 or RT0D02 EQUIV_RECEIPT.json and PROOF_SHA_TABLE.json (tools/gate_checks.py:142-200). See Section 6.5.
- **Files Scanned**: `.git`, tag `ve-sealed-1`, `visioncore`, `vmem`, `EQUIV_RECEIPT.json`, and `PROOF_SHA_TABLE.json` (`tools/gate_checks.py:144-200`).
- **Finding Codes Emitted**: `E_GATE_SEALED` (`tools/gate_checks.py:147-200`).
- **Exemptions**:
  - Returns `[]` when `not (root / ".git").exists()` (`tools/gate_checks.py:144-145`).
  - Untracked paths containing `"__pycache__"` are ignored (`tools/gate_checks.py:153`).
  - Tracked files validated against `RT0D01` (`rt0`) or `RT0D02` (`rt0d02`) equivalence receipts are placed in `allowed` (`tools/gate_checks.py:157-199`).

### 4.18 Gate 18: g_disk_frames (tools/gates.py:637-638, tools/gate_checks.py:203-245)

- **Rule**: Calls _checks().disk_frames(root) (tools/gates.py:637-638) to run a 1-repeat eval.x1_determinism smoke test and verify that no frame-like files are left in build newer than .stamp, Path(ram.ROOT) / "engine" is empty, and no new blender or godot processes remain in /proc (tools/gate_checks.py:203-245). See Section 6.6.
- **Files Scanned**: eval/x1_determinism.py, vadapt/ramws.py, `build`, `Path(ram.ROOT) / "engine"`, and `/proc` (`tools/gate_checks.py:203-245`).
- **Finding Codes Emitted**: `E_GATE_DISK` (`tools/gate_checks.py:237-242`).
- **Exemptions**:
  - Returns `[]` if `not (root / "eval" / "x1_determinism.py").is_file()` (`tools/gate_checks.py:222-223`).
  - Pre-existing blender or godot processes recorded in `before = set(_live_engines())` are excluded (`tools/gate_checks.py:232-242`).

### 4.19 Gate 19: `g_native_scope` (`tools/gates.py:770-793`)

- **Rule**:
  - Appended via `GATES.append(("g_native_scope", g_native_scope))` (`tools/gates.py:793`).
  - Walks `os.walk(root)` pruning `NATIVE_SKIP = {".git", "build", "__pycache__", "target"}` (`tools/gates.py:770-778`).
  - Checks each file `p` (`tools/gates.py:779-790`):
    1. If `top in ("visioncore", "vmem")` and `any(k in p.read_bytes() for k in (b"vnative", b"VISIONCORE_BACKEND"))`, emits `E_GATE_NATIVE_SCOPE` (`tools/gates.py:781-782`).
    2. If `p.suffix == ".so" or ".so." in p.name`, emits `E_GATE_NATIVE_SCOPE` (`tools/gates.py:783-784`).
    3. Otherwise (elif p.suffix == ".py" and top != "vnative"), walks parse(p) and checks whether any ast.Attribute (n.attr), ast.Name (n.id), or ast.alias (getattr(n, "name", None)) is in DL_NAMES = {"CDLL", "PyDLL", "cdll", "pydll", "LoadLibrary", "WinDLL", "OleDLL", "windll", "oledll"} (tools/gates.py:771, tools/gates.py:785-789).
- **Files Scanned**: All files under `root` outside `NATIVE_SKIP = {".git", "build", "__pycache__", "target"}` (`tools/gates.py:770-779`).
- **Finding Codes Emitted**:
  - `("E_GATE_NATIVE_SCOPE", f"{r} mentions vnative/VISIONCORE_BACKEND")` (`tools/gates.py:782`).
  - `("E_GATE_NATIVE_SCOPE", f"{r} shared object in source tree")` (`tools/gates.py:784`).
  - `("E_GATE_NATIVE_SCOPE", f"{r}:{n.lineno} {hit} (dlopen only inside vnative/)")` (`tools/gates.py:789`).
- **Exemptions**:
  - Directories in `NATIVE_SKIP = {".git", "build", "__pycache__", "target"}` are skipped (`tools/gates.py:770-778`).
  - Files with top == "vnative" may reference names in `DL_NAMES` (`tools/gates.py:771-789`).

## 5. Mutants Mode (`run_mutants`)

Mutants mode is implemented by `run_mutants(root)` (tools/gates.py:675-718) and dispatched from main when mode == "mutants" (tools/gates.py:747-751).

1. **Mutant Discovery and Fail-Closed Guard**:
   - Inspects `mdir = root / "tests" / "mutants" / "gate"`, globs `files = sorted(mdir.glob("m_*.py")) if mdir.is_dir() else []`, and checks `base_p = mdir / "_base.py"` (`tools/gates.py:678-680`).
   - If not files or not base_p.is_file(), returns {"error": "no mutants or _base.py"}, ["no-mutants"] (tools/gates.py:681-682), causing main to emit "code": "E_MUTANT_SURVIVED" with "rc": 5 (tools/gates.py:748-750).
2. **Base Tree Loading**:
   - Inserts `str(mdir)` at the front of `sys.path`, removes `"_base"` from `sys.modules`, and calls `base = load_py(base_p, "_base").base_tree()` (`tools/gates.py:683-685`).
3. **Mutant Evaluation Loop**:
   - Creates `tmp = Path(tempfile.mkdtemp(prefix="vgm_"))` and iterates `for i, f in enumerate([None] + files):` (`tools/gates.py:687-714`):
     - **Clean Base (`i == 0`, `f is None`)**:
       - Copies `tree = dict(base)`, writes all files into `d = tmp / f"t{i}"`, and calls `res = run_gates(d)` and `failed, _ = receipt(res)` (`tools/gates.py:690-706`).
       - Sets `ok = not failed` and appends `{"mutant": "<clean base>", "expect": "pass", "failed": failed, "ok": ok}` to `results` (`tools/gates.py:707-709`).
       - If `not ok`, appends `results[-1]["mutant"]` (`"<clean base>"`) to `survived` (`tools/gates.py:709-714`).
     - **Mutant Overlay (`i >= 1`, `f` in `files`)**:
       - Loads `m = load_py(f, "_gate_mut_" + f.stem)` and reads `gate = m.GATE` (`tools/gates.py:693-694`).
       - Applies `m.OVERLAY.items()` to `tree`: if `v is None`, deletes `k` via `tree.pop(k, None)`; otherwise sets `tree[k] = v` (`tools/gates.py:695-699`).
       - Materializes `tree` under `d = tmp / f"t{i}"` and calls `res = run_gates(d)` and `failed, _ = receipt(res)` (`tools/gates.py:700-706`).
       - Sets `ok = gate in failed` (a mutant is killed if and only if its declared GATE failed), appends `{"mutant": f.stem, "gate": gate, "failed": failed, "killed": ok}` to `results`, and if `not ok` appends `results[-1]["mutant"]` (`f.stem`) to `survived` (`tools/gates.py:711-714`).
   - Always deletes `tmp` via `shutil.rmtree(tmp, ignore_errors=True)` and removes `str(mdir)` from `sys.path` in `finally` (`tools/gates.py:715-717`).
4. **Unit Tests for Mutants Mode (`tests/test_gates.py:16-28`)**:
   - `test_mutants_all_killed` runs `run("--mutants", "--json")` and asserts `rc` is `0`, `o["code"]` is `"OK"`, and `o["killed"]` is >= 16 (`tests/test_gates.py:17-21`).
   - `test_every_gate_has_a_mutant` collects `{p.name.split("__")[0][2:] for p in (ROOT / "tests/mutants/gate").glob("m_*.py")}` and checks that each of `g_tier1`, `g_anchor`, `g_header`, `g_layer`, `g_threshold`, `g_nofloat`, `g_engine_free`, `g_ids`, `g_schema`, `g_size`, `g_canon`, and `g_pure` is present in `names` (`tests/test_gates.py:23-27`).

## 6. Heavier Gate Bodies in `tools/gate_checks.py`

### 6.0 Shared Helpers in `tools/gate_checks.py`

- **`load_py(path, name)`** (`tools/gate_checks.py:13-17`): Loads a module from `path` via `importlib.util.spec_from_file_location(name, path)` and `spec.loader.exec_module(mod)`.
- **`load_tool(root, name)`** (`tools/gate_checks.py:20-25`): Resolves `p = root / "tools" / (name + ".py")`, falling back to `Path(__file__).resolve().parent / (name + ".py")` when `not p.is_file()` (for mutant trees without `tools/`), and loads it as `"_gate_tool_" + name`.
- **`with_tree(root, fn)`** (`tools/gate_checks.py:43-54`): Saves `old = list(sys.path)`, purges `"visioncore"`, `"vadapt"`, and `"vmem"` modules from `sys.modules`, prepends `str(root)` to `sys.path`, runs `fn()`, and restores `sys.path[:] = old` and purges `sys.modules` in `finally`.
- **`_git(root, *args)`** (`tools/gate_checks.py:137-139`): Invokes `subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)`.
- **`_live_engines()`** (`tools/gate_checks.py:203-213`): Scans numeric directories in `Path("/proc").iterdir()`, reads `(p / "comm").read_text().strip().lower()`, and returns `f"{p.name}:{comm}"` for entries where `comm.startswith(("blender", "godot"))`.

### 6.1 Catalogue Staleness (`catalogue`, `tools/gate_checks.py:28-40`)

```python
def catalogue(root):
    """generated catalogue files must equal a fresh build_catalogue run (staleness)."""
    cat = root / "catalogue"
    if not (cat / "layers.src").is_file():
        return []  # trees without catalogue sources (gate mutant bases)
```

1. **Exemption Guard**: Returns `[]` if `not (cat / "layers.src").is_file()` (`tools/gate_checks.py:30-32`).
2. **In-Memory Rebuild**: Loads `bc = load_tool(root, "build_catalogue")` and executes `out, _ = bc.build(str(root))` (`tools/gate_checks.py:33-35`).
3. **Build Error Handling**: If bc.build raises `bc.BuildError as e`, returns `[("E_GATE_CATALOGUE", f"build failed: {e.code} {e.msg}")]` (`tools/gate_checks.py:36-37`).
4. **Staleness Check**: For each `name, text` in `sorted(out.items())`, if `not (cat / name).is_file() or (cat / name).read_text(encoding="utf-8") != text`, emits `("E_STALE", f"catalogue/{name} is stale: run tools/build_catalogue.py")` (`tools/gate_checks.py:38-40`).

### 6.2 Golden Replay (`golden`, `tools/gate_checks.py:56-98`)

```python
def golden(root):
    """W1 exit gate: regenerate every synth golden, compare frame shas and the S0-S2 class histograms."""
    gd = root / "tests" / "golden"
    if not gd.is_dir():
        return []
```

1. **Exemption Guard**: Returns `[]` if `not gd.is_dir()` where `gd = root / "tests" / "golden"` (`tools/gate_checks.py:58-60`).
2. **Isolated Package Execution**: Runs inner run() via with_tree(root, run) (tools/gate_checks.py:62-98), dynamically importing `numpy`, `vadapt.synth`, `vadapt.synth.synthetic`, `visioncore.pipeline`, and `visioncore.frame` (tools/gate_checks.py:63-68).
3. **Per-Scenario Verification**: Iterates `for d in sorted(x for x in gd.iterdir() if (x / "expect").is_dir()):` with `ex = d / "expect"` (`tools/gate_checks.py:70-71`):
   - **Step A — Load `synth.json`**: Parses `exp = json.loads((ex / "synth.json").read_text(encoding="utf-8"))`; on `(OSError, ValueError) as e`, appends `("E_GATE_GOLDEN", f"{d.name}: synth.json unreadable: {e}")` and continues (`tools/gate_checks.py:72-76`).
   - **Step B — Regenerate Frames and Compare SHA-256**: Calls frames, _, _ = synth.render_scenario(exp["scenario"], exp["seed"]) and shas = [syn.frame_sha(f) for f in frames] (tools/gate_checks.py:77-78). If shas != exp["frame_sha256"] or syn.frames_sha256(shas) != exp["frames_sha256"], appends ("E_GATE_GOLDEN", f"{d.name}: regenerated frames differ from expect/synth.json") (tools/gate_checks.py:79-80).
   - **Step C — Verify `frames.sha256` File**: Checks `fs = ex / "frames.sha256"`; if `not fs.is_file() or fs.read_text(encoding="utf-8") != exp["frames_sha256"] + "\n"`, appends `("E_GATE_GOLDEN", f"{d.name}: expect/frames.sha256 differs from synth.json")` (`tools/gate_checks.py:81-83`).
   - **Step D — Check `classes.json` Presence**: Checks `cp = ex / "classes.json"`; if `not cp.is_file()`, appends `("E_GATE_GOLDEN", f"{d.name}: expect/classes.json missing (class histograms are the S2 golden)")` and continues (`tools/gate_checks.py:84-87`).
   - **Step E — In-Memory S0–S2 Replay**: Constructs src = fr.MemoryFrameSource([fr.Frame(i, i * 33333, i, np.ascontiguousarray(f)) for i, f in enumerate(frames)], {"width": w, "height": h}) (keeping all frames in RAM without writing frames to disk), runs run_ = pipeline.analyze(src), and computes the 7-class histogram hist = [[int((t["cls"] == c).sum()) for c in range(7)] for t in run_.tiles] (tools/gate_checks.py:88-92). Reads want = json.loads(cp.read_text(encoding="utf-8")).get("class_hist"); if hist != want, computes bad = [i for i, (a, b) in enumerate(zip(hist, want or [])) if a != b][:3] and appends ("E_GATE_GOLDEN", f"{d.name}: class histogram differs from expect/classes.json class_hist (frames {bad})") (tools/gate_checks.py:93-96).

### 6.3 Route Budget (`route_budget`, `tools/gate_checks.py:101-129`)

```python
def route_budget(root):
    """navigability: every catalogue/route_queries.tsv query answers an anchor with a code path in the top 3 (<= 2000 B),
    ABSTAIN queries must abstain (rc 3)."""
    qf = root / "catalogue" / "route_queries.tsv"
    if not qf.is_file():
        return [("E_GATE_ROUTE", "catalogue/route_queries.tsv missing")] if (root / "catalogue" / "routes.tsv").is_file() else []
```

1. **Missing File Check**: If `not qf.is_file()`, returns `[("E_GATE_ROUTE", "catalogue/route_queries.tsv missing")]` if `(root / "catalogue" / "routes.tsv").is_file()` is truthy, else `[]` (`tools/gate_checks.py:104-106`).
2. **Router Execution**: Loads `rc_mod = load_tool(root, "route_code")` and iterates `for ln, line in enumerate(qf.read_text(encoding="utf-8").splitlines(), 1):`, skipping lines where `not line.strip() or line.startswith("#")` (`tools/gate_checks.py:107-112`):
   - Splits `cols = line.split("\t")`; if `len(cols) != 2`, appends `("E_GATE_ROUTE", f"route_queries.tsv:{ln} expected 2 columns")` and continues (`tools/gate_checks.py:113-116`).
   - Increments `n += 1`, unpacks `q, want = cols`, and calls `res = rc_mod.route(q, 3, str(root))` (`tools/gate_checks.py:117-119`).
   - **Byte Cap**: If `len(rc_mod.canon(res).encode("utf-8")) > rc_mod.MAX_B`, appends `("E_GATE_ROUTE", f"route_queries.tsv:{ln} answer over {rc_mod.MAX_B} B")` (`tools/gate_checks.py:120-121`).
   - **Abstention vs. Top-3 Hit**:
     - When `want == "ABSTAIN"`: if `not res["abstain"]["flag"]`, appends `("E_GATE_ROUTE", f"route_queries.tsv:{ln} {q!r} should abstain, answered {[r[0] for r in res['r']]}")` (`tools/gate_checks.py:122-124`).
     - Otherwise (`elif not any(r[0] == want and r[1] and r[2] > 0 for r in res["r"][:3]):`): appends `("E_GATE_ROUTE", f"route_queries.tsv:{ln} {q!r}: {want} (with path) not in top-3 {[r[0] for r in res['r'][:3]]}")` (`tools/gate_checks.py:125-126`).
3. **Minimum Query Floor**: If `n < 20`, appends `("E_GATE_ROUTE", f"route_queries.tsv has {n} queries < 20")` (`tools/gate_checks.py:127-128`).

### 6.4 Hint Neutrality (`hint_neutral`, `tools/gate_checks.py:132-134`)

```python
def hint_neutral(root):
    """NA until the hint path exists in W3+ pipelines with a hint-neutral fixture; reported in the receipt as NA."""
    return []
```

- `hint_neutral(root)` returns `[]` (`tools/gate_checks.py:132-134`).
- Because `NA_GATES = ("g_hint_neutral",)`, `receipt` appends `" NA:" + na` (NA:g_hint_neutral) whenever `g_hint_neutral` is in `res` and its finding list is empty (`tools/gates.py:641-665`). Active hint-neutrality verification logic is ABSENT.

### 6.5 Sealed Trees (`sealed_untouched`, `tools/gate_checks.py:142-200`)

```python
def sealed_untouched(root):
    """STORAGE LAW §4: visioncore/ and vmem/ are byte-identical to tag ve-sealed-1 (tracked diff + untracked files). NA outside a git checkout."""
    if not (root / ".git").exists():
        return []
```

1. **Git Checkout Guard**: Returns `[]` if `not (root / ".git").exists()` (`tools/gate_checks.py:144-145`).
2. **Tag Verification**: Checks `_git(root, "rev-parse", "--verify", "-q", "ve-sealed-1^{commit}").returncode != 0`; if non-zero, returns `[("E_GATE_SEALED", "tag ve-sealed-1 not found")]` (`tools/gate_checks.py:146-147`).
3. **Diff and Untracked Collection**:
   - Runs `d = _git(root, "diff", "--name-only", "ve-sealed-1", "--", "visioncore", "vmem")` and `u = _git(root, "ls-files", "--others", "--exclude-standard", "--", "visioncore", "vmem")` (`tools/gate_checks.py:148-149`).
   - If `d.returncode or u.returncode`, returns `[("E_GATE_SEALED", "git failed: " + (d.stderr or u.stderr)[-200:])]` (`tools/gate_checks.py:150-151`).
   - Sets `changed = d.stdout.splitlines()` and `untracked = [n for n in u.stdout.splitlines() if "__pycache__" not in n]` (`tools/gate_checks.py:152-153`).
4. **RT0 Equivalence Receipt Validation (`RT0D01`, `RT0D02`)**:
   - Iterates `for card_id, directory in (("RT0D01", "rt0"), ("RT0D02", "rt0d02")):` and checks `receipt_path = root / "eval/results" / directory / "EQUIV_RECEIPT.json"` (`tools/gate_checks.py:157-160`).
   - When receipt_path.exists() is true, verifies (`tools/gate_checks.py:163-198`):
     - `receipt["schema"] != "vqa.rt0.equiv/v1" or receipt["card_id"] != card_id` raises `ValueError("receipt identity")` (`tools/gate_checks.py:165-166`).
     - `receipt["proof_table"] != proof_path` (where `proof_path = "eval/results/" + directory + "/PROOF_SHA_TABLE.json"`) raises `ValueError("proof table path")` (`tools/gate_checks.py:167-169`).
     - `receipt["proof_table_sha256"] != proof_sha or proof.get("verdict") != "PASS"` raises `ValueError("proof table hash/verdict")` (`tools/gate_checks.py:170-174`).
     - `not isinstance(entries, list) or not entries` (where `entries = receipt["files"]`) raises `ValueError("empty file list")` (`tools/gate_checks.py:175-177`).
     - Each `entry` in `entries` must have a string `name = entry["path"]` not already in `allowed` or `card_allowed`, starting with `("visioncore/", "vmem/")`, resolving to `root.resolve() / name` with no `".."` in `Path(name).parts`, satisfying `path.is_file()` and not path.is_symlink(), matching entry["card_id"] == card_id and entry["proof_table_sha256"] == proof_sha, matching `old_sha256` against git -C <root> show ve-sealed-1:<name>, and matching `new_sha256` against `hashlib.sha256(path.read_bytes()).hexdigest()` (`tools/gate_checks.py:179-195`).
     - Any `(OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError)` appends `("E_GATE_SEALED", "invalid " + card_id + " receipt: " + str(exc)[:200])` to `errors` (`tools/gate_checks.py:197-198`).
5. **Return Value**:
   - Returns `errors`, plus `("E_GATE_SEALED", f"{n} differs from ve-sealed-1")` for each `n in changed if n not in allowed`, plus `("E_GATE_SEALED", f"{n} untracked in sealed tree")` for each `n in untracked` (`tools/gate_checks.py:199-200`).

### 6.6 Disk Frames (`disk_frames`, `tools/gate_checks.py:203-245`)

```python
def disk_frames(root):
    """STORAGE LAW §4: the engine smoke (eval/x1_determinism, 1 repeat) leaves no frame-like file in build/ newer than a stamp,
    /dev/shm/vqa-ramws/engine/ empty and 0 NEW Blender/Godot procs (pre-existing ones, e.g. an operator editor, are not ours). NA in trees without eval/ (mutant bases)."""
```

1. **Exemption Guard**: Returns `[]` if `not (root / "eval" / "x1_determinism.py").is_file()` (`tools/gate_checks.py:222-223`).
2. **Pre-Run Stamp and Process Baseline**:
   - Loads `ram = load_py(root / "vadapt" / "ramws.py", "_gate_ramws")` (`tools/gate_checks.py:224`).
   - Creates `out_dir = Path(tempfile.mkdtemp(prefix="g", dir=build / "gate_disk"))` under `build = root / "build"`, writes `stamp = out_dir / ".stamp"`, records `since = stamp.stat().st_mtime_ns`, and captures `before = set(_live_engines())` (`tools/gate_checks.py:225-232`).
3. **Engine Smoke Run**:
   - Executes `[sys.executable, "-B", "-m", "eval.x1_determinism", "--out", str(out_dir / "o"), "--repeats", "1"]` with `cwd=str(root)`, `timeout=280`, and `PYTHONDONTWRITEBYTECODE="1"` (`tools/gate_checks.py:233-234`).
4. **Post-Run Checks**:
   - If `r.returncode != 0`, appends `("E_GATE_DISK", f"smoke rc {r.returncode}: {r.stderr[-200:]}")` (`tools/gate_checks.py:236-237`).
   - Appends `("E_GATE_DISK", f"frame-like file outside RAM workspace: build/{n}")` for each `n in ram.check_disk(build, since=since)` (`tools/gate_checks.py:238`).
   - Checks `left = Path(ram.ROOT) / "engine"`; if `left.is_dir() and any(left.iterdir())`, appends `("E_GATE_DISK", f"{left} not empty")` (`tools/gate_checks.py:239-241`).
   - Appends `("E_GATE_DISK", f"engine process alive {n}")` for each `n in sorted(set(_live_engines()) - before)` (`tools/gate_checks.py:242`).
5. **Cleanup**: Removes `out_dir` via `shutil.rmtree(out_dir, ignore_errors=True)` in `finally` (`tools/gate_checks.py:244-245`).

## 7. Undefined or Absent Behaviours (ABSENT)

The following behaviours are undefined by the bound source files and are therefore marked ABSENT:

- **CLI Mode Mutual-Exclusion Validation**: Rejecting simultaneous `--all`, `--gate`, and `--mutants` flags is ABSENT; `while a:` overwrites `mode` sequentially (`tools/gates.py:731-740`).
- **Active Hint-Neutrality Check**: Any check inside `hint_neutral(root)` beyond returning `[]` is ABSENT (`tools/gate_checks.py:132-134`).
- **Parallel Gate Execution**: Concurrent execution in `run_gates` (tools/gates.py:650-659) or run_mutants (tools/gates.py:675-718) is ABSENT.
- **Non-`/proc` Engine Process Discovery**: Process discovery in `_live_engines()` outside `/proc` (`Path("/proc").iterdir()`) is ABSENT (`tools/gate_checks.py:203-213`).
- **Aliased Import Attribute Tracking**: Tracking aliased module names in g_nofloat (tools/gates.py:436-445) or g_pure (tools/gates.py:603-604) is ABSENT.
