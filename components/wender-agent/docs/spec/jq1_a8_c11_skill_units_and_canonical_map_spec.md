# C7/C-11 skill units and canonical program maps

## Source binding and status

The historical analysis and independent reference contract below originated in `jq1-a8-c11-skill-units-and-canonical-map-spec`, attempt `a1`; the FX2 production implementation section records the later repair under `fx2-f4-learn-remember-canonical-skills`, attempt `a2`. Repository: `example-owner/wender-agent`. Requested and read ref: `bridge/integration`. The GitHub connector resolved that ref to **`f0c75b21d8114ce58e9a0c66ad3a3dc3fb62a9a0`**. All seven bound files were read with the explicit ref; their Git blob SHAs were then compared with reads at that exact commit and matched. References beginning `f0c75b21:` and using `#L` belong only to that historical snapshot; they must not be interpreted as current line numbers. Unqualified `path:line` coordinates in the FX2 production section identify the revised source and are checked separately.

The task card and metadata record an earlier commit, `70360d6fb0b1d8ae47ca0740b35c63092c909a44`. The controlling prompt explicitly requests the resolved `bridge/integration` ref, so that observed ref governs this analysis and the branch base. This discrepancy is recorded, not silently replaced with a claim that the older commit was observed. The two bound contract documents describe an even earlier snapshot, `c0a8df9ec8809f38ab3f6d9d18f776a7cdfb39b9` (`f0c75b21:docs/spec/WA_S3_REMEMBER_CONTRACTS.md#L3`, `f0c75b21:docs/spec/WA_S4_LEARN_CONTRACTS.md#L3`).

**BASELINE** means observed at the historical snapshot above. **CURRENT** is reserved for the FX2 production section. **ABSENT** in baseline or TARGET sections describes those scopes, not a claim that a later implementation is missing. **TARGET** names the independent executable reference contract. Its functions remain inside `tests/test_jq1_a8_c11_skill_units_and_canonical_map_spec.py`; they are not substituted for production code. Production differences and remaining integration limits are recorded explicitly below.

Source evidence E0 is SHA-256 `50afaa71076ce3999cc80ea61701b0cb08fd92b55cc37dff7ac5183ddd75802e` for the exact 1,831 bytes of `wender/remember/program_map.py`. The base64 file response was decoded, matched against the UTF-8 response, and checked against Git blob SHA `6d6e535652f8f5e500c9989bed3398a72494bd49`. E1, the exact eleventh line without its terminator, is:

```python
def state_key(screen_fp, ax_stable_sha):
```

The following snapshot-qualified coordinates preserve the original review evidence. Their exact text is historical; the live source pins below move with the repair. Method names refer to definitions, not invented repository anchor comments.

| Exact coordinate | Observed source text, indentation omitted in this table |
|---|---|
| `f0c75b21:wender/remember/program_map.py#L11` | `def state_key(screen_fp, ax_stable_sha):` |
| `f0c75b21:wender/remember/program_map.py#L22` | `def observe(self, screen_fp, ax_stable_sha):` |
| `f0c75b21:wender/remember/program_map.py#L28` | `def add_edge(self, pre, action, post, changed=None):` |
| `f0c75b21:wender/remember/program_map.py#L37` | `def successors(self, key):` |
| `f0c75b21:wender/remember/program_map.py#L40` | `def to_json(self):` |
| `f0c75b21:wender/remember/skills.py#L17` | `def admit(skill):` |
| `f0c75b21:wender/remember/skills.py#L26` | `ev = skill["evidence"]` |
| `f0c75b21:wender/remember/store.py#L57` | `def _version(skill):` |
| `f0c75b21:wender/remember/store.py#L203` | `def append_skill(self, skill):` |
| `f0c75b21:wender/learn/replay.py#L135` | `draft["evidence"] = {` |
| `f0c75b21:wender/state/schema.py#L31` | `def _no_float(o, path="$"):` |

## C7 baseline behavior analysis

### D-A8-01: state identity loses type and boundaries

BASELINE `state_key` constructs `"%s:%s" % (screen_fp or "-", ax_stable_sha or "-")`. Missing values, empty strings, integer zero and `False` collapse to the hyphen sentinel; a literal hyphen also collides. Truthy values are stringified, so integer `1` and string `"1"` can collide. Embedded colons erase component boundaries: `("a:b", "c")` and `("a", "b:c")` both produce `"a:b:c"`. These are deductions from the expression, not claims about actual producer traffic (`f0c75b21:wender/remember/program_map.py#L11-L12`).

BASELINE `observe` increments `seen` on a shared computed key while retaining the first raw identity pair. Consequently, a collision can change which identity is retained when observation order changes. A permitted identity alphabet, typed encoding and collision-resolution policy are ABSENT (`f0c75b21:wender/remember/program_map.py#L22-L26`).

### D-A8-02: Python equality is not canonical edge identity

BASELINE `add_edge` checks that both endpoints exist, constructs a four-field dictionary, and tests membership in `self.edges` using Python equality. That equality includes `changed`, but it treats `True` and integer `1` as equal inside an action or change payload. JSON distinguishes them. Deduplication can therefore retain whichever differently encoded payload arrived first. An explicit canonical edge identity is ABSENT (`f0c75b21:wender/remember/program_map.py#L28-L35`, `f0c75b21:wender/state/schema.py#L25-L28`).

### D-A8-03: export omits the final tie-breaker

BASELINE `to_json` sorts by `(pre, dumps(action), post)` and emits `changed` without using it to break ties. Two distinct edges with equal endpoints and action but different changes retain insertion order under the stable sort. Sorting object keys does not remove that array-order dependency. A total edge ordering that includes `changed` is ABSENT (`f0c75b21:wender/remember/program_map.py#L40-L43`, `f0c75b21:wender/state/schema.py#L28`).

### D-A8-04: mutable aliases cross the map boundary

