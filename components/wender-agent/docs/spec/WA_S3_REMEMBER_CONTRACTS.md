# WA S3 REMEMBER contracts

Source snapshot: `example-owner/wender-agent`, explicit ref `bridge/integration`, observed commit `c0a8df9ec8809f38ab3f6d9d18f776a7cdfb39b9`. All path:line citations below refer to that snapshot. **AUSENTE** identifies an unspecified contract; **PROPOSAL** identifies a suggested change or test, not implemented behavior.

## Contracts

### Scope and public symbols

The program map is an in-memory graph with a JSON dump; its persistence location and merge policy are explicitly AUSENTE. Skill admission validates a record and returns JSON; the skill store itself is AUSENTE. These are separate capabilities, and admission does not perform a reproduction or write a record to a store (`wender/remember/program_map.py:4-6`, `wender/remember/skills.py:4-5`, `wender/remember/skills.py:17-34`).

Anchor locations identify the comment marker; definition locations identify the symbol. The scanner records marker lines, and the convention test checks uniqueness and a minimum anchor count, without enumerating public symbols (`tools/anchors.py:33-36`, `tests/test_repo_conventions.py:32-36`). AUSENTE in the table means there is no individual anchor in the bound source; the class marker is not an invented method marker.

| Public symbol defined in the scoped modules | Definition | Existing anchor ID and marker location |
|---|---|---|
| `state_key` | `wender/remember/program_map.py:11` | AUSENTE |
| `ProgramMap` | `wender/remember/program_map.py:16` | `wa.remember.program_map.ProgramMap` at `wender/remember/program_map.py:15` |
| `ProgramMap.__init__` (construction) | `wender/remember/program_map.py:17` | AUSENTE; containing class marker above |
| `ProgramMap.observe` | `wender/remember/program_map.py:22` | AUSENTE; containing class marker above |
| `ProgramMap.add_edge` | `wender/remember/program_map.py:28` | AUSENTE; containing class marker above |
| `ProgramMap.successors` | `wender/remember/program_map.py:37` | AUSENTE; containing class marker above |
| `ProgramMap.to_json` | `wender/remember/program_map.py:40` | AUSENTE; containing class marker above |
| `REQUIRED` | `wender/remember/skills.py:9` | AUSENTE |
| `SkillRejected` | `wender/remember/skills.py:12` | AUSENTE |
| `admit` | `wender/remember/skills.py:17` | `wa.remember.skills.admit` at `wender/remember/skills.py:16` |
| `SCHEMA_ID` | `wender/state/schema.py:11` | AUSENTE |
| `SOURCE_KINDS` | `wender/state/schema.py:12` | AUSENTE |
| `NODE_KEYS` | `wender/state/schema.py:15` | AUSENTE |
| `TOP_KEYS` | `wender/state/schema.py:16-17` | AUSENTE |
| `StateError` | `wender/state/schema.py:20` | AUSENTE |
| `dumps` | `wender/state/schema.py:25` | `wa.state.schema.dumps` at `wender/state/schema.py:24` |
| `build_state` | `wender/state/schema.py:43-44` | `wa.state.schema.build_state` at `wender/state/schema.py:42` |
| `validate_state` | `wender/state/schema.py:92` | `wa.state.schema.validate_state` at `wender/state/schema.py:91` |

The STATE build/validation functions are listed to distinguish them from serialization: `build_state` calls `validate_state` and raises `StateError` when it receives violations. REMEMBER imports only `dumps`; neither REMEMBER path invokes STATE validation (`wender/state/schema.py:81-84`, `wender/remember/program_map.py:8`, `wender/remember/skills.py:7`, `wender/state/schema.py:25-28`).

### State identity: `state_key(screen_fp, ax_stable_sha)`

The exact construction is `"%s:%s" % (screen_fp or "-", ax_stable_sha or "-")`. Each falsey argument is replaced by the literal hyphen before string interpolation. Truthy arguments are string-formatted, joined with one colon, and returned as a string. There is no hashing, escaping, hash-length check, type check, normalization, or requirement that at least one identity component be present (`wender/remember/program_map.py:11-12`).

