# Agent quickstart: the implemented CLI

## Source scope

This guide describes only `visioncore/cli.py`, `visioncore/errors.py`, `visioncore/pipeline.py`, and `AGENTS.md`, read at `bridge/integration`, resolved to commit `2e6a2ccaa193bf1f4b1d4a7968c3d08c9e3b10e5`. All `file:line` references below refer to that snapshot. `ABSENT` means that a definition or guarantee cannot be established from those four files; it does not assert that the rest of the repository lacks it.

| Bound source | Snapshot |
|---|---|
| `visioncore/cli.py` | [Source](https://github.com/oguirado/vision-engine/blob/2e6a2ccaa193bf1f4b1d4a7968c3d08c9e3b10e5/visioncore/cli.py) |
| `visioncore/errors.py` | [Source](https://github.com/oguirado/vision-engine/blob/2e6a2ccaa193bf1f4b1d4a7968c3d08c9e3b10e5/visioncore/errors.py) |
| `visioncore/pipeline.py` | [Source](https://github.com/oguirado/vision-engine/blob/2e6a2ccaa193bf1f4b1d4a7968c3d08c9e3b10e5/visioncore/pipeline.py) |
| `AGENTS.md` | [Source](https://github.com/oguirado/vision-engine/blob/2e6a2ccaa193bf1f4b1d4a7968c3d08c9e3b10e5/AGENTS.md) |

The examples invoke `python3 -m visioncore.cli`, using the module's `__main__` block, which calls `sys.exit(main())`; the CLI docstring calls the interface `vcore`. Installation instructions, a registered `vcore` console entry point, supported Python versions, and dependency versions are **ABSENT** from the bound files. Imports require NumPy and other package modules, whose complete implementations are outside this guide. (`visioncore/cli.py:2-9`, `visioncore/cli.py:200-201`.)

Example paths are illustrative inputs, not claims that files with those names exist. Output blocks marked **schematic** preserve the keys and nesting constructed by the code. An unquoted `<...>` token stands for a complete runtime JSON value, not a literal string to expect. These blocks are source-derived shapes, not observed engine runs. Exact runtime values imported or computed elsewhere are **ABSENT**.

## Choose a command

| Question about a recording or stored run | Command | What the bound code actually does |
|---|---|---|
| Can the built-in analysis scenes produce repeatable output in this environment? | `selftest` | Analyzes four generated scenarios three times each and requires one unique `vision_json()` digest per scenario. This does not inspect your recording. (`visioncore/cli.py:11`, `visioncore/cli.py:167-178`.) |
| I have a frame manifest; how do I create change-analysis artifacts? | `analyze MANIFEST` | Constructs a `ManifestSource`, analyzes it, writes an output directory, and returns its location, frame count, grid, hashes, and stage status. (`visioncore/cli.py:52-65`.) |
| I have an analyzed vision directory; how do I ask the pack stage to package it? | `pack VISION_DIR` | Builds the batch, requires nonempty GOP rows, calls the imported packer, writes its result, and returns sorted result filenames. Packing policy itself is **ABSENT**. (`visioncore/cli.py:68-109`.) |
| Does stored judge output verify against a pack and a vision directory? | `cite-check VISION_DIR --pack PACK_JSON --judge-output JUDGE_JSON` | Calls the imported citation verifier and wraps its verdict. The verifier's actual citation rules are **ABSENT**. (`visioncore/cli.py:112-127`.) |
| My caller uses the verb `verify-citations`; is it a separate verifier? | `verify-citations VISION_DIR --pack PACK_JSON --judge-output JUDGE_JSON` | Dispatches to exactly the same handler as `cite-check`. (`visioncore/cli.py:181-182`.) |
| Can I recheck stored evidence and optionally save a successful verdict outside the run? | `rejudge --from-run VISION_DIR --pack PACK_JSON --judge-output JUDGE_JSON [--out FILE]` | Checks the optional destination, delegates to `cite-check`, and writes the result only when its `rc` equals `0`. (`visioncore/cli.py:130-146`.) |
| Which CLI command captures a screen, decodes an MP4 directly, runs model inference, or answers a free-text visual question? | **ABSENT** | No such verb or argument is registered here. The analysis entry point takes a manifest; `rejudge` explicitly describes stored-output verification without capture or inference. (`visioncore/cli.py:52-62`, `visioncore/cli.py:130-143`, `visioncore/cli.py:181-190`.) |

## Invocation and parser rules

The complete registered verb set is `selftest`, `analyze`, `pack`, `cite-check`, `verify-citations`, and `rejudge`; the last-but-one name is an alias for `cmd_cite_check`. Verb matching is direct dictionary membership, with no normalization. (`visioncore/cli.py:181-190`.)

| Parser rule | Implemented behavior | Source |
|---|---|---|
| CLI argument source | `main(argv=None)` copies `sys.argv[1:]` when `argv` is `None`; otherwise it copies the supplied iterable. This conversion occurs before the `try` block. | `visioncore/cli.py:185-187` |
| No verb / unknown verb | Raises `E_USAGE` with `detail.reason` beginning `verb must be one of ` and the sorted registered names joined by comma-space. | `visioncore/cli.py:14-15`, `visioncore/cli.py:188-189` |
| Option syntax | Every option token beginning `--` must be a permitted name followed by a separate value token. `--name=value` is not split. Unknown names or a final option with no following token produce `E_USAGE`, reason `bad flag <token>`. | `visioncore/cli.py:18-27` |
| Value handling | The next token is consumed literally as the value, even if it starts with `--`; there is no second option check on that token. | `visioncore/cli.py:24-27` |
| Repeated option | The later occurrence overwrites the earlier value in the options dictionary. | `visioncore/cli.py:26` |
| Positionals and ordering | Tokens not beginning `--` become positionals; options can occur among them. The final positional count must match the command's exact count, or `E_USAGE` reports `expected N positional argument(s)`. | `visioncore/cli.py:20-33` |
| Short options, option terminator, boolean switches | No short-option or special `--` terminator handling is defined. A single-dash token is a positional token unless consumed as a value. Every recognized long option consumes a value; no boolean switch is defined. | `visioncore/cli.py:18-33` |
| Help / version flags | **ABSENT** as supported options. `--help` as the first token is an unknown verb; as an option token after a valid verb it is not in any handler's allowed flag set. The same applies to `--version`. | `visioncore/cli.py:53`, `visioncore/cli.py:93`, `visioncore/cli.py:113`, `visioncore/cli.py:132`, `visioncore/cli.py:168`, `visioncore/cli.py:188-190` |

### Stdout, stderr, and exit classes

Handled command results are serialized with `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`, followed by exactly one newline. This sorts keys, removes separator padding, and retains non-ASCII characters. `main` writes the result to stdout and returns `obj["rc"]`; the module entry point passes that result to `sys.exit`. (`visioncore/errors.py:50-52`, `visioncore/cli.py:196-201`.)

| Envelope field | Construction and limits | Source |
|---|---|---|
| `code` | `result_obj` copies its `code` argument. Command success constructors use `"OK"`; citation commands may copy any existing verdict `code` or select their fallback. `result_obj` does not independently validate the field's type. | `visioncore/errors.py:45-47`; `visioncore/cli.py:63`, `visioncore/cli.py:109`, `visioncore/cli.py:127`, `visioncore/cli.py:178` |
| `rc` | `result_obj` uses `CODES[code]` when no `rc` is supplied; otherwise it copies the explicit value. It performs no separate allowed-class or type check. | `visioncore/errors.py:45-47` |
| `detail` | `result_obj` and `VqaError` copy `dict(detail or {})`; `cite-check` directly places the complete returned verdict in `detail`. There is no universal detailed payload schema. | `visioncore/errors.py:34`, `visioncore/errors.py:47`; `visioncore/cli.py:127` |

The declared classes are the integers below. Their human-readable labels appear in `AGENTS.md:37`; the actual tuple is `RC_CLASSES = (0, 2, 3, 4, 5, 6)` at `visioncore/errors.py:20`.

| Declared `rc` | Class label | Source |
|---|---|---|
| `0` | OK | `AGENTS.md:37`, `visioncore/errors.py:7` |
| `2` | usage/schema | `AGENTS.md:37`, `visioncore/errors.py:8-10` |
| `3` | reject | `AGENTS.md:37`, `visioncore/errors.py:11-14` |
| `4` | stale | `AGENTS.md:37`, `visioncore/errors.py:15` |
| `5` | gate | `AGENTS.md:37`, `visioncore/errors.py:16-17` |
| `6` | budget | `AGENTS.md:37`, `visioncore/errors.py:18` |

`VqaError` rejects `rc == 1`, any value outside `RC_CLASSES`, and `rc == 0`. An unknown code without an explicit `rc` raises `ValueError`; an unknown code with an allowed nonzero explicit `rc` is accepted. The allowed-class checks use equality/membership, not an integer-type test. `rc_of(code)` is simply `CODES[code]`. (`visioncore/errors.py:24-35`, `visioncore/errors.py:41-42`.)

When a handler raises `VqaError`, `main` converts it with `to_obj()` and writes the exception text plus a newline to stderr. That text is formed as `"%s: %s" % (code, detail)`, not as a second JSON envelope. (`visioncore/errors.py:34-38`, `visioncore/cli.py:191-193`.)

`KeyError`, `TypeError`, and `ValueError` raised inside the dispatch `try` become `E_SCHEMA` / `rc 2`, with `detail.reason` set to the exception class name, `": "`, and the first `100` characters of `str(e)`. This branch does not write to stderr. (`visioncore/cli.py:194-195`, `visioncore/errors.py:8`.)

**The bound implementation does not establish an all-failures JSON or never-process-exit-1 guarantee.** Imports and argument-list conversion occur outside that `try`, and other exception classes are not caught there. In particular, `_vision_batch` reads `tiles.bin` with a direct `read_bytes()` lacking an `OSError` wrapper. Exact stdout, stderr, and process status for such uncaught failures are **ABSENT** from the wrapper contract; do not reinterpret them as one of its handled JSON results. (`visioncore/cli.py:3-9`, `visioncore/cli.py:68-76`, `visioncore/cli.py:185-201`.)

## Command reference

### `selftest`

| Argument or option | Required/default | Source |
|---|---|---|
| Positionals | Exactly `0`; no positional values accepted. | `visioncore/cli.py:168` |
| Options | None; scenario, size, and repeat count cannot be configured through this verb. | `visioncore/cli.py:168-173` |

Invocation:

```sh
python3 -m visioncore.cli selftest
```

Schematic successful stdout:

```text
{"code":"OK","detail":{"api_version":<API_VERSION>,"digests":{"clean_static":<digest>,"flicker":<digest>,"move":<digest>,"scroll":<digest>}},"rc":0}
```

`code`, `rc`, `detail.api_version`, and `detail.digests` are constructed at `visioncore/cli.py:178`. The digest keys are the four values of `SELFTEST_SCENARIOS` (`visioncore/cli.py:11`); the shown serialization order follows `visioncore/errors.py:52`. The literal API version and exact digest values/algorithm are **ABSENT** because `API_VERSION` and `canon.sha256_obj` are imported (`visioncore/cli.py:8`, `visioncore/cli.py:174`).

Each scenario runs `pipeline.analyze` three times and hashes `run.vision_json()`. A unique-digest count other than `1` raises `E_DETERMINISM` / `rc 5`, with `detail.scenario` equal to the scenario name and `detail.digests` equal to the number of distinct digests. No output directory is passed. (`visioncore/cli.py:170-177`, `visioncore/errors.py:16`, `visioncore/pipeline.py:174`, `visioncore/pipeline.py:247-248`.)

The internal generator defaults to width `64`, height `48`, and `10` frames. The base RGB channels are `((x*3+y)&255, (x+y*5)&255, (x*y)&255)`, converted to `uint8`; each frame is constructed with `fr.Frame(i, i * 33333, i, np.ascontiguousarray(f))`. The imported constructor's validation is **ABSENT**. These are internal constants, not CLI options. (`visioncore/cli.py:149-154`, `visioncore/cli.py:162-164`.)

| Scenario | Generator operation | Source |
|---|---|---|
| `clean_static` | Copies the base image; none of the three modification branches matches. | `visioncore/cli.py:11`, `visioncore/cli.py:155-161` |
| `scroll` | Applies `np.roll(base, 4 * i, axis=1)`. | `visioncore/cli.py:156-157` |
| `flicker` | On odd `i`, replaces `[16:32, 16:32]` with `255 -` that region. | `visioncore/cli.py:158-159` |
| `move` | Sets `[16:32, 4*i:4*i+16]` to `200`. | `visioncore/cli.py:160-161` |

### `analyze`

| Argument or option | Required/default and actual use | Source |
|---|---|---|
| `MANIFEST` | Exactly `1` positional, converted to `Path`; used to construct `pipeline.ManifestSource`. | `visioncore/cli.py:53-54`, `visioncore/cli.py:62` |
| `--out PATH` | Optional; default is `MANIFEST.parent / "vision"`. | `visioncore/cli.py:61` |
| `--hints PATH` | Optional JSONL input. If present, load it directly; otherwise use the manifest's truthy `hints` value relative to the manifest directory only when that path is a file. If neither case applies, pass `None`. | `visioncore/cli.py:56-60` |
| `--profile PATH` | Optional JSON input; passes the decoded value to `P.effective`, or passes `None` when omitted. Actual parameter names, values, profile schema, and numeric defaults are **ABSENT** because that function is imported. | `visioncore/cli.py:8`, `visioncore/cli.py:55` |

Invocation, assuming an existing valid manifest:

```sh
python3 -m visioncore.cli analyze recording/frames.manifest.json --out recording/vision
```

Schematic successful stdout:

```text
{"code":"OK","detail":{"frames_sha256":<run.frames_sha256>,"grid":<run.grid.ident>,"n_frames":<len(run.tiles)>,"out":"recording/vision","params_sha256":<run.params_sha256>,"stages":{"s0-s4":"ok","s5":<"ok" or "absent">}},"rc":0}
```

| Successful field | Actual source | Definition |
|---|---|---|
| `code`, `rc` | `visioncore/cli.py:63` | Literal `"OK"` and `0`. |
| `detail.out` | `visioncore/cli.py:61-63` | `str(out)`. |
| `detail.n_frames` | `visioncore/cli.py:63` | `len(run.tiles)`. |
| `detail.grid` | `visioncore/cli.py:63` | `run.grid.ident`; identifier formatting is **ABSENT** from these files. |
| `detail.params_sha256` | `visioncore/cli.py:64`, `visioncore/pipeline.py:80` | `P.params_sha256(table)` stored on the run; the hash procedure is imported. |
| `detail.frames_sha256` | `visioncore/cli.py:64`, `visioncore/pipeline.py:230`, `visioncore/pipeline.py:244` | `fr.frames_sha256` applied to accumulated frame SHA values; that combination procedure is imported. |
| `detail.stages` | `visioncore/cli.py:65`, `visioncore/pipeline.py:245-246`, `visioncore/pipeline.py:252-264` | `s0-s4` becomes `"ok"`; `s5` becomes `"absent"` on the handled import/missing-encoder paths, otherwise `"ok"` after encoder return. |

The CLI passes source, effective table, hints, and output directory positionally to `pipeline.analyze`; it supplies no worker or CPU arguments. The function defaults those to `workers=1` and `cpus=None`; `--workers` and `--cpus` are **ABSENT** from this CLI. (`visioncore/cli.py:53`, `visioncore/cli.py:62`, `visioncore/pipeline.py:174`.)

The function copies the effective table, compares its key set to `P.PARAMS` using symmetric difference, raises `E_VC_PARAM_UNKNOWN` for the first sorted differing key, then calls imported `P._check`. An empty frame iteration raises `E_VC_FRAME_SCHEMA` with reason `no frames`. (`visioncore/pipeline.py:178-182`, `visioncore/pipeline.py:242-243`.)

Success returns `rc 0` only after the analysis call, including its directory write, returns. Local failure sites include usage/schema errors, I/O errors, rejected output destinations, empty frames, and frame-shape mismatches; imported components may raise additional registered or uncaught failures. The complete imported failure set is **ABSENT**. (`visioncore/cli.py:52-65`, `visioncore/cli.py:191-195`, `visioncore/pipeline.py:64-74`, `visioncore/pipeline.py:118-157`, `visioncore/pipeline.py:206-207`, `visioncore/pipeline.py:242-248`.)

### `pack`

| Argument or option | Required/default and actual use | Source |
|---|---|---|
| `VISION_DIR` | Exactly `1` positional; used by `_vision_batch`. | `visioncore/cli.py:93`, `visioncore/cli.py:98` |
| `--out PATH` | Optional; default `VISION_DIR / "pack" / ("b%d" % batch_no)`. | `visioncore/cli.py:107` |
| `--batch VALUE` | Optional; default `0`; converted with `int` and passed as `batch_no`. Allowed range is **ABSENT**. | `visioncore/cli.py:101` |
| `--cap VALUE` | Optional; when supplied, converted with `int` and passed as `cap_bytes`. When omitted, this keyword is not passed. The packer's numeric default, validation, and budget algorithm are **ABSENT**. | `visioncore/cli.py:102-106` |
| `--shuffle-window VALUE` | Optional; when supplied, converted with `int` and passed as `shuffle_window`. When omitted, this keyword is not passed. Its default, units, range, and algorithm are **ABSENT**. | `visioncore/cli.py:104-106` |
| `--manifest PATH` | Optional; omission passes `None`. When supplied, all frames from `ManifestSource` are placed in a dictionary keyed by `pipeline.frame_number(f)`, and a frame lookup callable is added to the batch. | `visioncore/cli.py:68`, `visioncore/cli.py:86-88`, `visioncore/cli.py:98` |

Invocation, assuming an analyzed directory with nonempty GOP rows:

```sh
python3 -m visioncore.cli pack recording/vision --manifest recording/frames.manifest.json --batch 0
```

Schematic successful stdout:

```text
{"code":"OK","detail":{"files":<sorted(res.files)>,"out":"recording/vision/pack/b0"},"rc":0}
```

`code`, `rc`, `detail.out`, and `detail.files` are constructed at `visioncore/cli.py:109`; the output path is selected at `visioncore/cli.py:107`. The literal filenames, file formats, and contents of `res.files` are **ABSENT** because the result comes from imported `pack.pack` (`visioncore/cli.py:94-108`). In particular, the CLI does not establish a default pack JSON filename.

The batch reads `vision.json`, `frames.jsonl`, `tiles.bin`, and `regions.jsonl`; it reads `gops.jsonl` only if it is a file, otherwise supplies `[]`. `tiles.bin` is interpreted with imported `layout.parse_tile_header`, `change.TILE_DT`, and `layout.TILE_HDR_SIZE`; their binary definitions are **ABSENT**. (`visioncore/cli.py:70-85`.)

| Batch field passed to the packer | Construction | Source |
|---|---|---|
| `W`, `H`, `params_sha256` | `vision.json` fields `width`, `height`, `params_sha256`. | `visioncore/cli.py:83` |
| `tiles` | Lookup by frame number via rows' `frame` values; returns the matching tile row. Unknown frame raises `E_VC_ID_RANGE` with `detail.frame`. | `visioncore/cli.py:75-81`, `visioncore/cli.py:83` |
| `regions` | Dictionary keyed by each region row's `id`, containing the row itself. | `visioncore/cli.py:84` |
| `gops` | Decoded nonempty JSONL lines, or `[]` when `gops.jsonl` is not a file. | `visioncore/cli.py:49`, `visioncore/cli.py:85` |
| `frames` | Present only with `--manifest`; callable returning the RGB array for a frame-number key. | `visioncore/cli.py:86-88`; numbering: `visioncore/pipeline.py:20-22` |

An import failure raises `E_USAGE` / `rc 2`, reason `pack stage unavailable`. A falsey `batch["gops"]` raises `E_USAGE` / `rc 2`, reason `vision dir has no gops.jsonl (S5 absent)`; that message is also used when the file exists but yields no rows. (`visioncore/cli.py:94-100`, `visioncore/errors.py:8`.)

The integer conversions can raise `ValueError`, handled by `main` as `E_SCHEMA` / `rc 2`. The call to `pack.pack` occurs before `res.write`; its source comment says `E_BUDGET` / `rc 6` propagates before anything is written. Actual budget thresholds, the imported writer's overwrite policy, and a universal no-side-effect guarantee are **ABSENT**. (`visioncore/cli.py:101-108`, `visioncore/cli.py:194-195`, `visioncore/errors.py:8`, `visioncore/errors.py:18`.)

### `cite-check`

| Argument or option | Required/default and actual use | Source |
|---|---|---|
| `VISION_DIR` | Exactly `1` positional; passed to `cite.verify_dir`. | `visioncore/cli.py:113`, `visioncore/cli.py:123` |
| `--pack PATH` | Required; JSON input loaded through `_load_json`. No default. | `visioncore/cli.py:114-115`, `visioncore/cli.py:120` |
| `--judge-output PATH` | Required; JSON input passed to `cite.verify_dir`. No default. | `visioncore/cli.py:114-115`, `visioncore/cli.py:123` |

Invocation with illustrative caller-supplied JSON filenames:

```sh
python3 -m visioncore.cli cite-check recording/vision --pack evidence/pack.json --judge-output evidence/judge-output.json
```

Schematic stdout for a returned verdict:

```text
{"code":<verdict code or fallback>,"detail":<complete verdict>,"rc":<verdict rc or 0>}
```

| Returned field | Actual rule | Source |
|---|---|---|
| `rc` | `verdict.get("rc", 0)`. If it is outside `errors.RC_CLASSES` or equals `1`, raise `E_USAGE` with reason `verifier returned rc %r` instead. No separate integer-type check. | `visioncore/cli.py:124-126`, `visioncore/errors.py:20` |
| `code` | `verdict.get("code", "OK" if rc == 0 else "E_CITE")`. A present value is preserved; there is no registry validation of that value here. | `visioncore/cli.py:127` |
| `detail` | The entire `verdict`, unchanged by this return expression; its additional fields are **ABSENT** from the bound source. | `visioncore/cli.py:123`, `visioncore/cli.py:127` |

If loaded pack data lacks `api_version`, the handler inserts the value from `VISION_DIR / "vision.json"`; an existing pack value is preserved. This is a specific fallback, not validation of the rest of the pack schema. (`visioncore/cli.py:120-123`.)

Missing required flags and an unavailable `cite` import produce `E_USAGE` / `rc 2`; JSON-loading or schema failures also follow the common exception rules. A normally returned verifier verdict can carry any declared class `0`, `2`, `3`, `4`, `5`, or `6`; it is not forced to `0` or `3`. Citation syntax, validation rules, the full pack/judge-output/verdict schemas, and actual verdict values are **ABSENT** because `cite.verify_dir` is imported. (`visioncore/cli.py:113-127`, `visioncore/cli.py:36-41`, `visioncore/cli.py:191-195`, `visioncore/errors.py:8`, `visioncore/errors.py:20`.)

The handler itself contains no file-write call. Whether the imported verifier performs other side effects is **ABSENT**, so this source alone does not establish a complete side-effect-free guarantee. (`visioncore/cli.py:112-127`.)

### `verify-citations`

This verb maps directly to `cmd_cite_check`; there is no second implementation. It has exactly `1` positional `VISION_DIR`, required `--pack PATH` and `--judge-output PATH`, no defaults for those required options, and the same output/failure rules as `cite-check`. (`visioncore/cli.py:113-127`, `visioncore/cli.py:181-182`.)

Invocation:

```sh
python3 -m visioncore.cli verify-citations recording/vision --pack evidence/pack.json --judge-output evidence/judge-output.json
```

Schematic stdout, using the identical return expression:

```text
{"code":<verdict code or fallback>,"detail":<complete verdict>,"rc":<verdict rc or 0>}
```

Each envelope field is defined at `visioncore/cli.py:124-127`; the alias is registered at `visioncore/cli.py:182`. No additional arguments, output fields, or exit classes are introduced by the alias.

### `rejudge`

| Argument or option | Required/default and actual use | Source |
|---|---|---|
| Positionals | Exactly `0`. | `visioncore/cli.py:132` |
| `--from-run PATH` | Required; stored vision directory passed as the `cite-check` positional. No default. | `visioncore/cli.py:133-134`, `visioncore/cli.py:143` |
| `--pack PATH` | Required; passed to `cite-check`. No default. | `visioncore/cli.py:133-134`, `visioncore/cli.py:143` |
| `--judge-output PATH` | Required; passed to `cite-check`. No default. | `visioncore/cli.py:133-134`, `visioncore/cli.py:143` |
| `--out PATH` | Optional; default `None`, so no result-file write is requested. If supplied, treated as a file destination and checked before verification. | `visioncore/cli.py:135-145` |

Invocation, assuming the output and its `.tmp` sibling do not exist and the output is outside the stored run:

```sh
python3 -m visioncore.cli rejudge --from-run recording/vision --pack evidence/pack.json --judge-output evidence/judge-output.json --out review/rechecked.json
```

Schematic stdout:

```text
{"code":<verdict code or fallback>,"detail":<complete verdict>,"rc":<verdict rc or 0>}
```

There is no extra `out` field: `rejudge` returns the `cite-check` result directly. Those fields are constructed at `visioncore/cli.py:124-127`; delegation and return are at `visioncore/cli.py:143-146`.

When `--out` is supplied, the code first builds a sibling name by appending `.tmp` to the destination name. If either the destination or that sibling exists or is a symlink, it raises `E_VC_OUTPUT_EXISTS` / `rc 3`, with `detail.path` equal to the destination string. This also refuses an existing empty directory at that name. Next it resolves the stored run path and rejects an output whose resolved path is relative to that run, with `E_USAGE` / `rc 2` and reason `--out must be outside the stored run`. (`visioncore/cli.py:135-142`, `visioncore/errors.py:8`, `visioncore/errors.py:12`.)

Only after those checks does it verify. It calls `canon.write_atomic(out, canon.dumps(result))` only if `out` is not `None` and the returned `rc` equals `0`; otherwise this wrapper does not call the writer. The saved representation uses imported `canon.dumps`, whereas stdout uses `errors.result_line`; their byte-for-byte equivalence, atomic-writer internals, and parent-directory creation behavior are **ABSENT** from the bound files. (`visioncore/cli.py:143-146`, `visioncore/cli.py:196`, `visioncore/errors.py:50-52`.)

The default operation re-verifies stored output; it does not contain a call for inference or capture. Success is not guaranteed: all verifier return classes and common failure handling apply in addition to the prechecks above. (`visioncore/cli.py:130-146`, `visioncore/cli.py:191-197`.)

## Analysis inputs and files that the CLI exposes

### JSON and frame-manifest reads

`_load_json` reads UTF-8 text and calls imported `canon.loads`. `_load_jsonl` reads UTF-8 text, splits it with `splitlines()`, skips only falsey lines, and calls `canon.loads` for every remaining line; a whitespace-only nonempty line is not skipped here. Each file-read `OSError` is wrapped as `E_IO` with `detail.path = str(path)` and `detail.reason = str(e)[:80]`. JSON syntax rules beyond these calls are **ABSENT**. (`visioncore/cli.py:36-49`.)

`ManifestSource` obtains metadata and frame rows through imported `fr.read_manifest`; it excludes `frames` from `meta` and retains `frames` as `_rows`. Its docstring names `vqa.frame/v1`, `png_dir`, and `rgb24_raw`. Actual manifest validation and a complete manifest schema are **ABSENT** because `fr.read_manifest` is outside the bound files. (`visioncore/pipeline.py:45-56`.)

| Field consumed by `ManifestSource` | Visible use | Source |
|---|---|---|
| Manifest `frames` | Supplies iteration rows; `__len__` returns their count. | `visioncore/pipeline.py:50-59` |
| Metadata `storage` | Exact equality to `"rgb24_raw"` selects the raw-array branch. Other validation is delegated. | `visioncore/pipeline.py:54-56` |
| Metadata `raw_path`, `width`, `height`, `count` | Passed to `decode.rgb24_raw`, with the raw path relative to the manifest's parent. | `visioncore/pipeline.py:55-56` |
| Metadata `width`, `height` | Establish `(H, W, 3)` for the RGB-shape comparison. | `visioncore/pipeline.py:62`, `visioncore/pipeline.py:72-73` |
| Row `i` | Indexes the raw array when present; is passed to `fr.Frame`; identifies size errors. | `visioncore/pipeline.py:65`, `visioncore/pipeline.py:73-74` |
| Row `file` | In the non-raw branch, joined to the manifest parent and read as bytes for `decode.png`. | `visioncore/pipeline.py:67-69` |
| Row `t_us` | Passed to `fr.Frame`. Its validation and time interpretation are **ABSENT** here. | `visioncore/pipeline.py:74` |
| Row `render_index` | Optional `.get` result passed to `fr.Frame`; absent becomes `None`. | `visioncore/pipeline.py:74` |
| Row `sha256` | Passed to `fr.Frame`; validation/computation by that class is **ABSENT** here. | `visioncore/pipeline.py:74` |

Raw frames are converted with `np.ascontiguousarray`; the PNG branch wraps file-read `OSError` as `E_IO`. After either branch, a decoded shape other than `(H, W, 3)` is rejected with `E_VC_FRAME_SIZE`, including `i` and a list-valued `shape` in `detail`. Raw decoder and PNG decoder validation are **ABSENT**. (`visioncore/pipeline.py:64-74`.)

`frame_number(f)` returns `f.i` only when `f.render_index is None`, otherwise `f.render_index`. Analysis output frame numbers, pack-manifest RGB keys, and hint matching therefore need care: output numbering uses this helper, while hints are grouped by `h["i"]` and applied using `f.i`. (`visioncore/pipeline.py:20-22`, `visioncore/pipeline.py:187-188`, `visioncore/pipeline.py:205`, `visioncore/pipeline.py:225`; `visioncore/cli.py:87`.)

### Hints

The bound hint logic consumes `i`, `role`, and `bbox_px`. `i` selects the frame group. Only roles `ui` and `actor` map to bits, respectively `1 << 4` (`16`) and `1 << 5` (`32`); other or absent roles are skipped. `bbox_px` is unpacked as `x, y, w, h`; nonpositive width or height is skipped. Tiles whose grid cells overlap the box have the selected bit ORed into their `flags`. No other semantic effect or complete hint schema is established here. (`visioncore/pipeline.py:17`, `visioncore/pipeline.py:160-171`, `visioncore/pipeline.py:187-188`, `visioncore/pipeline.py:225`.)

### Written artifacts

The base `FILES` tuple is `tiles.bin`, `frames.jsonl`, `regions.jsonl`, `links.jsonl`, `vision.json`. Actual blobs are assembled by `_blobs`, which also incorporates `gop_files`; it is not limited to the tuple. (`visioncore/pipeline.py:16`, `visioncore/pipeline.py:105-116`.)

| File | Construction visible in the bound code | Source |
|---|---|---|
| `tiles.bin` | Imported tile header plus each tile array's `tobytes()`. Header layout, tile dtype, grid constants, and endianness are **ABSENT**. | `visioncore/pipeline.py:107-109` |
| `frames.jsonl` | Concatenation of `canon.dumps` for `run.frame_rows`, encoded to bytes. Fields are listed below. | `visioncore/pipeline.py:110`, `visioncore/pipeline.py:238-240` |
| `regions.jsonl` | Concatenation of `canon.dumps` for each region returned by imported `rg.regions_of`. Full row schema is **ABSENT**. | `visioncore/pipeline.py:111`, `visioncore/pipeline.py:226`, `visioncore/pipeline.py:235` |
| `links.jsonl` | Concatenation of serialized link objects. With no previous region set, the object has `schema="vqa.core-link/v1"`, current `frame`, and empty `links`, `split`, and `merge` lists. Later results come from imported `rg.match_regions`; their full schema is **ABSENT**. | `visioncore/pipeline.py:112`, `visioncore/pipeline.py:228-229`, `visioncore/pipeline.py:236` |
| `vision.json` | `canon.dumps(run.vision_json()).encode()`. Its visible fields are listed below; exact `canon.dumps` byte formatting is **ABSENT**. | `visioncore/pipeline.py:93-97`, `visioncore/pipeline.py:114-115` |
| `gops.bin` | Added after the S5 encoder path returns: concatenates each `g.blob`, starting with `b""`. | `visioncore/pipeline.py:263-271` |
| `gops.jsonl` | Added on the same path: serializes `gop.gop_record(g, grid.ident, params_sha256, off)` for each GOP; offset starts at `0` and increases by the blob's byte length. Row schema is **ABSENT**. Empty encoder output still produces empty GOP files with `s5="ok"`. | `visioncore/pipeline.py:263-272` |

`vision_json()` fields:

| Field | Value source | Source |
|---|---|---|
| `schema` | Literal `"vqa.vision/v1"`. | `visioncore/pipeline.py:94` |
| `api_version` | Imported `API_VERSION`; literal value **ABSENT**. | `visioncore/pipeline.py:12`, `visioncore/pipeline.py:94` |
| `params_sha256` | `self.params_sha256`, initialized with `P.params_sha256(table)`. | `visioncore/pipeline.py:80`, `visioncore/pipeline.py:94` |
| `frames_sha256` | `self.frames_sha256`, populated after frame iteration with `fr.frames_sha256(shas)`. | `visioncore/pipeline.py:95`, `visioncore/pipeline.py:244` |
| `grid` | `self.grid.ident`; format **ABSENT**. | `visioncore/pipeline.py:95` |
| `width`, `height` | `self.grid.W`, `self.grid.H`. | `visioncore/pipeline.py:95-96` |
| `n_frames` | `len(self.tiles)`. | `visioncore/pipeline.py:96` |
| `stages` | A dictionary of sorted stage items. Visible assignments are `s0-s4="ok"` and `s5="ok"` or `"absent"`. | `visioncore/pipeline.py:96`, `visioncore/pipeline.py:245`, `visioncore/pipeline.py:257`, `visioncore/pipeline.py:261`, `visioncore/pipeline.py:264` |
| `files` | Sorted mapping of each non-vision blob filename to `canon.sha256_bytes(blob)`; `vision.json` is excluded through `_blobs(with_vision=False)`. Hash function implementation **ABSENT**. | `visioncore/pipeline.py:97`, `visioncore/pipeline.py:102-115` |

Each `frames.jsonl` row is constructed as follows:

| Field | Value source | Source |
|---|---|---|
| `schema` | Literal `"vqa.core-frame/v1"`. | `visioncore/pipeline.py:238` |
| `frame` | `frame_number(f)`. | `visioncore/pipeline.py:20-22`, `visioncore/pipeline.py:205`, `visioncore/pipeline.py:238` |
| `grid` | `grid.ident`; format **ABSENT**. | `visioncore/pipeline.py:238` |
| `n` | `summ["n"]` from imported classification; internal shape and semantics **ABSENT**. | `visioncore/pipeline.py:219-221`, `visioncore/pipeline.py:238` |
| `cov_ppm` | `summ["cov_ppm"]`; computation and internal shape **ABSENT**. | `visioncore/pipeline.py:219-221`, `visioncore/pipeline.py:239` |
| `global_mv` | `summ["global_mv"]` directly, including any `None`; the separately stored run motion uses a `(0, 0)` fallback. Detailed motion semantics **ABSENT**. | `visioncore/pipeline.py:227`, `visioncore/pipeline.py:237`, `visioncore/pipeline.py:239` |
| `regions` | `len(regs)`, where `regs = rg.regions_of(...)`. | `visioncore/pipeline.py:226`, `visioncore/pipeline.py:239` |
| `tiles_sha256` | `hashlib.sha256(out.tobytes()).hexdigest()`. | `visioncore/pipeline.py:240` |

### Destination checks and optional S5

`VisionRun.write` accepts a destination that is absent or an empty directory. `_output_occupied` rejects a symlink, any existing non-directory, and a nonempty directory. It checks before building/writing the staging output and again before publication; those refusals raise `E_VC_OUTPUT_EXISTS` with `detail.path`. (`visioncore/pipeline.py:118-123`, `visioncore/pipeline.py:136-138`, `visioncore/pipeline.py:152-157`.)

The writer creates missing parent directories, creates a temporary sibling directory with prefix `"." + out_dir.name + "."`, writes each blob using imported `canon.write_atomic`, then publishes via `os.rename(stage, out_dir)`. It wraps `OSError` from its `try` as `E_IO` with a reason truncated to at most `80` characters. The `finally` block removes an outstanding stage and attempts to remove created parent directories under its stated conditions. There is no proof here of rollback for every possible cleanup failure or concurrent change. (`visioncore/pipeline.py:124-149`.)

`_run_gop` sets `s5="absent"` if importing `gop` raises `ImportError` or `getattr(gop, "encode_run", None)` returns `None`; it then returns without creating GOP blobs. Otherwise it calls the encoder and sets `s5="ok"` after the call returns. Other encoder exceptions are not handled in `_run_gop`. Installed module availability and actual encoder behavior are **ABSENT**. (`visioncore/pipeline.py:252-272`.)

The code also defines shared-memory helpers: `SHM_ROOT` is `/dev/shm`; `shm_run_dir` accepts only a string matching `[a-z0-9_]{1,48}` and returns `root / ("vqa_" + run_id)`. `open_shm_run` uses `frames.manifest.json` and requires metadata storage `rgb24_raw`. The docstring names raw storage `frames.rgb24`. These helpers are not registered CLI subcommands or flags; the CLI requires a manifest path. (`visioncore/pipeline.py:25-42`, `visioncore/cli.py:53`, `visioncore/cli.py:181-182`.)

## Complete registered error-code inventory

`CODES` is a code-to-class mapping, not a full explanation of every code. The table includes every key defined there. Where the bound executable code does not show a specific meaning or producer, **ABSENT** is intentional; a code name alone is not a behavioral specification. The general class labels come from `AGENTS.md:37`. (`visioncore/errors.py:6-20`.)

| Code | Default `rc` | Specific meaning established within the bound files | Source |
|---|---|---|---|
| `OK` | `0` | Explicit success constructors; also the citation fallback when the verdict has no code and `rc == 0`. | `visioncore/errors.py:7`; `visioncore/cli.py:63`, `visioncore/cli.py:109`, `visioncore/cli.py:127`, `visioncore/cli.py:178` |
| `ANSWERED` | `0` | Specific producer/meaning **ABSENT**; registered in the success class. | `visioncore/errors.py:7` |
| `E_USAGE` | `2` | Bad verb, flags or positional counts; missing required options; unavailable pack/cite stage; no GOP rows; invalid verifier `rc`; `rejudge --out` inside the run. Details use `reason`. | `visioncore/errors.py:8`; `visioncore/cli.py:14-32`, `visioncore/cli.py:97-100`, `visioncore/cli.py:114-126`, `visioncore/cli.py:133-142`, `visioncore/cli.py:188-189` |
| `E_SCHEMA` | `2` | `main` converts `KeyError`, `TypeError`, and `ValueError` from its dispatch `try` to this code; `detail.reason` identifies the exception and truncates its message. Other producers **ABSENT**. | `visioncore/errors.py:8`; `visioncore/cli.py:194-195` |
| `E_GRID` | `2` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:8` |
| `E_IO` | `2` | Wrapped `OSError` at JSON/JSONL reads, PNG reads, destination occupancy checks, and the analysis directory-write `try`; details carry `path` and `reason` truncated to at most `80` characters. Not every raw I/O call is wrapped. | `visioncore/errors.py:8`; `visioncore/cli.py:36-49`, `visioncore/cli.py:73`; `visioncore/pipeline.py:67-71`, `visioncore/pipeline.py:140-141`, `visioncore/pipeline.py:152-157` |
| `E_DECODE` | `2` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:8` |
| `E_FLOAT` | `2` | Specific meaning/producer **ABSENT**; the registry alone supplies the class. | `visioncore/errors.py:8` |
| `E_VC_FRAME_SCHEMA` | `2` | Invalid shared-memory run id (`reason="bad shm run id"`), wrong shared-memory storage (`reason="shm run must use rgb24_raw storage"`), or no analyzed frames (`reason="no frames"`). The shared-memory helpers are not CLI verbs. | `visioncore/errors.py:9`; `visioncore/pipeline.py:32-41`, `visioncore/pipeline.py:242-243`; `visioncore/cli.py:181-182` |
| `E_VC_FRAME_SHAPE` | `2` | Specific meaning/producer **ABSENT**; visible shape checks instead raise `E_VC_FRAME_SIZE`. | `visioncore/errors.py:9`; `visioncore/pipeline.py:72-73`, `visioncore/pipeline.py:206-207` |
| `E_VC_FORMAT` | `2` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:9` |
| `E_VC_PARAM_UNKNOWN` | `2` | A differing parameter-key set: the symmetric difference with `P.PARAMS` is nonempty; `detail.param` is the first sorted difference, which can represent an extra or missing key. | `visioncore/errors.py:10`; `visioncore/pipeline.py:178-182` |
| `E_VC_PARAM_RANGE` | `2` | Specific meaning, accepted ranges, and producer **ABSENT**; `_check` is imported. | `visioncore/errors.py:10`; `visioncore/pipeline.py:182` |
| `E_CITE` | `3` | Citation wrapper fallback code when a returned verdict lacks `code` and has nonzero `rc`. The wrapper preserves that verdict's allowed `rc`, so a fallback `E_CITE` envelope is not necessarily `rc 3`. Actual citation-rejection rules **ABSENT**. | `visioncore/errors.py:11`; `visioncore/cli.py:124-127` |
| `NOT_IN_VISUAL_MEMORY` | `3` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:11` |
| `E_ANCHOR` | `3` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:11` |
| `E_ID` | `3` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:11` |
| `E_VC_OUTPUT_EXISTS` | `3` | Occupied analysis destination, or an existing/symlink `rejudge` destination or `.tmp` sibling. `detail.path` is the requested destination. | `visioncore/errors.py:12`; `visioncore/pipeline.py:121-122`, `visioncore/pipeline.py:136-137`, `visioncore/pipeline.py:152-155`; `visioncore/cli.py:135-139` |
| `E_VC_ID_SYNTAX` | `3` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:13` |
| `E_VC_ID_RANGE` | `3` | Pack batch tile lookup requested a frame absent from `row_of`; detail contains that `frame`. Other producers **ABSENT**. | `visioncore/errors.py:13`; `visioncore/cli.py:75-81` |
| `E_VC_FRAME_SHA` | `3` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:13` |
| `E_VC_FRAME_SIZE` | `3` | RGB shape differs from `(H, W, 3)`. Manifest iteration reports `i` and `shape`; analysis checks report `i`. | `visioncore/errors.py:13`; `visioncore/pipeline.py:62-74`, `visioncore/pipeline.py:199-201`, `visioncore/pipeline.py:206-207` |
| `E_VC_PNG` | `3` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:14` |
| `E_NAV_ANCHOR` | `3` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:14` |
| `E_NAV_ABSTAIN` | `3` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:14` |
| `E_STALE` | `4` | Specific meaning/producer **ABSENT**; class label is stale. | `visioncore/errors.py:15`, `AGENTS.md:37` |
| `E_DRIFT` | `4` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:15` |
| `E_VQA_PIN` | `4` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:15` |
| `E_VC_GRID_MISMATCH` | `4` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:15` |
| `E_GATE` | `5` | Specific meaning/producer **ABSENT**; class label is gate. | `visioncore/errors.py:16`, `AGENTS.md:37` |
| `E_MUTANT_SURVIVED` | `5` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:16` |
| `E_DETERMINISM` | `5` | A selftest scenario yields a unique-digest count other than `1`; detail contains `scenario` and the distinct `digests` count. | `visioncore/errors.py:16`; `visioncore/cli.py:170-176` |
| `E_GATE_LAYER_UP` | `5` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:16` |
| `E_GATE_LAYER_PKG` | `5` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:17` |
| `E_VC_GOLDEN` | `5` | Specific meaning/producer **ABSENT**. | `visioncore/errors.py:17` |
| `E_BUDGET` | `6` | Budget class; a comment at the packer call states that this error propagates before writing. Actual budget conditions and limits **ABSENT**. | `visioncore/errors.py:18`, `AGENTS.md:37`; `visioncore/cli.py:106-108` |

For example, calling the module with no verb has this source-derived stdout line, followed by the newline specified by `result_line`:

```json
{"code":"E_USAGE","detail":{"reason":"verb must be one of analyze, cite-check, pack, rejudge, selftest, verify-citations"},"rc":2}
```

The exact reason is assembled at `visioncore/cli.py:188-189` from `visioncore/cli.py:181-182`; `E_USAGE` maps to `2` at `visioncore/errors.py:8`; the envelope and serialization are defined at `visioncore/errors.py:45-52`. This is a derivation, not a claim that an engine process was run.

## ABSENT and source limitations

| Item | What cannot be established from the bound source | Boundary |
|---|---|---|
| Runtime readiness | Installation steps, console-script registration, supported versions, complete dependencies, and whether optional stages are installed. | `visioncore/cli.py:2-9`, `visioncore/cli.py:94-97`, `visioncore/cli.py:116-119`; `visioncore/pipeline.py:254-262` |
| Imported parameter contract | Parameter names/default values, accepted profile shape, ranges, and hashing implementation. | `visioncore/cli.py:55`; `visioncore/pipeline.py:80`, `visioncore/pipeline.py:178-182` |
| Full external formats | Complete manifest, pack, judge-output, verdict, region, later-link, GOP, tile-header, and tile-record schemas. Only fields visibly consumed or built above are established. | `visioncore/cli.py:68-88`, `visioncore/cli.py:120-127`; `visioncore/pipeline.py:45-74`, `visioncore/pipeline.py:107-112`, `visioncore/pipeline.py:226-240`, `visioncore/pipeline.py:263-272` |
| Packing behavior | Default cap/window, selection/shuffle algorithms, emitted filenames, writer overwrite/rollback policy. | `visioncore/cli.py:101-109` |
| Citation correctness | Actual verification rules, proof of correctness, full verdict fields, and model-generated answers. | `visioncore/cli.py:116-127` |
| Serializer equivalence | Whether `canon.dumps` and `errors.result_line` emit identical bytes, including JSONL line separators and saved `rejudge` output. | `visioncore/cli.py:145`, `visioncore/cli.py:196`; `visioncore/errors.py:52`; `visioncore/pipeline.py:110-115` |
| Operational measurements | No engine runs, actual output digests, timing measurements, or acceptance results are asserted by these source-derived examples. | Examples are schematic; runtime calls are at `visioncore/cli.py:62`, `visioncore/cli.py:106`, `visioncore/cli.py:123`, `visioncore/cli.py:173-174`. |

Two repository prose claims must not be substituted for the visible implementation. `AGENTS.md:38` describes a general refusal guarantee with `<out>.tmp` and `os.replace`; `VisionRun.write` actually uses a randomly named staging directory and `os.rename`, with the explicit checks and cleanup above (`visioncore/pipeline.py:118-157`). `AGENTS.md:56` says frames are RAM-only, while `ManifestSource` visibly has a PNG path-reading branch (`visioncore/pipeline.py:64-74`). This guide documents those executable paths and does not infer universal storage-policy enforcement or refusal guarantees.