BASELINE edge insertion retains `action` and `changed` references. A new edge's returned dictionary is also the stored dictionary; a duplicate returns a freshly constructed candidate rather than the previously stored object. `successors` creates a new list whose dictionaries still belong to the map. Input mutation and mutation through a returned edge can therefore change later exports. The public `nodes` and `edges` containers have no ownership barrier. Defensive-copy guarantees are ABSENT (`f0c75b21:wender/remember/program_map.py#L18-L20`, `f0c75b21:wender/remember/program_map.py#L32-L38`).

### D-A8-05: the integer-only check skips object keys

BASELINE `_no_float` rejects encountered float values and descends through dictionary values and list/tuple items, but never validates dictionary keys. A sole float key, including one nested in another object or list, can reach JSON's key conversion. This does not imply every mixed-key input succeeds: native key sorting may reject incompatible key types. Rejection of float keys and a string-only object-key domain are ABSENT from this precheck (`f0c75b21:wender/state/schema.py#L31-L39`).

### D-A8-06: C-11 promotion and version allocation are missing

BASELINE skill admission requires `id`, `intent`, `params`, `steps`, `checkpoints`, `provenance` and `evidence`. It requires nonempty steps/checkpoints, literal `True` at `evidence.reproduced`, a truthy verifier and provenance source, and step paths in `api`, `ax` or `coordinates`. It returns canonical JSON. It does not execute a verifier or define the new C-11 fields; a claim passing validation is not independently proven reproduction (`f0c75b21:wender/remember/skills.py#L9`, `f0c75b21:wender/remember/skills.py#L17-L34`, particularly `f0c75b21:wender/remember/skills.py#L26`).

BASELINE storage exists. It appends validated logs, reloads maps and skills, and compacts them. It uses observation batches whose node counts are additive deltas. It is therefore incorrect to carry forward an unqualified claim that present persistence is ABSENT (`f0c75b21:wender/remember/store.py#L4-L6`, `f0c75b21:wender/remember/store.py#L165-L200`, `f0c75b21:wender/remember/store.py#L203-L253`). The earlier S3 document's persistence statements describe its own snapshot (`f0c75b21:docs/spec/WA_S3_REMEMBER_CONTRACTS.md#L9`, `f0c75b21:docs/spec/WA_S3_REMEMBER_CONTRACTS.md#L115-L121`).

BASELINE `_version` accepts exact Python integers, including zero and negative integers; `_newer` requires both versions to be integers and merely `new > old`. A first otherwise admitted skill is not required to carry a version. An identical canonical record returns before the version comparison; a changed record whose previous version is missing cannot satisfy `_newer`. Initialization at one, consecutive allocation and validation of first-record versions are ABSENT (`f0c75b21:wender/remember/store.py#L53-L64`, `f0c75b21:wender/remember/store.py#L203-L216`).

BASELINE `replay_and_admit` does execute steps, evaluate matched checkpoints, compare run summaries and admit a resulting draft. Its default is two runs. It mutates the draft by replacing its evidence with `reproduced`, `verified_by`, `runs` and `sha256`, omitting `version`. This can discard a caller-supplied version; it does not allocate a new one (`f0c75b21:wender/learn/replay.py#L72-L73`, `f0c75b21:wender/learn/replay.py#L92-L142`, particularly `f0c75b21:wender/learn/replay.py#L135`). The earlier S4 text discusses a separate demonstrations pipeline and cannot establish absence of this bound replay function. The present implementation of that unbound pipeline is ABSENT from this review's evidence (`f0c75b21:docs/spec/WA_S4_LEARN_CONTRACTS.md#L13-L21`, `f0c75b21:docs/spec/WA_S4_LEARN_CONTRACTS.md#L46-L52`).

The findings above remain historical cited analysis. Independent reference tests remain ordinary passing assertions; repaired production pins in their dedicated modules are promoted only after their original semantic assertions pass. The source-coordinate tests distinguish preserved historical evidence from the live implementation, rather than asserting that old line numbers remain current.

## C-11 skill-unit target

**TARGET:** a skill unit is a typed, nonpixel procedure record with an explicit starting condition, ordered actions, expected outcome, recovery description and promotion evidence. The executable reference deliberately defines a small closed schema. Its shape is a proposal, not an assertion that production already validates these fields. BASELINE has `steps`; the C-11 additions and Markdown interface are ABSENT from the bound implementations (`f0c75b21:wender/remember/skills.py#L9-L34`, `f0c75b21:wender/learn/replay.py#L135-L142`).

| Field | TARGET shape and meaning |
|---|---|
| `id`, `intent` | Nonblank exact strings; stable unit identifier and intent description. |
| `params` | Dictionary of named, nonpixel metadata values; an empty dictionary is allowed. |
| `precondition_signature` | Nonempty metadata dictionary describing the expected initial structured state and any identity binding. It is declarative; no pixel or semantic predicate evaluator is supplied here. |
| `steps` | Nonempty ordered list of exact `{path, action}` dictionaries. `path` is `api`, `ax` or `coordinates`; `action` is a nonempty metadata dictionary. Coordinates are integer action metadata, never a frame. |
| `expected_post` | Nonempty metadata dictionary containing the declared final structured outcome. |
| `checkpoints` | Nonempty list of exact `{after_step, expected_post}` dictionaries. `after_step` is an exact integer in `0..len(steps)-1`; each expected outcome is a nonempty metadata dictionary. |
| `failure_recovery` | Exact `{policy, steps}` dictionary. `policy="abort"` requires an empty step list; `policy="replay"` requires a nonempty list of the same step shape. This is a recovery plan, not execution authority. |
| `support_n` | Exact nonnegative integer evidence-support count. Promotion requires at least three. |
| `min_err` | Exact integer in a draft; promotion requires at least zero. Its operational statistic and units are ABSENT, as explained below. |
| `provenance` | Exact `{source}` dictionary, with a nonblank source string. |
| `evidence` | Required `reproduced` Boolean and `verified_by` string; an admitted unit also requires `version`. Optional `runs` is a positive exact integer and optional `sha256` is 64 lowercase hexadecimal characters. |