Consequently, `state_key(None, None)` yields `"-:-"`; zero, false, and empty strings collapse to the same fallback as missing values. Literal hyphens also collide with the fallback. Embedded colons are not escaped: `("a:b", "c")` and `("a", "b:c")` both yield `"a:b:c"`. These are consequences of the implemented expression, not an assertion that a producer supplies such identities. A permitted input alphabet and collision policy are AUSENTE (`wender/remember/program_map.py:12`).

### `ProgramMap` construction and observation

Construction retains `program` as supplied, creates an empty `nodes` dictionary, and creates an empty `edges` list. No constructor validation or persistent storage is performed (`wender/remember/program_map.py:17-20`).

`observe` computes the state key, uses `setdefault` to create a node containing the original `screen_fp`, original `ax_stable_sha`, and `seen: 0` only when the key is new, increments `seen`, and returns the key. The first observation therefore has `seen == 1`; later observations at that key increment the existing record without replacing either identity field (`wender/remember/program_map.py:22-26`).

Identity collisions therefore preserve whichever raw identity pair was observed first. The node value is not reconstructed from the key, and observations do not verify screen or AX identities. Counter limits, eviction, observation timestamps, collision rejection, synchronization, and persistence are AUSENTE in this implementation (`wender/remember/program_map.py:17-26`).

### `add_edge(pre, action, post, changed=None)`

Endpoints are checked in the order `pre`, then `post`. If an endpoint is not present in `nodes`, the method raises `KeyError("unknown state %s" % k)` before creating or adding an edge. A self-loop is accepted when its single endpoint exists (`wender/remember/program_map.py:28-32`).

The candidate edge is exactly a dictionary with `pre`, `action`, `post`, and `changed`. Deduplication is the expression `e not in self.edges`: it compares the entire candidate dictionary using Python equality, including `changed`, and appends only if no equal edge is present. A different `changed` value can therefore produce another edge for the same endpoints and action. There is no count of repeated edges (`wender/remember/program_map.py:32-34`).

The method always returns the newly constructed candidate `e`. For a new edge, that dictionary is the stored object. For a duplicate, it is equal to an existing dictionary but is not that stored dictionary. `action` and `changed` are retained by reference, and no defensive copy, action-schema check, evidence verification, or eager serialization occurs (`wender/remember/program_map.py:32-35`).

### `successors(key)` and `to_json()`

`successors` creates a new outer list containing stored edge dictionaries whose `pre` equals the supplied key. It preserves their order in `self.edges`, performs no node-existence check, and returns an empty list when nothing matches. The dictionaries are shared with the map, so changing a returned edge changes the stored edge (`wender/remember/program_map.py:37-38`).

`to_json` creates a separate sorted edge list using the tuple `(e["pre"], dumps(e["action"]), e["post"])`. Actions are compared through their canonical JSON text, not by an action-specific semantic order. The method then serializes `schema: "wender.program_map/v0"`, `program`, `nodes`, and that edge list through the shared `dumps`. It does not reorder `self.edges` in place (`wender/remember/program_map.py:40-43`).

`changed` is included in the output edge but omitted from the sorting key. Distinct edges tied on pre/action-JSON/post retain their insertion order in the sorted result. Node dictionary keys and nested object keys are sorted by the final serializer; array order is not canonicalized independently of the edge key (`wender/remember/program_map.py:32-34`, `wender/remember/program_map.py:41-43`, `wender/state/schema.py:28`).

### Skill admission: every explicit rejection

`SkillRejected` subclasses `ValueError`. Required fields are `id`, `intent`, `params`, `steps`, `checkpoints`, `provenance`, and `evidence`, in that order. The following branches run sequentially; the first encountered rejection is raised, rather than a combined list of violations (`wender/remember/skills.py:9-13`, `wender/remember/skills.py:19-33`).

