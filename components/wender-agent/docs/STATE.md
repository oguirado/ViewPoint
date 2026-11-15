# Wender STATE - `wender.state/v1`

The STATE is the only perception product that reaches a model. Raw AX trees or HTML never do
(a raw dump of ~50000 tokens per turn destroys prefix caching [CITED, HB-WA-a0-arch ML-a0-2]).

Machine schema: `wender/state/wender.state.v1.schema.json`. Code: `wender/state/schema.py`
(`build_state`, `validate_state`, `dumps`). Producer for web pages: `wender/perceive/browser.py`.

## Encoding
Canonical JSON: sorted keys, `(",", ":")` separators, UTF-8, integers only (a float is refused).
Equal pages give equal bytes.

## Top level
| Key | Type | Meaning |
|---|---|---|
| `schema` | const | `wender.state/v1` |
| `source` | object | `kind` (`cdp`, `atspi` or `godot`; `godot` = an in-engine UI-tree exporter such as Holdings T2, whose extra fields live in a separate extension block keyed by node id, never in the node), `url`, `title` (normalized like an AX name) |
| `prune` | object | `{"name":"prune_ax_v1","version":1}` |
| `ax_sha256` | hex64 | sha256 of the prune canonical blob, state attrs included |
| `ax_stable_sha256` | hex64 | same blob with attrs empty; stable across reloads and state flips |
| `viewport` | object/null | integers: `w`, `h`, `scroll_x`, `scroll_y`, `doc_w`, `doc_h`, `dpr_ppm` |
| `nodes` | array | emitted nodes in document order (below) |
| `actionable` | array | ids of actionable nodes, document order |
| `counts` | object | `raw` (AX nodes read), `nodes`, `actionable` |

## Node
| Key | Type | Meaning |
|---|---|---|
| `id` | hex16 | sha256(stable fingerprint + ordinal)[:16]; reload-stable, independent of CDP ids |
| `role` | string | AX role (chassis roles only, see `wender/perceive/ax.py`) |
| `name` | string | NFC, whitespace-collapsed, <= 64 UTF-8 bytes, `\` and `|` escaped |
| `depth` | int | emitted ancestors only |
| `parent` | hex16/null | nearest emitted ancestor |
| `ordinal` | int | position among nodes with the same role and name |
| `states` | object | subset of busy, checked, current, disabled, expanded, hidden, pressed, selected |
| `actionable` | bool | interactive role and not `disabled=true` |
| `box` | [x,y,w,h]/null | integer CSS px border box (DOM.getBoxModel); never hashed |

## Prune rules
`prune_ax_v1`, ported from the operator's OpenFresnel `companions/browser/ax.py`; the 8 rules are
restated in the header of `wender/perceive/ax.py`. Content inside `article` is opaque by design.

## Example size
The integration fixture page (27 raw AX nodes, 10 emitted) produces a 2148-byte STATE
[MEASURED, tests/test_integration_chrome.py fixture, Chrome 154 headless, 2026-10-19].
A size budget per STATE is TARGET (value AUSENTE).