These are exactly twelve top-level fields because `steps` is shared with the existing seven-field record. Unknown top-level fields and unknown nested schema fields are rejected, rather than discarded. Optional replay `runs` and `sha256` are preserved across copying, version allocation and Markdown roundtrips. `verified_by` may be blank in a draft so the gate can return false; admitted records require it to be nonblank. `reproduced` must be an actual Boolean, not integer one.

TARGET checkpoint numbering stays zero-based, consistent with the existing replay loop's use of `enumerate(steps)` and matching `after_step` against `step_idx`. The proposed checkpoint value field is still a new schema; an adapter to the current `expect` field is ABSENT (`f0c75b21:wender/learn/replay.py#L101-L118`). Recovery execution, actual predicate evaluation, authorization and completeness of checkpoint coverage are outside this reference. It validates the declared shape and evidence claims without manufacturing those claims.

## Promotion thresholds

**TARGET:** `promotion_eligible(skill)` first validates the draft's schema and integer-only metadata, then returns the conjunction of these four conditions:

```python
skill["support_n"] >= 3
skill["min_err"] >= 0
skill["evidence"]["reproduced"] is True
bool(skill["evidence"]["verified_by"].strip())
```

The numeric predicates above operate only on exact integers after validation: `False`, `True`, strings and floats are invalid numeric fields. Negative support is malformed. A negative `min_err` is a representable draft that is ineligible; it cannot be rendered or promoted as an admitted unit. A structurally valid draft that misses a threshold returns false; invalid shape raises `SpecRejected`.

The numeric cross-product below is a concrete TARGET truth table when reproduction is true and the verifier is nonblank. Every cell is repeated with the other evidence combinations; false reproduction or a blank verifier makes every cell ineligible.

| `support_n` | `min_err=-1` | `min_err=0` | `min_err=1` |
|---:|:---:|:---:|:---:|
| 0 | false | false | false |
| 2 | false | false | false |
| 3 | false | true | true |
| 4 | false | true | true |

TARGET support should count distinct qualifying replay evidence units bound to the same preconditions, procedure, expectations and verifier configuration. Reusing the same receipt must not inflate support. The reference accepts an already aggregated count; counting receipts, proving independence and reconciling `support_n` with optional `runs` remain ABSENT. The existing replay default of two runs cannot be substituted for the requested threshold of three (`f0c75b21:wender/learn/replay.py#L72`).

The bound sources do not define `min_err`, its units or whether a larger value is desirable. Consequently, `min_err >= 0` is the requested TARGET domain gate, not a calibrated error-quality claim. No upper cutoff, zero-error-only rule, confidence level or success-rate interpretation is invented. Operational meaning and any stronger acceptance threshold remain ABSENT pending a separately specified evidence producer.

## Evidence version lifecycle

**TARGET:** `next_evidence_version(previous=None)` returns integer **1** when there is no previous record. For an eligible, fully versioned previous target unit, it returns the previous exact positive integer version **plus one**. Thus the first and second revisions carry 1 and 2. `promote_skill(draft, previous=None)` returns a defensive copy with that computed version, retaining all other permitted evidence.

| Situation | Explicitly proposed handling |
|---|---|
| No previous record; draft omits version | Allocate 1. Only the outer `previous=None` sentinel means no previous record. |
| No previous record; draft supplies version 1 | Accept 1 after validating the entire draft and promotion gate. |
| Previous eligible version 1; draft omits version | Allocate 2. |
| Previous eligible version 1; draft supplies version 2 | Accept 2. |
| Candidate supplies stale version, a skipped version, or a nonpositive/noninteger version | Raise `SpecRejected`; do not override it silently. |
| Previous record lacks version, or contains `None`, a Boolean, string, float, zero or negative version | Raise `SpecRejected`; do not repair, coerce or guess a prior version. |
| Previous record is malformed/ineligible or its id differs from the draft | Reject promotion without mutating either input. |

This allocation policy is explicitly stronger than BASELINE store ordering, which accepts any strictly greater pair of exact integer versions and does not enforce first version 1 (`f0c75b21:wender/remember/store.py#L57-L64`, `f0c75b21:wender/remember/store.py#L203-L216`). It also differs from BASELINE replay's replacement of the evidence dictionary (`f0c75b21:wender/learn/replay.py#L135-L140`).

Each successful promotion invocation creates a new revision. It is not a persistence retry API: an exact same-version repeat is rejected as stale by promotion, while BASELINE store retries of an identical canonical body are no-ops. Persist the already allocated revision when retrying a store append. Concurrent allocation, locking and migration of legacy versionless records are ABSENT from this reference; the existing store declares a single-writer requirement (`f0c75b21:wender/remember/store.py#L5-L6`, `f0c75b21:wender/remember/store.py#L211-L216`).

## Deterministic non-pixel Markdown

**TARGET:** `render_skill_markdown` accepts only an eligible, versioned target unit and returns this exact grammar, where `J` is one canonical JSON text line:

```text
# Wender C-11 skill unit (TARGET v1)\n\n```json\nJ\n```\n
```

The line above describes escape sequences; the returned Markdown contains actual LF characters. Concretely, the renderer is the concatenation below. This keeps the Markdown envelope deterministic and keeps data out of headings, link destinations and executable template slots.