| Order | Rejection condition | Exact `SkillRejected` message or expression | Source |
|---|---|---|---|
| 1 | A required field is absent under membership testing | `"missing %s" % missing`; `missing` is the Python list in `REQUIRED` order | `wender/remember/skills.py:19-21` |
| 2 | `skill["steps"]` is falsey | `no steps` | `wender/remember/skills.py:22-23` |
| 3 | `skill["checkpoints"]` is falsey | `no verification checkpoints` | `wender/remember/skills.py:24-25` |
| 4 | Evidence is not a dictionary, or `reproduced is not True`, or `verified_by` is falsey | `reproduction not verified (evidence.reproduced/verified_by)` | `wender/remember/skills.py:26-28` |
| 5 | `provenance.get("source")` is falsey | `provenance.source required` | `wender/remember/skills.py:29-30` |
| 6 | Any iterated step has a `path` outside the allowed tuple | `step path must be api\|ax\|coordinates` | `wender/remember/skills.py:31-33` |

The evidence check requires the literal boolean `True` for `reproduced`; integer `1` is insufficient. In contrast, `verified_by` and `provenance.source` are checked for truthiness, not a particular nonempty string schema. Each step's path must equal one of `"api"`, `"ax"`, or `"coordinates"`; execution and target validity are not tested (`wender/remember/skills.py:27-33`).

When these checks complete, `admit` returns `dumps(skill)`, preserving additional fields that the serializer supports. There is no whitelist rejecting extra keys, no structural validation of params or checkpoint predicates, no required type/value check for id or intent beyond key presence, and no reproduction/verifier invocation. Detailed skill schemas and independent verification are AUSENTE; the implemented trust boundary is validation of supplied claims (`wender/remember/skills.py:19-34`).

### Shared canonical dump rules

| Rule actually implemented | Consequence and limit | Source |
|---|---|---|
| Run `_no_float` before encoding | Any encountered float value, including a nonfinite float, raises `StateError("float at %s" % path)` | `wender/state/schema.py:25-33` |
| Recurse through dictionary values and list/tuple items | Error paths begin with `$`, append `.key` for dictionary values and `[index]` for sequence items; dictionary keys themselves are not checked | `wender/state/schema.py:31-39` |
| `sort_keys=True` | Supported object keys are sorted; list/tuple element order is retained | `wender/state/schema.py:28` |
| `separators=(",", ":")` | Compact object/array separators with no added formatting whitespace | `wender/state/schema.py:28` |
| `ensure_ascii=False` | Non-ASCII text need not be ASCII-escaped; no Unicode normalization step is performed here | `wender/state/schema.py:25-28` |
| Return `json.dumps` directly | Returns JSON text (`str`), not encoded bytes; appends no newline and performs no file I/O | `wender/state/schema.py:28` |
| No call to `validate_state` | Serialization does not establish a valid STATE, skill schema, image-free payload, or verification receipt | `wender/state/schema.py:25-28`, `wender/state/schema.py:92` |

The float rule is not a ban on boolean, null, or string values: the precheck rejects `float` instances and traverses the listed containers, then delegates remaining encoding to `json.dumps`. Unsupported values, unsortable key mixtures, and cycles have no project-level normalization contract; native errors may escape. A byte encoding step for returned text is AUSENTE in `dumps` itself (`wender/state/schema.py:25-39`).

## Failure modes

Unknown endpoints produce the explicit `KeyError` above; an unhashable endpoint can instead fail at dictionary membership. Invalid actions or changed payloads may be accepted into the map and fail only during `to_json`, when action sort keys and the final graph are serialized. An error in dumping does not undo the previously added edge (`wender/remember/program_map.py:29-43`).

The six explicit admission rejections do not cover arbitrary malformed input. A non-container skill may fail membership testing; nonmapping provenance or a nonmapping step may raise `AttributeError` on `.get`; a noniterable steps value may raise `TypeError`. After admission checks, float values can raise `StateError`, and other JSON failures can propagate from `dumps`. No wrapper converts these failures into `SkillRejected` (`wender/remember/skills.py:19-34`, `wender/state/schema.py:25-39`).

Public `nodes`, `edges`, retained input objects, and returned edge objects allow external mutation to invalidate earlier observations or admission assumptions. Ownership/copying guarantees and concurrent mutation handling are AUSENTE (`wender/remember/program_map.py:18-20`, `wender/remember/program_map.py:24-25`, `wender/remember/program_map.py:32-38`).

## Determinism