```python
PREFIX = "# Wender C-11 skill unit (TARGET v1)\n\n```json\n"
SUFFIX = "\n```\n"
markdown = PREFIX + canonical_json(skill) + SUFFIX
```

`canonical_json` uses sorted string keys, compact `(',', ':')` separators and `ensure_ascii=False`, without Unicode normalization. Encoded bytes are UTF-8. JSON escapes embedded CR, LF and control characters, so an intent containing a newline followed by backticks cannot close the outer fence. Accents, CJK characters, emoji and combining marks roundtrip exactly; composed and decomposed strings remain distinct. Lone surrogate code points are rejected because they do not form valid UTF-8 text.

TARGET `parse_skill_markdown` requires the exact header, blank line, opening fence, single JSON body line, closing fence and final LF. It rejects duplicate JSON member names at any nesting depth, float syntax including nonfinite constants, malformed JSON, wrong schema, ineligible or unversioned records, CRLF envelopes, missing/extra fences, extra headings, comments and trailing material. Re-encoding the parsed body must equal the supplied canonical body: reordered keys, pretty-print spacing, noncanonical escapes and other alternate spellings are refused. This is a strict interchange subset of Markdown, not a general Markdown importer.

The JSON domain admits only nonpixel metadata. Binary objects and reserved pixel-related keys are rejected recursively. This structural rule is deliberately bounded: arbitrary text can conceal an encoded image, and a string validator cannot establish its provenance. The producer must supply nonpixel metadata; runtime detection of all hidden pixel encodings is ABSENT. BASELINE store already rejects several binary types, forbidden keys and cycles, so its existing guard is acknowledged rather than described as nonexistent (`f0c75b21:wender/remember/store.py#L19`, `f0c75b21:wender/remember/store.py#L32-L50`). The reference opens no frame buffers and writes no frame, image, video or screenshot to disk.

## Typed state identity

**TARGET:** `state_key(screen_fp, ax_stable_sha)` accepts only exact `None`, Boolean, integer or Unicode-scalar string components. Each component is encoded as `[tag, value]`, using `none`, `bool`, `int` and `str` tags. The resulting key is canonical JSON for:

```python
["wender.state-key/target-v1", [screen_tag, screen_fp], [ax_tag, ax_stable_sha]]
```

The prefix binds the proposed representation version. Typed JSON values and the two array positions preserve type, value and component boundaries. Under this accepted domain, equality of encoded keys implies equality of both original typed components. No delimiter escaping heuristic, truthiness fallback, string coercion or lossy normalization is involved. This is injectivity of an encoding; it does not prove that an upstream screen fingerprint or AX hash uniquely identifies a real application state.

Examples below hold the AX component at string `"x"`:

| Screen component | Exact TARGET key |
|---|---|
| `None` | `["wender.state-key/target-v1",["none",null],["str","x"]]` |
| Empty string | `["wender.state-key/target-v1",["str",""],["str","x"]]` |
| Hyphen | `["wender.state-key/target-v1",["str","-"],["str","x"]]` |
| Integer zero | `["wender.state-key/target-v1",["int",0],["str","x"]]` |
| Boolean `False` | `["wender.state-key/target-v1",["bool",false],["str","x"]]` |

The pair `("a:b", "c")` encodes as `["wender.state-key/target-v1",["str","a:b"],["str","c"]]`; `("a", "b:c")` encodes as `["wender.state-key/target-v1",["str","a"],["str","b:c"]]`. Tests exercise ambiguity candidates in both component positions, including integer/string zero, Boolean/integer identities and colon-containing values. Lists, dictionaries, tuples, float components and custom subclasses are rejected.

TARGET `observe` stores the original typed scalar fields with `seen=1` on the first observation, increments the same node on exact repeated observation and returns the typed key. Equal observation multisets therefore yield equal node snapshots regardless of insertion order. The count still depends on the number of observations; history independence never means ignoring actual evidence counts. Conversion of BASELINE sentinel keys to this encoding is ABSENT and cannot reconstruct identities already lost to collisions (`f0c75b21:wender/remember/program_map.py#L12`, `f0c75b21:wender/remember/program_map.py#L22-L26`).

## Canonical edge identity and ordering

**TARGET:** `CanonicalProgramMap(program)` requires a nonblank exact string program name and retains the logical four-field edge `{pre, action, post, changed}`. Endpoints must already be observed typed keys. The edge identity is the canonical JSON of the complete validated edge, including `changed`. Map actions and changes may be any value from the strict metadata domain; skill-step actions have the narrower dictionary shape defined above.

Canonical identity distinguishes these action texts and their resulting edges:

```text
{"flag":true}
{"flag":1}
```

It also distinguishes direct or nested `True` and integer one in `changed`. Dictionary insertion order is immaterial; array order is meaningful. Only a byte-identical canonical edge is deduplicated. Duplicate insertion does not increase a count or alter any stored payload. This repairs the proposed equality contract without asserting a production repair at `f0c75b21:wender/remember/program_map.py#L33`.

TARGET sorting uses the complete tuple:

```python
(edge["pre"], canonical_json(edge["action"]),
 edge["post"], canonical_json(edge["changed"]))
```

Comparison is lexicographic Python string order over canonical texts. The final component is the explicit **changed tie-breaking** rule. For otherwise equal edges whose changes are `null`, `{"n":1}` and `{"n":2}`, the output order is exactly that sequence: `n` sorts before `{`, then the object texts differ at the integer digit. This is a lexical rule, not numeric or domain-specific ranking. The test oracle uses its own literal expected complete edge list, not a duplicate sorting helper.

The same total order governs `successors`, snapshots and export. A query for an unknown key raises `KeyError` in the reference; BASELINE returns an empty list when no stored edge matches. That stricter query policy is a TARGET choice (`f0c75b21:wender/remember/program_map.py#L37-L38`). A snapshot contains `schema="wender.program-map/target-v1"`, `program`, nodes and edges; canonical serialization orders all object keys. The new schema marker prevents claiming compatibility with BASELINE `wender.program_map/v0` (`f0c75b21:wender/remember/program_map.py#L42-L43`).

## Ownership and integer-only validation