For unchanged supported data, JSON object-key insertion order is removed by the serializer. That does not establish history-independent graph bytes: `seen` depends on observation count, collisions keep the first raw identity values, Python-equal actions can deduplicate despite different JSON representations, and edges with identical sort tuples retain insertion order. `successors` deliberately reflects stored edge order rather than export sorting (`wender/state/schema.py:28`, `wender/remember/program_map.py:24-25`, `wender/remember/program_map.py:33-43`).

The first rejected skill condition is determined by code order; the missing-field list follows `REQUIRED`. The first float-value error follows dictionary traversal or sequence order in `_no_float`, before the JSON key sort. Deterministic successful serialization therefore does not imply that differently ordered invalid objects produce identical first-error paths (`wender/remember/skills.py:9`, `wender/remember/skills.py:19-33`, `wender/state/schema.py:31-39`).

The existing map test adds one exact duplicate, checks one successor, compares a map's dump with itself, and checks one unknown endpoint. The existing admission test checks one valid record and three invalid variants. These tests do not establish collision freedom, permutation-independent export, complete rejection coverage, or a persistence policy (`tests/test_layers.py:63-84`). No performance contract or timing is derived from these assertions.

## STORAGE LAW

The implementation stores program-map objects in memory and returns JSON text; persistence location/merge and the skill store remain AUSENTE. The applicable declared law is that frames never touch disk, also stated directly in the LEARN pipeline docstring (`wender/remember/program_map.py:5-6`, `wender/remember/program_map.py:17-20`, `wender/remember/program_map.py:40-43`, `wender/remember/skills.py:5`, `wender/remember/skills.py:34`, `wender/learn/demonstrations.py:7`).

Neither map mutation nor skill admission inspects payloads for screenshots, base64 images, raw frame arrays, or pixel-bearing strings. Arbitrary supported text can pass through the JSON serializer. Runtime enforcement that all future persisted map/skill/provenance/evidence content is nonpixel metadata is AUSENTE; canonical JSON is not a STORAGE LAW validator (`wender/remember/program_map.py:32-43`, `wender/remember/skills.py:19-34`, `wender/state/schema.py:25-39`).

The bound repository test scans under the repository root for suffixes `.png`, `.jpg`, `.jpeg`, `.webp`, and `.npy`, excluding paths containing `.git`. It is not a runtime guard against other encodings, pixels in JSON, or files outside that root (`tests/test_repo_conventions.py:38-41`).

**PROPOSAL:** define an explicit nonpixel metadata schema and enforce it before any future persistence boundary. Keep all frames and visual checkpoint references in RAM, including failure and diagnostic paths. Storage format, location, retention, and merge semantics remain AUSENTE pending Q4; this proposal does not add a store (`wender/remember/program_map.py:5-6`, `wender/remember/skills.py:5`, `wender/learn/demonstrations.py:7`).

## Findings

These are review findings, not source fixes. Every test below is **PROPOSAL**; the task report separately records any VM probes actually executed.