**TARGET:** canonical metadata is a finite acyclic tree of exact `None`, `bool`, `int`, UTF-8-encodable `str`, `list` and `dict`. Dictionary keys must be exact strings. Tuples, sets, bytes, bytearrays, memoryviews, user subclasses and floats are refused. Rejection includes float keys nested in dictionaries under lists, ordinary float values and nonfinite floats. Values such as `1.0` are not rounded or coerced to integers. String keys avoid JSON's native coercion of unrelated keys to identical property names.

Reserved keys `frame`, `frames`, `pixels`, `image`, `screenshot` and `png` are rejected case-insensitively through `casefold`, at every object depth. This TARGET rule is broader than BASELINE store's exact reserved-key matching and complements the stated producer obligation to provide nonpixel metadata (`f0c75b21:wender/remember/store.py#L19`, `f0c75b21:wender/remember/store.py#L41-L45`). Cycles are rejected; repeated references to an acyclic object are allowed and serialized by value. String keys and values are validated for Unicode scalar content before export.

TARGET mutation boundaries validate the complete candidate before changing internal state. Failed endpoint checks or invalid nested data leave the graph unchanged. Input `action`/`changed` payloads are copied on ingress; `add_edge` returns an independent deep copy for both new and duplicate insertions. `successors` and `snapshot` return independent copies, including nested objects. Reordering or clearing a returned container, or mutating any nested payload obtained through a public reference method, must not alter future snapshots. `promote_skill` similarly leaves draft and previous record untouched. Private underscore fields are an implementation convention, not a security boundary against deliberate internal access.

This ownership contract is limited to the pure reference API. It provides no locks, interprocess consistency, memory budget, persistence merge or atomic filesystem operation. Those integration behaviors remain ABSENT here. Existing store code continues to own its actual persistence behavior; these tests never call it (`f0c75b21:wender/remember/store.py#L165-L176`, `f0c75b21:wender/remember/store.py#L225-L253`).

## Reference functions and examples

**TARGET:** the following illustrative code uses only functions defined in the permitted test module. It does not import a new runtime module or execute application actions.

```python
from tests.test_jq1_a8_c11_skill_units_and_canonical_map_spec import (
    CanonicalProgramMap, parse_skill_markdown, promote_skill,
    render_skill_markdown, state_key,
)

draft = {
    "id": "open-settings",
    "intent": "Abrir ajustes — 設定",
    "params": {},
    "precondition_signature": {"screen": "home", "ax": "stable-home"},
    "steps": [{"path": "ax", "action": {"invoke": "settings"}}],
    "expected_post": {"screen": "settings"},
    "checkpoints": [{"after_step": 0, "expected_post": {"screen": "settings"}}],
    "failure_recovery": {"policy": "abort", "steps": []},
    "support_n": 3,
    "min_err": 0,
    "provenance": {"source": "offline:fixture:settings"},
    "evidence": {"reproduced": True, "verified_by": "fixture-verifier/v1"},
}
v1 = promote_skill(draft)
assert v1["evidence"]["version"] == 1
assert "version" not in draft["evidence"]
v2 = promote_skill(draft, previous=v1)
assert v2["evidence"]["version"] == 2
assert parse_skill_markdown(render_skill_markdown(v1)) == v1

graph = CanonicalProgramMap("offline-settings")
home = graph.observe(None, "stable-home")
settings = graph.observe("settings", "stable-settings")
action = {"invoke": "settings", "options": {"enabled": True}}
graph.add_edge(home, action, settings, {"screen": "settings"})
action["options"]["enabled"] = False
assert graph.successors(home)[0]["action"]["options"]["enabled"] is True
assert state_key(None, "x") != state_key("-", "x")
assert state_key(0, "x") != state_key(False, "x")
```

The executable module additionally exposes `canonical_json`, `promotion_eligible` and `next_evidence_version`. `SpecRejected` distinguishes invalid target inputs. The fixture claims are deliberately supplied in memory; they are not presented as actual application replay evidence. Markdown rendering never confers execution permission, and parsing a procedure never runs it.

## Verification matrix

The reference suite must pass ordinary tests with independent, concrete expected values. It must not recompute expected outputs by copying the reference encoder, comparator or admission predicate into a helper. Literal serialized identities, explicit promotion truth tables and fixed expected edge sequences provide independent checks; roundtrip and permutation properties supplement those checks.

| Contract | Reference checks and concrete failure modes |
|---|---|
| Promotion thresholds | Full support/error cross-product above, repeated across reproduced and verifier conditions; malformed numeric types, boundaries below three and at zero. |
| Skill schema | Required fields, nonempty structured conditions, valid step/recovery/checkpoint shapes, zero-based checkpoint bounds, unsupported fields and malformed evidence. |
| Evidence version lifecycle | Literal 1 then 2; valid supplied versions; missing, Boolean, nonpositive, float and string prior versions; stale/skipped candidates; identity mismatch; optional replay evidence retention and unchanged inputs. |
| Deterministic Markdown | Exact expected envelope and canonical Unicode content; accents/CJK/emoji/combining characters, escaped control characters and embedded fences; malformed/duplicate/noncanonical JSON, invalid schema, bad versions and altered envelopes. |
| State identity | Literal expected strings and distinct typed pairs for None, empty, hyphen, zero, False, colon and component swapping; Unicode exactness and invalid component types. |
| Canonical graph | Complete expected ordered snapshots for all permutations of edges with tied endpoints/action and distinct changes; direct/nested Boolean versus integer distinctions in both action and changed; duplicate insertion and observation-order invariance. |
| Ownership | Mutate original action/change data, insertion results, duplicate results, successors and snapshots at nested depths; compare to fixed unmodified expected records. |
| Strict metadata domain | Root and nested float keys, keys under list elements, float values, nonfinite values, nonstring keys, cycles, binary/pixel keys, invalid Unicode and custom types; failed mutation leaves prior state intact. |
| Document and evidence pinning | Document exists, required sections contain substantive contract terms, every bound citation coordinate/range exists, historical coordinates retain their recorded evidence, and live production coordinates retain their exact expected source lines. |

Run from the repository root:

```bash
python3 -m unittest tests.test_jq1_a8_c11_skill_units_and_canonical_map_spec
```

All behavioral fixtures are offline, in memory and stdlib-only. Document/source pinning necessarily reads the source files and this Markdown document; it does not create fixture files. No browser, network, GPU, clock, random source, production store, frame capture or pixel file is involved. The unittest runner's elapsed line, if recorded by the task report, is labeled **VM** and is not a production performance measurement. Observed successes and failures belong in that report; this specification itself does not certify orchestrator acceptance.

## Historical integration gaps

**BASELINE ABSENT:** a production C-11 promoter or Markdown importer/exporter; operational `min_err` semantics and units; independently established support aggregation; full semantic verification of declared preconditions, postconditions and recovery; a production adapter for the target's closed schema; automatic migration of old keys or versionless evidence; and any implementation of these TARGET copy/ordering rules in production. The relevant current boundaries remain admission, map mutation, storage and replay as cited above (`f0c75b21:wender/remember/skills.py#L17-L34`, `f0c75b21:wender/remember/program_map.py#L11-L43`, `f0c75b21:wender/remember/store.py#L57-L64`, `f0c75b21:wender/remember/store.py#L203-L216`, `f0c75b21:wender/learn/replay.py#L135-L142`).

**ABSENT:** an upstream identity domain guarantee, collision-free real-world screen identification, complete pixel-content detection within arbitrary text, concurrency behavior for the reference map, durable TARGET serialization migration and production performance results. An injective representation cannot undo collisions already present in a fingerprint or in legacy stored sentinel keys.

The later FX2 integration below addresses the applicable production gaps. The independent TARGET contract remains reviewable and is not relabeled as runtime code. A pull request collects the implementation and observed test evidence; it does not certify orchestrator acceptance.

## FX2 production implementation

The a2 repair was prepared from the explicit `bridge/integration` ref resolved to `820b9810cb986e037f7330f5e6c80a6d6ce4e517`. Its task metadata cites `97a66e90c7b7886fefb9a969189363274b8f1213`; that older metadata does not replace the ref observed for the repair. The original C7 source binding, hash, quote, and reference behavior above remain historical evidence. The sections below describe the revised production implementation, without relabeling the independent TARGET fixtures as runtime code.

### Canonical identity and ownership

CURRENT `state_key` encodes exact `None`, Boolean, integer, or UTF-8 string components with explicit tags and two fixed positions under the production marker `wender.state-key/v1`. This is intentionally a different marker from the independent reference's `wender.state-key/target-v1`. Empty text, the hyphen, zero, `False`, colon-containing strings, and swapped components remain distinct. There is no legacy alias-node path. Invalid component types and invalid Unicode are rejected before observing a node.

CURRENT `canonicalize_state_key(stored_key, screen_fp, ax_stable_sha)` is an explicit read-normalization helper. It validates the exact typed raw components and stored UTF-8 string, accepts either the canonical typed spelling or the exact historical `"%s:%s" % (screen_fp or "-", ax_stable_sha or "-")` spelling, and returns the canonical plain string. It never splits legacy text, guesses component boundaries, changes `state_key` equality, or creates alias nodes. This is a per-record transformation using supplied raw identity fields; a journal reader must separately detect a legacy spelling reused for different typed identities and remap edge endpoints. The Store read path invokes this helper before reconstructing each node, then maintains a per-program spelling table to detect ambiguous identity mappings and normalize observed edge endpoints.

CURRENT edge identity is canonical JSON for the complete `{pre, action, post, changed}` record; nested Boolean/integer distinctions survive deduplication. The same full `(pre, canonical action, post, canonical changed)` order drives successors and snapshots. Inserted payloads, insertion results, duplicate results, successors, and snapshots have independent nested ownership. Mutating any of those method results cannot mutate the stored graph.

Production deliberately retains the public `nodes` and `edges` containers needed by the Store reconstruction code. Direct mutations of those containers are deliberate graph edits, not protected read methods. The independent TARGET class's private-container ownership is therefore stronger than the production compatibility boundary. Production also retains the established empty-list result for an unknown successor key and accepts the existing empty string program identifier; the TARGET reference instead rejects an unknown key and requires a nonblank program name.

CURRENT map serialization retains the `wender.program_map/v0` envelope for the present Store interface while its state-key marker changes. Envelope retention alone is not a compatibility or migration claim; the bounded read normalization below is a separate implementation. The canonical metadata validator rejects floats including dictionary keys, nonstring keys, custom types, invalid Unicode, cycles, binary objects, and reserved pixel keys at any nesting depth. It accepts shared acyclic values by value. The unchanged general `state.schema._no_float` still has its separately documented key-check boundary; the owned map/skill paths perform their stronger validation before serialization.

### Legacy admission and C-11 variants

CURRENT `admit` retains the established rejection precedence: required fields, nonempty steps, nonempty checkpoints, verified reproduction, provenance source, and permitted step paths. Invalid container types are refused at their corresponding stage. Identity and intent are nonblank exact strings, params is an exact dictionary, and all admitted metadata satisfies the stronger canonical validator. Plain legacy records continue to permit extra metadata, arbitrary nonempty checkpoint metadata, truthy source/verifier values, and an absent or otherwise legacy-shaped optional version. Passing this record validation does not execute a verifier.

C-11 opt-in occurs when a record contains any workflow-unit field or either support counter. Partial C-11 records cannot silently fall back to legacy admission. Two explicitly distinguished forms are supported:

| Production C-11 form | Schema and retained behavior |
|---|---|
| Structured unit with top-level `support_n` and `min_err` | The independent reference's closed twelve-field schema: structured `{path, action}` steps, zero-based `{after_step, expected_post}` checkpoints, explicit abort/replay recovery, source-only provenance, and bounded evidence fields. |
| Existing workflow unit with counters in `evidence` | Preserves the published production fixture's legacy action strings, checkpoint metadata, recovery metadata, and provenance additions. It still requires all three workflow-unit dictionaries, exact counter types, nonblank source/verifier, reproduced success, and a positive exact evidence version. |