| Finding | Exact source line(s) and consequence | Proposed test or decision |
|---|---|---|
| Noninjective state-key encoding | `wender/remember/program_map.py:12`: falsey/sentinel, stringification, and colon collisions are possible | Assert current collisions for `None` versus `0` versus `"-"`, and `("a:b","c")` versus `("a","b:c")`. Decide whether to validate an input alphabet or introduce an unambiguous typed encoding before changing expectations. |
| First observation wins for colliding identities | `wender/remember/program_map.py:24-25`: raw identity fields are preserved although `seen` increases | Observe two colliding pairs in opposite orders; assert a shared key and count but different retained raw fields. Propose explicit collision rejection or a documented equivalence policy. |
| Dedup equality differs from JSON identity | `wender/remember/program_map.py:33` uses Python equality, while `wender/state/schema.py:28` serializes booleans and integers differently | Add action `{"flag":True}` and then `{"flag":1}` between the same nodes. Assert one edge today; reverse insertion and compare dumps. Decide whether equality should use canonical, schema-validated action identity. |
| Export has an incomplete tie-breaker | `wender/remember/program_map.py:41` omits `changed`, although `wender/remember/program_map.py:32-34` includes it in dedup | Add same pre/action/post with two distinct changes in opposite orders; assert different current dumps. If history-independent export is intended, propose an explicit canonical `changed` tie-breaker and test permutations. |
| Aliased and duplicate-return edges have different ownership | `wender/remember/program_map.py:32-35`, `wender/remember/program_map.py:38` | Assert that a duplicate result is equal but not identical to the stored edge; mutate the original action and an edge obtained via successors. Define copying/immutability or document shared ownership. |
| Float-key gap in the canonical precheck | `wender/state/schema.py:35-36` traverses values only | Assert that `dumps({1.5:"x"})` currently yields `{"1.5":"x"}`, while a nested float value raises its path-specific `StateError`. Propose string-only JSON object keys if that is the intended canonical domain. |
| Admission validates claims and has structural holes | `wender/remember/skills.py:27`, `wender/remember/skills.py:29`, `wender/remember/skills.py:32-34` | Cover all six exact rejection branches and their precedence; add malformed provenance/steps, truthy nonstring verifier, checkpoint-shape, extra-key, and float cases. Keep independent verification explicitly separate from record admission. |
| Public-symbol anchor coverage is incomplete | Unmarked definitions at `wender/remember/program_map.py:11`, `wender/remember/program_map.py:22`, `wender/remember/program_map.py:28`, `wender/remember/program_map.py:37`, `wender/remember/program_map.py:40`; uniqueness-only test at `tests/test_repo_conventions.py:32-36` | Propose an AST-to-anchor coverage check with a deliberate policy for constants, exceptions, and class methods. Do not invent IDs in this specification. |

## Open questions mapped to HB-WA-a0-arch Q1 to Q6

The architecture question names and statuses come from the bound question table. Impacts below identify unresolved REMEMBER interfaces; they do not claim behavior in unbound producer or executor code.

| Question | REMEMBER mapping and unresolved decision |
|---|---|
| **Q1 — vision-engine entry point and frame source** (`hyperbooks/HB-WA-a0-arch/book.md:114`) | The map accepts an unchecked screen fingerprint, so its format, producer contract, and relation to capture epochs are AUSENTE here. Define those before treating state keys as collision-safe screen identities (`wender/remember/program_map.py:4-6`, `wender/remember/program_map.py:11-12`). |
| **Q2 — AT-SPI tree walk into STATE** (`hyperbooks/HB-WA-a0-arch/book.md:115`) | REMEMBER accepts the caller's stable AX identity and does not know whether it came from a complete desktop STATE. Desktop identity provenance and completeness handling are AUSENTE in this map (`wender/remember/program_map.py:11-25`). |
| **Q3 — per-STATE token budget** (`hyperbooks/HB-WA-a0-arch/book.md:116`) | No budget bounds node history, actions, changes, skill fields, or dump size. How a future STATE budget affects stored identity and evidence is AUSENTE (`wender/remember/program_map.py:24-25`, `wender/remember/program_map.py:32-43`, `wender/remember/skills.py:19-34`). |
| **Q4 — program-map persistence and merge policy** (`hyperbooks/HB-WA-a0-arch/book.md:117`) | Directly unresolved: persistence and merge are AUSENTE. Decide collision semantics, observation-count merging, edge identity/order, and skill-store boundaries before promising durable or merged canonical state (`wender/remember/program_map.py:5-6`, `wender/remember/program_map.py:24-43`, `wender/remember/skills.py:5`). |
| **Q5 — first responsibility in rust/** (`hyperbooks/HB-WA-a0-arch/book.md:118`) | REMEMBER has no frame-capture or shared-memory ownership interface; it only receives caller objects. Any future transport must preserve the RAM-only frame boundary. That integration is AUSENTE (`wender/remember/program_map.py:11-25`, `wender/learn/demonstrations.py:7`). |
| **Q6 — cross-origin iframes and closed shadow roots in AX prune** (`hyperbooks/HB-WA-a0-arch/book.md:119`) | The map neither computes AX pruning nor annotates observation completeness. How partial AX coverage changes identity, edges, or verification is AUSENTE in these contracts (`wender/remember/program_map.py:8-25`, `wender/remember/skills.py:26-34`). |