Both forms enforce `support_n >= 3`, `min_err >= 0`, literal reproduced success, and nonblank verifier text. A negative exact `min_err` is a valid ineligible draft for `promotion_eligible`, while an admitted unit must meet the gate. Optional runs must be positive exact integers and optional hashes must be 64 lowercase hexadecimal characters. The two counter locations are not silently moved or coerced; mixing top-level structured counters with evidence-only legacy fields is rejected by the structured schema.

CURRENT `next_evidence_version` and `promote_skill` allocate one for no previous C-11 unit and exactly the valid prior unit's version plus one otherwise. Previous records are fully admitted and must have a positive exact version; malformed, unversioned, mismatched-id, stale, and skipped-version inputs are refused. Successful promotion returns a nested copy while preserving validated replay fields and caller inputs. It does not aggregate receipts or create verification evidence.

CURRENT `render_skill_md` and `parse_skill_md` implement the strict canonical Markdown envelope below; `render_skill_markdown` and `parse_skill_markdown` are explicit aliases. The production header omits the independent reference's `TARGET` qualifier, so the formats are intentionally distinguished:

```python
PREFIX = "# Wender C-11 skill unit (v1)\n\n```json\n"
SUFFIX = "\n```\n"
```

The body is one canonical sorted-key Unicode JSON line. Parsing rejects duplicate object names, float syntax and nonfinite constants, alternate headers or fences, CRLF envelopes, trailing material, noncanonical key order/spacing/escapes, malformed records, and invalid promotion/version claims. All validated fields and Unicode survive roundtrip. Importing this record is data parsing and grants no action authority.

### Replay and verified segment handoff

CURRENT `replay_and_admit` requires an exact integer run count of at least two before any Driver reset, observation, or action. It validates every checkpoint index and its supported expectation predicates before executing. Both legacy `expect` and C-11 `expected_post` checkpoint fields are understood, but a checkpoint must choose one field and its contents must be predicates the evaluator implements: `node_present`, `node_absent`, or `text_contains`. An arbitrary metadata dictionary cannot become a vacuously successful checkpoint. Malformed or conflicting indices, unknown predicates, empty predicate lists, and invalid predicate shapes are rejected.

Replay copies the draft, action payloads, observed STATEs, and checkpoint expectations so mutable Driver state and injected calls cannot alter earlier evidence. Each run must pass its checkpoints and produce the same diff summaries as the first run. The new evidence refreshes the actual verifier label, run count, and summary hash while retaining other validated evidence. A missing prior draft version starts at one; an existing exact positive prior version advances by one. This replay interface treats the input version as the prior revision. It is distinct from `promote_skill(draft, previous=...)`, whose explicit `previous` parameter controls the next version and whose candidate-supplied version must already equal that next version. Neither interface supplies a concurrent allocator or migrates an existing versionless Store record.

CURRENT segment handoff creates skill metadata only after the executed replayer reports literal reproduced and verified success. The outcome includes detached steps, a deterministic metadata-derived id, provenance identifying the segment, checkpoints, and versioned evidence. It retains a supplied verifier lineage only if the receipt and evidence agree; otherwise an absent lineage is identified by the actual executed callable's module and qualified name. Contradictory evidence is refused. When no predicate checkpoint was supplied, the fallback checkpoint is explicitly a verifier receipt, not a fabricated STATE predicate; it is suitable for legacy admission and is refused by replay until a supported expectation is supplied.

A single verified segment never manufactures C-11 support counts, `min_err`, or independently reproduced runs. The demonstrations labeler, mapper, replay, and segmentation placeholders remain `NotImplementedError` stubs, and the honest integer parts-per-million metric still counts only reproduced-and-verified segments. RGB frames stay in memory; only nonpixel metadata can enter the skill record or journal.

### Stopstate checks and F1 integration

CURRENT exploration calls the real `wender.perceive.stopstate.detect`, when installed, on each newly observed STATE before it accepts the corresponding node or asks for candidate actions. This applies to the initial observation, action result, replayed route hop, and reset result. The detector's `is_stop` field must be an exact Boolean, and a STOP must carry a nonblank reason. A STOP receipt is retained and prevents later actions, observations, and resets in that attempt. The explorer copies mutable STATE and action data at its boundaries.

The resolved source snapshot does not contain the F1 `wender/perceive/stopstate.py` dependency. Under the requested optional-dependency policy, that specific missing module preserves legacy exploration and adds `stopstate="unavailable"` to the result; ordinary exhaustion, budget, or error remains the stopping reason. No local heuristic or no-wall verdict is manufactured. A detector that is present but raises an error, returns an invalid receipt, or fails because a different dependency is missing is not treated as unavailable permission to act.

The real detector remains owned by `fx2-f1-perceive-wait-text-stopstate`. The two original real-STOP pins retain their `expectedFailure` markers naming that dependency, while seven ordinary orchestration tests check actual receipt handling, all newly observed boundaries, and the explicit absent-detector fallback. Passing those tests establishes the explorer contract and its disclosed legacy fallback; it does not certify the absent detector's semantics.

### Live source coordinates

These exact current source lines are independently checked against the revised files by `DocumentPinningTests`; the historical table above continues to use its original snapshot-qualified locations. Definition lines identify the implementation being described, while the whole functions supply the corresponding behavior.

| Exact current coordinate | Exact source text, indentation omitted |
|---|---|
| `wender/remember/program_map.py:55` | `def canonical_json(value):` |
| `wender/remember/program_map.py:69` | `def state_key(screen_fp, ax_stable_sha):` |
| `wender/remember/program_map.py:88` | `def canonicalize_state_key(stored_key, screen_fp, ax_stable_sha):` |
| `wender/remember/program_map.py:118` | `def observe(self, screen_fp, ax_stable_sha):` |
| `wender/remember/program_map.py:125` | `def add_edge(self, pre, action, post, changed=None):` |
| `wender/remember/program_map.py:138` | `def successors(self, key):` |
| `wender/remember/program_map.py:143` | `def snapshot(self):` |
| `wender/remember/program_map.py:147` | `def to_json(self):` |
| `wender/remember/skills.py:170` | `def admit(skill):` |
| `wender/remember/skills.py:179` | `def promotion_eligible(skill):` |
| `wender/remember/skills.py:186` | `def next_evidence_version(previous=None):` |
| `wender/remember/skills.py:197` | `def promote_skill(draft, previous=None):` |
| `wender/remember/skills.py:213` | `def render_skill_md(skill):` |
| `wender/remember/skills.py:234` | `def parse_skill_md(text):` |
| `wender/remember/store.py:57` | `def _version(skill):` |
| `wender/remember/store.py:115` | `def _maps(self, records):` |
| `wender/remember/store.py:196` | `def append_map(self, program_map):` |
| `wender/remember/store.py:220` | `def append_skill(self, skill):` |
| `wender/learn/replay.py:73` | `def _checkpoint_index(checkpoint, count):` |
| `wender/learn/replay.py:83` | `def _validate_expect(expect):` |
| `wender/learn/replay.py:106` | `def _observe_state(driver):` |
| `wender/learn/replay.py:116` | `def replay_and_admit(draft, driver, verified_by, runs=2):` |
| `wender/learn/segmenter.py:212` | `def _verified_handoff(intent, actions, segment, receipt, replayer):` |
| `wender/learn/segmenter.py:270` | `def run_pipeline(frames, engine, labeler=demonstrations.label_intent,` |
| `wender/learn/demonstrations.py:16` | `def label_intent(segment_frames):` |
| `wender/learn/demonstrations.py:29` | `def reproduced_share_ppm(segments):` |
| `wender/learn/explore.py:11` | `def _detect_stopstate(state):` |
| `wender/learn/explore.py:50` | `def explore(driver, program, max_steps=32, reset=None):` |
| `wender/learn/explore.py:110` | `def checked_observe():` |
| `wender/state/schema.py:31` | `def _no_float(o, path="$"):` |

The unchanged contract files still identify their own earlier snapshot at `docs/spec/WA_S3_REMEMBER_CONTRACTS.md:3` and `docs/spec/WA_S4_LEARN_CONTRACTS.md:3`. Their historical claims must be interpreted with that source binding.

## Journal migration boundary

A changed key encoding alone does not establish compatibility with pre-existing journals. Typed state keys distinguish values that the old truthiness/delimiter encoding collapsed, but cannot recover information already lost in both the old key and its recorded raw identity. No alias nodes are created for obsolete concatenated keys. The bounded read normalization below uses the existing raw fields as evidence rather than interpreting a delimiter string as a unique identity.

CURRENT `Store._read` still verifies record envelopes, sequence order, integrity hashes, and forbidden metadata before map replay. `Store._maps` calls `canonicalize_state_key` for each node's stored spelling and raw identity, validates its nonnegative exact observation count, and reconstructs the node only under its canonical typed key. Repeated observations keep their additive observation counts. The reader retains a temporary per-program table of the stored and canonical spellings observed so far. If either spelling would name a different typed identity, it rejects the record with `node identity conflict`; this includes sentinel/delimiter collisions and a legacy spelling that would shadow another canonical key.

For each edge, both endpoint spellings are resolved through that table and must identify already observed nodes. The reconstructed edge contains only typed endpoints. Unknown endpoints remain errors with the original journal spelling in the diagnostic, and malformed edge fields retain their earlier rejection stage. The table is discarded after replay and is not exported as graph nodes or persisted as aliases. Canonical typed and unambiguous legacy records may therefore be read together while the in-memory graph has one typed key for each raw identity.

Loading a map does not rewrite the journal. The existing append and compaction code uses the normalized map: new ProgramMap records contain typed keys, and explicit compaction serializes the reconstructed typed graph through its existing write path. This narrow read-boundary integration does not introduce a separate migration command, change skill-version ordering, weaken journal integrity checks, or repair missing/conflicting raw identity. Ambiguous histories are refused rather than silently merged, and the separate typed-edge append limitation below remains.

The reserved Store owner retains responsibility for any broader durable migration, historical-format detection, repair policy, and acceptance evidence beyond this bounded normalization. Tests of new metadata-only journal roundtrips do not alone establish arbitrary pre-existing journal compatibility. Existing versionless skill evidence likewise requires an explicit migration policy, not an inferred previous version.

## ABSENT and integration limits

**ABSENT:** the real F1 detector at the resolved repair base; semantic acceptance of its integration cannot be certified by injected test receipts. Also absent are a general migration/recovery path for ambiguous or incomplete legacy journals, a concurrent evidence-version allocator, operational `min_err` units and calibration, independently established support aggregation, arbitrary declarative pre/post/recovery execution, and detection of all pixel encodings concealed inside ordinary strings. The demonstrations model/reasoner/replay hooks remain explicit stubs.

The Store append path retains a separate typed-edge integration boundary: its merge uses Python equality before invoking ProgramMap replay. An offline metadata-only probe appended two separate observation batches with identical endpoints and changes, one action containing `{"flag": true}` and the other `{"flag": 1}`. Loading the map retained one Boolean edge. The in-memory canonical map distinguishes those records, but that does not repair this separate Store merge. The read-normalization change does not alter that append predicate. Further Store owner changes and their acceptance remain separate; the observed failure stays documented.

The repair preserves the plain legacy admission contract and explicitly identifies the stricter C-11 paths. Reference tests, promoted semantic production pins, remaining pins, unavailable dependencies, and Store integration failures must be reported separately. The existing gate is executed verbatim by the builder and independently by the orchestrator; a pull request is collection, not acceptance.
