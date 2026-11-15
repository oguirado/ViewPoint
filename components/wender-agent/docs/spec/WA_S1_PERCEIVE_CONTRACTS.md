# WA S1 PERCEIVE contracts

## Source boundary

This specification records the code read through the GitHub connector from `example-owner/wender-agent` at explicit ref `bridge/integration`, observed before and after retrieval at commit `c0a8df9ec8809f38ab3f6d9d18f776a7cdfb39b9`. Citations refer to that snapshot. It is material for the operator to fold into HB-WA-s1-perceive; it does not amend the HyperBook or implement a finding.

Implemented behavior below is derived from the bound code. **AUSENTE** means the cited implementation does not establish the requested behavior. Configured timeout and retry values are code constants, not latency observations; no performance result is claimed. The vision bridge's RAM-only frame handling is an intended contract in its docstring, while both callable entry points raise immediately (`wender/perceive/vision_bridge.py:4`, `wender/perceive/vision_bridge.py:12`, `wender/perceive/vision_bridge.py:17`).

## Contracts

### CDP framing and transport

`frame(msg)` serializes with compact `(',', ':')` separators and `ensure_ascii=False`, encodes UTF-8, and appends one NUL byte. It does not sort keys or impose the STATE integer-only/object-shape restrictions; it delegates serialization to `json.dumps` (`wender/perceive/cdp_pipe.py:27`, `wender/perceive/cdp_pipe.py:29`).

`Deframer.feed(data)` appends bytes to its retained buffer, repeatedly splits at the first NUL, ignores empty chunks, and decodes each nonempty chunk as UTF-8 JSON. It returns all decoded messages completed by that call and retains the unfinished suffix. Frame-size limits, decoded-message shape validation, explicit end-of-stream flushing, and recovery after invalid UTF-8/JSON are **AUSENTE** (`wender/perceive/cdp_pipe.py:36`, `wender/perceive/cdp_pipe.py:39`, `wender/perceive/cdp_pipe.py:43`, `wender/perceive/cdp_pipe.py:48`).

The transport uses two OS pipes. The child setup duplicates the command-read endpoint onto fd 3 and the reply-write endpoint onto fd 4; Chromium is started with `--remote-debugging-pipe`. The constructed argument list has no debugging TCP-port option (`wender/perceive/cdp_pipe.py:54`, `wender/perceive/cdp_pipe.py:100`, `wender/perceive/cdp_pipe.py:103`, `wender/perceive/cdp_pipe.py:109`).

### CDP lifecycle

| Stage | Implemented contract | Source |
|---|---|---|
| Construction | `chrome_bin` uses the supplied truthy value, otherwise `CHROME_BIN`; no value raises `CdpError`. Defaults are headless mode and timeout `DEFAULT_TIMEOUT_S = 15`. Construction initializes counters, synchronization objects, reply storage, event storage, and lifecycle fields; it does not launch Chromium. | `wender/perceive/cdp_pipe.py:17`, `wender/perceive/cdp_pipe.py:19`, `wender/perceive/cdp_pipe.py:78`, `wender/perceive/cdp_pipe.py:94` |
| Arguments | `chrome_args(user_data_dir, headless=True, url='about:blank')` includes the user-data directory, no-first-run/default-browser-check, disabled GPU/extensions/background networking/sync, metrics-recording-only, and mute-audio flags; it conditionally appends `--headless=new`, then the URL. | `wender/perceive/cdp_pipe.py:52`, `wender/perceive/cdp_pipe.py:53`, `wender/perceive/cdp_pipe.py:65` |
| Start | Creates a short `wa`-prefixed temporary profile under `/tmp`, opens the pipes, starts the specified executable with standard streams redirected to `DEVNULL`, closes the parent's child-side endpoints, starts a daemon reader, and returns `self`. A readiness handshake and repeated-start guard are **AUSENTE**. | `wender/perceive/cdp_pipe.py:97`, `wender/perceive/cdp_pipe.py:99`, `wender/perceive/cdp_pipe.py:109`, `wender/perceive/cdp_pipe.py:117` |
| Read loop | Reads chunks of up to 65536 bytes. Under the condition lock, messages containing `id` go into `_replies[id]`; all other messages go into `events`; every delivery notifies waiters. EOF exits. `OSError` is swallowed; `finally` sets `_closed=True` and notifies waiters. Other parsing/shape exceptions are not caught by that `except`. | `wender/perceive/cdp_pipe.py:121`, `wender/perceive/cdp_pipe.py:125`, `wender/perceive/cdp_pipe.py:129`, `wender/perceive/cdp_pipe.py:135` |
| Close | If the stored process is alive, tries `Browser.close` with timeout 5, suppressing `CdpError`; waits up to 5; on `TimeoutExpired`, kills that stored process and waits up to 5 again. Then closes stored parent fds, suppressing `OSError`, clears fd fields, removes its profile with `ignore_errors=True`, and clears the profile field. The waits are separate configured limits. | `wender/perceive/cdp_pipe.py:142`, `wender/perceive/cdp_pipe.py:145`, `wender/perceive/cdp_pipe.py:149`, `wender/perceive/cdp_pipe.py:151`, `wender/perceive/cdp_pipe.py:153`, `wender/perceive/cdp_pipe.py:160` |
| Context manager | Entry calls `start()`; exit calls `close()` and returns `False`, so an exception from the context body is not intentionally suppressed. | `wender/perceive/cdp_pipe.py:164`, `wender/perceive/cdp_pipe.py:167` |

Reader joining, restart/reset of `_closed`, idempotent `start()`, cleanup on a failed `Popen`, and a single overall close deadline are **AUSENTE**. The close code also does not wrap its entire body in a final cleanup guard, so exceptions outside its named handlers can prevent later cleanup (`wender/perceive/cdp_pipe.py:97`, `wender/perceive/cdp_pipe.py:109`, `wender/perceive/cdp_pipe.py:142`, `wender/perceive/cdp_pipe.py:152`).

### Commands, timeouts, and the event buffer

`send(method, params=None, session_id=None, timeout=None)` increments the connection-wide id under `_lock` (the first id is 1), constructs `{'id', 'method', 'params'}` with `params or {}`, and includes `sessionId` only when truthy. Serialization and one `os.write` occur under that same lock; response waiting occurs after releasing it. Responses are correlated by id, not by a separate session/id pair (`wender/perceive/cdp_pipe.py:86`, `wender/perceive/cdp_pipe.py:173`, `wender/perceive/cdp_pipe.py:177`, `wender/perceive/cdp_pipe.py:181`, `wender/perceive/cdp_pipe.py:131`).

The reply deadline is `time.monotonic() + (timeout or self.timeout)` and is created **after** the write. The loop first checks whether its reply exists, then `_closed`, then remaining time, and waits on the condition. A zero override therefore selects the default; validation of timeout type/range is **AUSENTE**. Lock acquisition and pipe-write duration are outside that deadline, and the write return count is not checked (`wender/perceive/cdp_pipe.py:174`, `wender/perceive/cdp_pipe.py:181`, `wender/perceive/cdp_pipe.py:184`, `wender/perceive/cdp_pipe.py:186`).

`send` removes its matching reply. A reply containing `error` raises `CdpError` using its message; otherwise the method returns the `result` value, or `{}` if that key is absent. Write-side `OSError` and `TypeError` are wrapped as `CdpError`; other serialization failures have no wrapper here (`wender/perceive/cdp_pipe.py:182`, `wender/perceive/cdp_pipe.py:193`, `wender/perceive/cdp_pipe.py:194`, `wender/perceive/cdp_pipe.py:196`).

`events` is a deque with `maxlen=1024`: arrival order is retained up to that bound, with oldest entries evicted on overflow. `wait_event` searches from the front, matches exact `method`, and treats `session_id=None` as any session; a supplied session must equal the event's `sessionId`. The first matching event is deleted and returned in full. Already-buffered matches are returned before closed/timeout checks. Nonmatching events remain buffered. Overflow notification, event subscriptions, broadcast delivery to multiple waiters, and a sequence/cursor API are **AUSENTE** (`wender/perceive/cdp_pipe.py:18`, `wender/perceive/cdp_pipe.py:90`, `wender/perceive/cdp_pipe.py:133`, `wender/perceive/cdp_pipe.py:199`, `wender/perceive/cdp_pipe.py:203`, `wender/perceive/cdp_pipe.py:207`).

`_replies` has no corresponding size bound or expiry policy. A late reply after a caller times out, or an unsolicited id, is stored by the reader and is only removed if a `send` waiter later pops that id (`wender/perceive/cdp_pipe.py:89`, `wender/perceive/cdp_pipe.py:131`, `wender/perceive/cdp_pipe.py:191`, `wender/perceive/cdp_pipe.py:193`).

| Helper | Exact behavior | Source |
|---|---|---|
| `attach_page()` | Gets target information; uses the first target whose type is `page`, preserving returned order. If none exists, creates `about:blank`. Attaches with `flatten=True`, enables Page on that session, returns `sessionId`. Target selection by URL/title and waiting for a particular page are **AUSENTE**. | `wender/perceive/cdp_pipe.py:216`, `wender/perceive/cdp_pipe.py:218`, `wender/perceive/cdp_pipe.py:223`, `wender/perceive/cdp_pipe.py:224` |
| `navigate(session_id, url, timeout=None)` | Deletes all buffered `Page.loadEventFired` events, irrespective of session; sends `Page.navigate`; rejects truthy `errorText`; waits for a matching-session `Page.loadEventFired`; returns the navigation result. Send and event wait each receive the full timeout argument. | `wender/perceive/cdp_pipe.py:229`, `wender/perceive/cdp_pipe.py:231`, `wender/perceive/cdp_pipe.py:233`, `wender/perceive/cdp_pipe.py:236` |
| `evaluate(session_id, expression, timeout=None)` | Sends `Runtime.evaluate` with `returnByValue=True`; rejects truthy `exceptionDetails` with `CdpError`; returns nested `result.value`, or `None` when absent. Promise awaiting, object-handle resolution, and result type validation are **AUSENTE**. | `wender/perceive/cdp_pipe.py:240`, `wender/perceive/cdp_pipe.py:241`, `wender/perceive/cdp_pipe.py:243`, `wender/perceive/cdp_pipe.py:245` |

Navigation establishes only receipt of that event under its implemented filter. Loader-id correlation, DOM/network quiescence, same-document-navigation handling, and stale-event generation tracking are **AUSENTE** (`wender/perceive/cdp_pipe.py:229`, `wender/perceive/cdp_pipe.py:236`).

### AX acquisition and release

`raw_tree(cdp, session_id)` makes up to `AX_RETRIES=3` attempts. Each attempt first enables Accessibility, then calls `Accessibility.getFullAXTree`, returning `result.get('nodes') or []`. Any `Exception` from those calls is retried; there is a configured `AX_RETRY_WAIT_S=0.25` sleep only between attempts. Exhaustion raises `RuntimeError` containing the attempt count and last error text. This is broad exception retry, without classifying transient versus permanent failures, and no explicit timeout is passed to `send` (`wender/perceive/ax.py:54`, `wender/perceive/ax.py:55`, `wender/perceive/ax.py:76`, `wender/perceive/ax.py:83`, `wender/perceive/ax.py:91`).

`release(cdp, session_id)` sends `Accessibility.disable`, returning `True` on success and `False` on any caught `Exception`. It does not rethrow or retry. `raw_tree` does not call it, and `page_state` ignores its Boolean result (`wender/perceive/ax.py:94`, `wender/perceive/ax.py:97`, `wender/perceive/ax.py:99`, `wender/perceive/browser.py:53`).

### AX pruning: `prune_ax_v1`

The version marker is `PRUNE_NAME='prune_ax_v1'`, `PRUNE_VERSION=1`. Role and attribute membership are literal code sets/tuples, not inferred browser capabilities (`wender/perceive/ax.py:31`, `wender/perceive/ax.py:32`, `wender/perceive/ax.py:35`).

| Set | Exact members | Source |
|---|---|---|
| `CHASSIS_ROLES` | `RootWebArea`, `main`, `navigation`, `banner`, `contentinfo`, `complementary`, `region`, `search`, `form`, `dialog`, `alertdialog`, `toolbar`, `menu`, `menubar`, `menuitem`, `menuitemcheckbox`, `menuitemradio`, `tablist`, `tab`, `tabpanel`, `heading`, `button`, `link`, `textbox`, `searchbox`, `combobox`, `listbox`, `option`, `checkbox`, `radio`, `switch`, `slider`, `spinbutton`, `status`, `alert`, `log`, `progressbar`, `timer`, `marquee`, `article`, `note`, `group`, `application` | `wender/perceive/ax.py:35` |
| `OPAQUE_ROLES` | `article` | `wender/perceive/ax.py:45` |
| `STATE_ATTRS` | `busy`, `checked`, `current`, `disabled`, `expanded`, `hidden`, `pressed`, `selected` | `wender/perceive/ax.py:46` |
| `ACTIONABLE_ROLES` | `button`, `link`, `textbox`, `searchbox`, `combobox`, `listbox`, `option`, `checkbox`, `radio`, `switch`, `slider`, `spinbutton`, `menuitem`, `menuitemcheckbox`, `menuitemradio`, `tab` | `wender/perceive/ax.py:48` |

| Rule | Implemented contract | Source |
|---|---|---|
| Roots and traversal | Builds `by_id` from non-`None` node ids; duplicate ids overwrite earlier lookup entries. Roots are raw-list entries whose id is not in any `childIds`, retaining raw-list order; if no roots exist it falls back to the first raw entry. An explicit stack pushes children in reverse so traversal follows each `childIds` list in forward depth-first order. Unknown child ids are skipped. | `wender/perceive/ax.py:121`, `wender/perceive/ax.py:122`, `wender/perceive/ax.py:123`, `wender/perceive/ax.py:128`, `wender/perceive/ax.py:152` |
| Repeated nodes | A global `seen` set admits each encountered `nodeId` once, preventing repeated traversal of a cycle/shared id. No complete input-graph validation is performed. | `wender/perceive/ax.py:125`, `wender/perceive/ax.py:132` |
| Emission | Emits only if `not nd.get('ignored')` and the extracted role is in `CHASSIS_ROLES`. Missing/falsy `ignored` therefore passes; strict Boolean equality to `False` is not checked. Other nodes are traversed without emission. | `wender/perceive/ax.py:136`, `wender/perceive/ax.py:137`, `wender/perceive/ax.py:152` |
| Opaque content | An **emitted** article is appended, then its children are skipped. The opaque check occurs inside the emission branch, so an ignored article is traversed and can expose emitted descendants. | `wender/perceive/ax.py:137`, `wender/perceive/ax.py:148`, `wender/perceive/ax.py:152` |
| Emitted depth/ancestry | Roots start at depth zero with no ancestors. Depth increments and the role is appended to the ancestor tuple only after emitting a nonopaque node. Ancestors contain emitted roles, not names or ids. | `wender/perceive/ax.py:143`, `wender/perceive/ax.py:144`, `wender/perceive/ax.py:150`, `wender/perceive/ax.py:157` |
| Name | Extracts the AX value, then applies the normalization sequence below. | `wender/perceive/ax.py:103`, `wender/perceive/ax.py:141` |
| State attributes | Properties become a name-to-string map; duplicate property names take the last value. Only present `STATE_ATTRS` are emitted, in that tuple's alphabetical order. `focusable`, `focused`, and `invalid` are outside that tuple. | `wender/perceive/ax.py:46`, `wender/perceive/ax.py:103`, `wender/perceive/ax.py:114`, `wender/perceive/ax.py:142` |
| Ordinals and ids | After traversal, ordinal is zero-based across **all** emitted nodes with the same `(role, normalized name)`, not per parent. Cardinality is the final total for that pair. Every node then receives `stable_id(node)`. | `wender/perceive/ax.py:158`, `wender/perceive/ax.py:160`, `wender/perceive/ax.py:163` |

The emitted intermediate dictionary contains `role`, `name`, `attrs`, `depth`, `ancestors`, `node_id`, `backend_id`, then `ordinal`, `cardinality`, and `id`. `backend_id` is the raw `backendDOMNodeId` or `None`; it enables later box lookup. These intermediate dictionaries are not the final STATE nodes (`wender/perceive/ax.py:139`, `wender/perceive/ax.py:146`, `wender/perceive/ax.py:161`, `wender/perceive/ax.py:165`, `wender/state/schema.py:58`).

`_value` maps dictionary Boolean values to lowercase `true`/`false`, dictionary `None` to the empty string, other dictionary values through `str`, and nondictionaries to the empty string. `normalize_name` returns empty for falsy input; otherwise it applies `str`, Unicode NFC, whitespace collapse to one ASCII space, edge stripping, a 64-byte UTF-8 cut with incomplete code points discarded, then backslash and pipe escaping. The 64-byte bound is **before** escaping; the returned escaped string can be longer. No case folding or URL normalization occurs (`wender/perceive/ax.py:33`, `wender/perceive/ax.py:59`, `wender/perceive/ax.py:63`, `wender/perceive/ax.py:67`, `wender/perceive/ax.py:68`, `wender/perceive/ax.py:103`).

### AX re-resolution and irreversible eligibility

`describe(node)` returns `role`, `name`, `ordinal`, `cardinality`, `fp_exact`, and `fp_stable`. `match(nodes, entry)` reads role/name unchanged, converts `entry.get('ordinal') or 0` with `int`, and converts `name_variants or []` with `list`; it does not normalize supplied names or validate an entry schema (`wender/perceive/ax.py:239`, `wender/perceive/ax.py:241`, `wender/perceive/ax.py:252`).

For this table, `same` means current nodes with the exact recorded role and normalized-name string; `at_ord` is the subset with the requested ordinal. The first successful level returns immediately (`wender/perceive/ax.py:265`, `wender/perceive/ax.py:266`, `wender/perceive/ax.py:269`).

| Level | Candidate selection and stopping rule | Source |
|---|---|---|
| `L0_EXACT` | Starts with `at_ord`. If `fp_exact` is absent or `None`, any `at_ord` candidate succeeds and the result additionally contains `established=True`. Otherwise an `at_ord` candidate must have the recorded exact fingerprint. | `wender/perceive/ax.py:267`, `wender/perceive/ax.py:268`, `wender/perceive/ax.py:273` |
| `L1_STABLE` | Uses `at_ord` candidates whose stable fingerprint equals `entry.fp_stable`. | `wender/perceive/ax.py:276`, `wender/perceive/ax.py:277` |
| `L2_NAME` | Prefers `same` candidates at **other** ordinals. Only if there are none does it fall back to `at_ord`, even though its fingerprints failed above. | `wender/perceive/ax.py:280`, `wender/perceive/ax.py:282`, `wender/perceive/ax.py:284` |
| `L3_VARIANT` | Any node with the same role and a name contained in the supplied variants list. Variants are exact strings, not fuzzy-name expansion. | `wender/perceive/ax.py:254`, `wender/perceive/ax.py:286` |
| `L4_UNIQUE_ROLE` | Succeeds only if exactly one node of the recorded role exists. | `wender/perceive/ax.py:290`, `wender/perceive/ax.py:292` |
| No match | Returns `level=None`, `node=None`, `score=None`, `tie=False`, `candidates=0`, and the accumulated `ladder_tried`. | `wender/perceive/ax.py:294` |

Within a successful level, `_score` compares `role|name|stable fingerprint` using `difflib.SequenceMatcher`. It subtracts `min(0.05, 0.01 * ordinal_distance)`, clamps at zero, and rounds to six decimal places. Candidates sort by descending score and ascending ordinal, retaining input order for equal sort keys. The score does not move a candidate across levels or impose a minimum success threshold (`wender/perceive/ax.py:230`, `wender/perceive/ax.py:232`, `wender/perceive/ax.py:235`, `wender/perceive/ax.py:260`).

Successful results contain `level`, selected `node`, numeric `score`, `tie`, actual selected-level `candidates`, and `ladder_tried`; the missing-fingerprint L0 branch alone adds `established=True`. `tie` means the top two scores differ by strictly less than 0.05 and does not prevent returning a node. `ladder_tried` records levels reached up to success; its L0/L1 counts are pre-fingerprint `at_ord` counts, and its L2 count is the `others` count even when the fallback selects `at_ord` (`wender/perceive/ax.py:261`, `wender/perceive/ax.py:262`, `wender/perceive/ax.py:271`, `wender/perceive/ax.py:267`, `wender/perceive/ax.py:276`, `wender/perceive/ax.py:281`, `wender/perceive/ax.py:284`).

`level_allows_irreversible(level)` returns true only for the exact strings `L0_EXACT` and `L1_STABLE`; all lower levels, unknown strings, and `None` return false. This helper consumes only a level, with no tie, context, cardinality, provenance, or `established` check. `match` itself does not invoke `context_matches`, check the recorded cardinality, or gate an action. Enforcement by an executor is **AUSENTE**, because the bound action executor is a stub (`wender/perceive/ax.py:246`, `wender/perceive/ax.py:298`, `wender/perceive/ax.py:300`, `wender/act/actor.py:54`).

### Browser STATE and boxes

`page_state(cdp, session_id, with_boxes=True)` executes the following ordered steps: acquire raw AX; prune; evaluate JSON containing `location.href` and `document.title`; decode it; load/evaluate the viewport script and decode its JSON; optionally resolve boxes; call `ax.release`; build and validate the STATE. Title is AX-normalized, while URL is passed through. The reads are sequential, with no atomic-snapshot or page-mutation guard; cleanup is not in `finally` (`wender/perceive/browser.py:45`, `wender/perceive/browser.py:46`, `wender/perceive/browser.py:48`, `wender/perceive/browser.py:50`, `wender/perceive/browser.py:52`, `wender/perceive/browser.py:53`, `wender/perceive/browser.py:55`).

`inpage_script(name)` reads UTF-8 text from `INPAGE_DIR / name`; `INPAGE_DIR` is derived from the browser module's location. Script-name/path validation is **AUSENTE** in this helper (`wender/perceive/browser.py:12`, `wender/perceive/browser.py:15`).

`boxes` first sends `DOM.getDocument` with depth zero, even for an empty node list. It skips nodes without a backend id and requests `DOM.getBoxModel` for each remaining node. An exception from an individual request is swallowed. Only a truthy `model.border` with eight entries is converted. Missing boxes are omitted from the returned id-to-box map, then become `None` through `build_state`; `boxes` itself does not emit null entries (`wender/perceive/browser.py:26`, `wender/perceive/browser.py:28`, `wender/perceive/browser.py:31`, `wender/perceive/browser.py:35`, `wender/perceive/browser.py:38`, `wender/state/schema.py:67`).

The Python quad conversion takes min/max x/y coordinates, rounds the bounds with Python `round`, converts to integers, and computes nonnegative width/height from rounded edges. It therefore returns an axis-aligned integer border box in the units described by its code docstring, CSS px; it does not round an independently supplied width/height or apply viewport/scroll corrections. Initial `DOM.getDocument` errors and malformed successful model/quad values have no broad catch around them (`wender/perceive/browser.py:19`, `wender/perceive/browser.py:21`, `wender/perceive/browser.py:22`, `wender/perceive/browser.py:27`, `wender/perceive/browser.py:28`, `wender/perceive/browser.py:38`).

The assembled STATE includes schema id, source, prune marker, both AX hashes, viewport, nodes, actionable ids, and counts. Each final node contains exactly `id`, `role`, `name`, `depth`, `parent`, `ordinal`, `states`, `actionable`, and `box`. Parent is computed from the stack of emitted ancestors. A node is actionable only when its role is allowed and its `disabled` attribute is not the exact string `true`; the actionable list preserves output order. `with_boxes=False` passes an empty box map, producing null node boxes while still collecting viewport and AX (`wender/state/schema.py:11`, `wender/state/schema.py:50`, `wender/state/schema.py:53`, `wender/state/schema.py:58`, `wender/state/schema.py:65`, `wender/state/schema.py:66`, `wender/state/schema.py:69`, `wender/state/schema.py:77`, `wender/perceive/browser.py:52`).

`build_state` raises `StateError` if `validate_state` returns violations. The validator checks the declared top/node key sets, schema id, source kind, hash formats, integer viewport values, node ids/duplicates, parent membership in the ids accumulated so far, nonnegative depth/ordinal, string role/name/state values, Boolean actionability, box shape/nonnegative dimensions, actionable order, counts, and recursive absence of floats. A complete arbitrary-input schema validator, required viewport-key set, semantic prune validation, and URL/title normalization by `build_state` are **AUSENTE**; those fields are not fully validated by this function (`wender/state/schema.py:81`, `wender/state/schema.py:92`, `wender/state/schema.py:97`, `wender/state/schema.py:103`, `wender/state/schema.py:109`, `wender/state/schema.py:117`, `wender/state/schema.py:124`, `wender/state/schema.py:133`, `wender/state/schema.py:137`, `wender/state/schema.py:143`).

### AT-SPI discovery and `window_summary`

`bus_address(display=None)` first returns a truthy `AT_SPI_BUS_ADDRESS` environment value. Otherwise it copies the environment, optionally overrides `DISPLAY`, and runs `xprop -root AT_SPI_BUS` with a configured timeout of 10 seconds. `OSError`, subprocess errors/timeouts, nonzero exit, or failure to match the expected `AT_SPI_BUS(STRING)` text return `None`. Other bus-discovery paths are **AUSENTE** (`wender/perceive/atspi.py:25`, `wender/perceive/atspi.py:26`, `wender/perceive/atspi.py:28`, `wender/perceive/atspi.py:32`, `wender/perceive/atspi.py:38`).

The default `_call` runs `gdbus call` with the supplied address/destination/object path/method/arguments, with timeout 10 unless supplied otherwise. It returns stripped stdout for exit zero, otherwise `None`, including caught OS/subprocess errors. `parse_children` uses a regex over the supplied text or empty string, accepting unique bus names of the form matched by `:[0-9.]+` and optional `objectpath` tokens; it does not parse general D-Bus values (`wender/perceive/atspi.py:18`, `wender/perceive/atspi.py:42`, `wender/perceive/atspi.py:46`, `wender/perceive/atspi.py:49`, `wender/perceive/atspi.py:53`).

`Index(display=None, timeout=10, address=None, call=_call)` uses `address or bus_address(display)`, eagerly discovers registry children at `ROOT_PATH`, resolves each connection's PID through `GetConnectionUnixProcessID`, and appends parseable connections to `by_pid`. Missing address leaves an empty index. The lazy portion is per-PID detail lookup: `get(pid)` fetches `ChildCount`, `Name`, and `ToolkitName` for each connection only on first lookup, selecting the connection with the largest `(n_children or 0)`, and retaining the first on equality. Results, including a missing PID's `None`, are cached without refresh/invalidation. Truthiness and membership inspect `by_pid`, not a live bus probe (`wender/perceive/atspi.py:17`, `wender/perceive/atspi.py:57`, `wender/perceive/atspi.py:70`, `wender/perceive/atspi.py:78`, `wender/perceive/atspi.py:81`, `wender/perceive/atspi.py:85`, `wender/perceive/atspi.py:87`, `wender/perceive/atspi.py:90`, `wender/perceive/atspi.py:93`, `wender/perceive/atspi.py:101`, `wender/perceive/atspi.py:103`).

`window_summary(pid, idx, family_pids=None)` tries a direct PID first. Only when that lookup returns `None` does it try the first family PID present in the index, in supplied order; a registered but childless direct PID does not trigger family fallback (`wender/perceive/atspi.py:108`, `wender/perceive/atspi.py:112`, `wender/perceive/atspi.py:113`, `wender/perceive/atspi.py:115`).

| Outcome | Returned contract | Source |
|---|---|---|
| `pid is None` | `available=bool(idx)`, `tree=None`, `link='no_pid'`, reason `window has no _NET_WM_PID`; no rung field. | `wender/perceive/atspi.py:110` |
| No direct/family match | `available=bool(idx)`, `tree=None`, `link='no_match'`, explanatory reason; no rung field. | `wender/perceive/atspi.py:118` |
| Linked application | `available=True`, `tree=bool(n_children)`, `link='by_pid'` or `'by_family_pid'`, `bus`, `pid_atspi`, `name`, `n_children`, `toolkit`, and rung 2 if child count is truthy, otherwise rung 4. | `wender/perceive/atspi.py:112`, `wender/perceive/atspi.py:116`, `wender/perceive/atspi.py:121` |

The implementation uses truthiness, not a positive-count comparison. Unreadable/unparseable `ChildCount` becomes `None`, but a linked summary then reports `tree=False` and rung 4; negative parsed counts are truthy. PID linkage does not establish an exact window-to-accessible identity. Desktop tree traversal, STATE construction, and an action target handle are **AUSENTE** in this discovery module (`wender/perceive/atspi.py:20`, `wender/perceive/atspi.py:59`, `wender/perceive/atspi.py:62`, `wender/perceive/atspi.py:108`, `wender/perceive/atspi.py:121`).

### Perception rungs

`choose_rung(capabilities)` converts the argument to a set, visits `sorted(RUNGS)`, and returns the first rung whose exact capability string is present. It returns `None` if none match. Unknown capability names do not match; capability discovery, probing, execution, dynamic costs, retries, and escalation after failure are **AUSENTE** in this selector (`wender/perceive/ladder.py:21`, `wender/perceive/ladder.py:23`, `wender/perceive/ladder.py:24`, `wender/perceive/ladder.py:27`).

| Rung | `RUNGS` name | Required capability | Code's descriptive provider | Source |
|---|---|---|---|---|
| 1 | `structured` | `structured` | App JSON-L/API state, such as Blender bpy or MCP | `wender/perceive/ladder.py:11`, `wender/perceive/ladder.py:17` |
| 2 | `accessibility` | `accessibility` | CDP Accessibility tree or AT-SPI | `wender/perceive/ladder.py:12`, `wender/perceive/ladder.py:17` |
| 3 | `pixel` | `pixels` | vision-engine screen fingerprint and per-tile change/motion/flicker | `wender/perceive/ladder.py:13`, `wender/perceive/ladder.py:17` |
| 4 | `vision_model` | `vision_model` | Multimodal model on engine-selected crops | `wender/perceive/ladder.py:14`, `wender/perceive/ladder.py:17` |
| 5 | `blind_coordinates` | `pointer` | Coordinates without semantic confirmation | `wender/perceive/ladder.py:15`, `wender/perceive/ladder.py:17` |

These rows describe selection metadata, not proof that each provider is implemented. In particular, both vision-bridge callables below raise (`wender/perceive/ladder.py:10`, `wender/perceive/vision_bridge.py:12`, `wender/perceive/vision_bridge.py:17`).

### In-page viewport and vision bridge

`toInt(x)` performs JavaScript `Number(x)`, then `Math.round` for finite values or zero for nonfinite values. Exceptions thrown by numeric coercion are not caught. `rectToBox(r)` returns `null` for a falsy rectangle; otherwise it rounds x/y/width/height individually and clamps only width/height to zero. This is a different conversion from Python's rounded quad bounds (`js/inpage/viewport.js:8`, `js/inpage/viewport.js:10`, `js/inpage/viewport.js:15`, `js/inpage/viewport.js:17`, `wender/perceive/browser.py:19`).

`wenderViewport(win)` returns `w`, `h`, `scroll_x`, `scroll_y`, `doc_w`, `doc_h`, and `dpr_ppm`. Window dimensions and scroll offsets use `toInt`; document dimensions use the document element's scroll sizes or zero when the element is absent. Device pixel ratio is `(win.devicePixelRatio || 1) * 1000000` before `toInt`, so a falsy ratio defaults to one. General nonnegative bounds, safe-integer enforcement, and clipping are **AUSENTE**. A CommonJS guard exports all three functions; browser evaluation defines them in the evaluated script (`js/inpage/viewport.js:21`, `js/inpage/viewport.js:22`, `js/inpage/viewport.js:24`, `js/inpage/viewport.js:28`, `js/inpage/viewport.js:30`, `js/inpage/viewport.js:34`, `wender/perceive/browser.py:50`).

`screen_identity(frame_rgb)` and `changed_regions(prev_frame_rgb, frame_rgb)` always raise `NotImplementedError('vision-engine bridge is a v0 stub')`. Input frame layout, dtype, dimensions, ownership/lifetime, engine transport/entry point, crop schema, fingerprint algorithm, and return/error contracts beyond that exception are **AUSENTE**. RAM-only frames and engine-selected crops are explicitly intended/TARGET behavior in the source docstring, not an implemented pipeline (`wender/perceive/vision_bridge.py:4`, `wender/perceive/vision_bridge.py:6`, `wender/perceive/vision_bridge.py:12`, `wender/perceive/vision_bridge.py:17`).

## Failure modes

| Surface | Observable failure or limitation | Source |
|---|---|---|
| CDP construction/start | Missing executable selection raises `CdpError`; OS/process creation failures have no start-time wrapping or cleanup guard. | `wender/perceive/cdp_pipe.py:79`, `wender/perceive/cdp_pipe.py:99`, `wender/perceive/cdp_pipe.py:109` |
| CDP framing/read | Invalid UTF-8/JSON or non-object messages can escape the reader's `OSError` handler; `finally` marks the pipe closed. An unfinished EOF suffix has no flush/error report. | `wender/perceive/cdp_pipe.py:48`, `wender/perceive/cdp_pipe.py:126`, `wender/perceive/cdp_pipe.py:130`, `wender/perceive/cdp_pipe.py:135` |
| CDP request | Wrapped write failure, closed pipe while waiting, timeout, and protocol error become `CdpError`; no cancellation message or expired-reply eviction is sent. | `wender/perceive/cdp_pipe.py:182`, `wender/perceive/cdp_pipe.py:187`, `wender/perceive/cdp_pipe.py:190`, `wender/perceive/cdp_pipe.py:194` |
| CDP event/navigation/evaluation | Events can be evicted before waiting. Event waits can time out/observe closure; navigation can return `errorText`; evaluation can return `exceptionDetails`. Event absence does not distinguish overflow, wrong generation, or no event production. | `wender/perceive/cdp_pipe.py:90`, `wender/perceive/cdp_pipe.py:207`, `wender/perceive/cdp_pipe.py:210`, `wender/perceive/cdp_pipe.py:234`, `wender/perceive/cdp_pipe.py:243` |
| AX acquisition | Any caught exception retries the entire enable/read pair; exhaustion becomes `RuntimeError`; missing/falsy `nodes` is treated as an empty snapshot. | `wender/perceive/ax.py:85`, `wender/perceive/ax.py:86`, `wender/perceive/ax.py:87`, `wender/perceive/ax.py:91` |
| AX helpers | Shape/type validation is not a front-door contract. Pruning assumes dictionary-like nodes/properties; fingerprinting expects required fields; invalid ordinal conversion can raise; `match` returns diagnostic scores/ties rather than rejecting ambiguity. | `wender/perceive/ax.py:114`, `wender/perceive/ax.py:121`, `wender/perceive/ax.py:193`, `wender/perceive/ax.py:253`, `wender/perceive/ax.py:261` |
| Browser production | JSON/evaluation/script-read/initial-DOM failures propagate and may skip AX release. Per-node box request errors instead omit that box. Invalid assembled STATE raises `StateError`; no partial STATE result is returned by `page_state`. | `wender/perceive/browser.py:16`, `wender/perceive/browser.py:28`, `wender/perceive/browser.py:36`, `wender/perceive/browser.py:46`, `wender/perceive/browser.py:53`, `wender/state/schema.py:81` |
| AT-SPI | Default subprocess failures collapse to `None`; regex mismatch is treated as missing data; unknown children can become a childless summary; cache entries remain stale. Exceptions from a custom injected `call` are not generally caught by `Index` or `_detail`. | `wender/perceive/atspi.py:34`, `wender/perceive/atspi.py:47`, `wender/perceive/atspi.py:59`, `wender/perceive/atspi.py:78`, `wender/perceive/atspi.py:95`, `wender/perceive/atspi.py:121` |
| Rung/stub | No matching capability returns `None`; selected metadata does not execute a provider. Both vision functions raise immediately. | `wender/perceive/ladder.py:21`, `wender/perceive/ladder.py:27`, `wender/perceive/vision_bridge.py:13`, `wender/perceive/vision_bridge.py:18` |

## Determinism and hashing

The formulas below apply to the fields as supplied to the helpers. `canonical_line`, `canon`, and `fingerprints` do not themselves prune, normalize names, or reorder arbitrary caller-supplied attribute tuples (`wender/perceive/ax.py:169`, `wender/perceive/ax.py:174`, `wender/perceive/ax.py:179`, `wender/perceive/ax.py:191`).

| Product | Exact definition and dependencies | Source |
|---|---|---|
| Attribute text | Join input `(key, value)` pairs as `key=value`, separated by commas. Pruned nodes obtain alphabetical order from `STATE_ATTRS`; this helper does not sort. | `wender/perceive/ax.py:46`, `wender/perceive/ax.py:142`, `wender/perceive/ax.py:169` |
| Canonical line | Decimal `depth`, role, escaped name, and attribute text separated by `\|`: `depth\|role\|name\|attrs`. With `with_state=False`, only the final field is empty. | `wender/perceive/ax.py:174` |
| Canonical blob | Empty node sequence gives the empty string. Otherwise join canonical lines with newline and append a final newline. | `wender/perceive/ax.py:179` |
| `snapshot_sha` | Full lowercase SHA-256 hex of the canonical blob's UTF-8 bytes. `with_state=True` includes attributes; `False` excludes all attributes. | `wender/perceive/ax.py:71`, `wender/perceive/ax.py:186` |
| Fingerprint base | Ancestor roles joined with `/`, then NUL, role, NUL, normalized/escaped name. Depth is represented indirectly by the supplied ancestor-role path, not an explicit depth field. | `wender/perceive/ax.py:193` |
| Exact fingerprint | SHA-256 of UTF-8 `(base + NUL + attribute_text)`, truncated to the first 16 hex characters. | `wender/perceive/ax.py:194` |
| Stable fingerprint | SHA-256 of UTF-8 `(base + NUL)`, truncated to the first 16 hex characters. | `wender/perceive/ax.py:195` |
| `stable_id` | SHA-256 of UTF-8 `(stable_fingerprint_hex + NUL + decimal_ordinal)`, truncated to the first 16 hex characters. The input fingerprint is already truncated. | `wender/perceive/ax.py:200`, `wender/perceive/ax.py:203` |
| Final STATE serialization | `schema.dumps` first rejects nested floats, then JSON-serializes with sorted keys, compact separators, and `ensure_ascii=False`; it returns text, not bytes, and does not call `validate_state`. UTF-8 encoding is the caller's next step if bytes are required. | `wender/state/schema.py:25`, `wender/state/schema.py:27`, `wender/state/schema.py:28`, `wender/state/schema.py:31` |

AX snapshot hashes depend on emitted order, depth, role, normalized name, and optionally state attributes. They do not include raw CDP ids, backend ids, box coordinates, viewport, URL/title, final STATE parents, ordinals, cardinalities, or the generated ids. Per-node fingerprints additionally use emitted ancestor roles; stable ids add ordinal. Focus flags do not enter the pruned attribute tuple (`wender/perceive/ax.py:46`, `wender/perceive/ax.py:139`, `wender/perceive/ax.py:174`, `wender/perceive/ax.py:182`, `wender/perceive/ax.py:193`, `wender/perceive/ax.py:203`).

Consequently, a stable hash/id is stable under a state-attribute-only change when the emitted topology, role/name, ancestor roles, and relevant ordinal stay the same. It is not a universal identity guarantee across arbitrary reloads or edits: insertion of an earlier same-role/name node can change later ordinals, normalization/truncation can collapse names, and truncated hashes have no collision-resolution mechanism in `stable_id` (`wender/perceive/ax.py:63`, `wender/perceive/ax.py:67`, `wender/perceive/ax.py:160`, `wender/perceive/ax.py:195`, `wender/perceive/ax.py:203`).

`context_fingerprint(nodes, url_normalized, title)` returns the URL argument unchanged, SHA-256 of `title or ''`, a count of role-membership actionables, sorted unique roles, the stable snapshot hash as `chassis_sha256`, and prune version. It does not normalize URL/title. Its `n_actionable` includes disabled nodes whose roles are actionable, whereas final STATE actionability excludes `disabled='true'` (`wender/perceive/ax.py:207`, `wender/perceive/ax.py:209`, `wender/perceive/ax.py:211`, `wender/perceive/ax.py:213`, `wender/state/schema.py:66`).

`context_matches(recorded, now)` compares URL, title hash, and actionable count in that order, then compares role sets. It returns `(not broken, broken)` with field/before/now records. Chassis hash and prune version do not vote; missing matching scalar fields and empty/missing role lists can compare equal. Validation of a complete context record and enforcement before matching are **AUSENTE** (`wender/perceive/ax.py:218`, `wender/perceive/ax.py:221`, `wender/perceive/ax.py:224`, `wender/perceive/ax.py:227`, `wender/perceive/ax.py:246`).

Canonical STATE serialization does not promise identical bytes for snapshots differing in viewport, boxes, URL/title, counts, or node state. CDP framing is also not canonical STATE serialization, and match scores are floating-point diagnostic values outside the integer-only STATE schema (`wender/state/schema.py:28`, `wender/state/schema.py:69`, `wender/perceive/cdp_pipe.py:29`, `wender/perceive/ax.py:234`, `wender/perceive/ax.py:236`).

Attribute serialization does not escape commas or equals signs in values, while `_value` accepts arbitrary string content. Distinct attribute tuples can therefore produce identical canonical attribute text and identical stateful hashes/exact fingerprints. This is a serialization ambiguity, not evidence of a cryptographic SHA-256 collision; an accepted-value restriction that rules it out is **AUSENTE** in these helpers (`wender/perceive/ax.py:103`, `wender/perceive/ax.py:114`, `wender/perceive/ax.py:169`, `wender/perceive/ax.py:194`).

## Public symbols and anchors

Scope: every non-underscore definition/constant and public instance field defined by the S1 implementation files, their constructors/protocol methods, and the bound STATE builder/validator surface used by `page_state`. Imported library bindings and private helpers are excluded. A direct anchor is reported only when the source contains that symbol's anchor. **AUSENTE** does not invent an id from a nearby class anchor. Definition locations and anchor-comment locations are separate because the scanner records the latter (`tools/anchors.py:13`, `tools/anchors.py:33`, `tools/anchors.py:36`).

### `wender/perceive/cdp_pipe.py`

| Public symbol | Direct anchor id | Definition / anchor location |
|---|---|---|
| `DEFAULT_TIMEOUT_S` | AUSENTE | `wender/perceive/cdp_pipe.py:17` |
| `EVENT_BUFFER` | AUSENTE | `wender/perceive/cdp_pipe.py:18` |
| `CHROME_ENV` | AUSENTE | `wender/perceive/cdp_pipe.py:19` |
| `CdpError` | AUSENTE | `wender/perceive/cdp_pipe.py:22` |
| `frame` | `wa.perceive.cdp_pipe.frame` | `wender/perceive/cdp_pipe.py:27`; anchor `wender/perceive/cdp_pipe.py:26` |
| `Deframer` | `wa.perceive.cdp_pipe.Deframer` | `wender/perceive/cdp_pipe.py:33`; anchor `wender/perceive/cdp_pipe.py:32` |
| `Deframer.__init__` | AUSENTE | `wender/perceive/cdp_pipe.py:36` |
| `Deframer.feed` | AUSENTE | `wender/perceive/cdp_pipe.py:39` |
| `chrome_args` | `wa.perceive.cdp_pipe.chrome_args` | `wender/perceive/cdp_pipe.py:52`; anchor `wender/perceive/cdp_pipe.py:51` |
| `CdpPipe` | `wa.perceive.cdp_pipe.CdpPipe` | `wender/perceive/cdp_pipe.py:72`; anchor `wender/perceive/cdp_pipe.py:71` |
| `CdpPipe.__init__` | AUSENTE | `wender/perceive/cdp_pipe.py:78` |
| `CdpPipe.chrome_bin` | AUSENTE | `wender/perceive/cdp_pipe.py:79` |
| `CdpPipe.headless` | AUSENTE | `wender/perceive/cdp_pipe.py:82` |
| `CdpPipe.timeout` | AUSENTE | `wender/perceive/cdp_pipe.py:83` |
| `CdpPipe.proc` | AUSENTE | `wender/perceive/cdp_pipe.py:84` |
| `CdpPipe.user_data_dir` | AUSENTE | `wender/perceive/cdp_pipe.py:85` |
| `CdpPipe.events` | AUSENTE | `wender/perceive/cdp_pipe.py:90` |
| `CdpPipe.start` | AUSENTE | `wender/perceive/cdp_pipe.py:97` |
| `CdpPipe.close` | AUSENTE | `wender/perceive/cdp_pipe.py:142` |
| `CdpPipe.__enter__` | AUSENTE | `wender/perceive/cdp_pipe.py:164` |
| `CdpPipe.__exit__` | AUSENTE | `wender/perceive/cdp_pipe.py:167` |
| `CdpPipe.send` | `wa.perceive.cdp_pipe.CdpPipe.send` | `wender/perceive/cdp_pipe.py:173`; anchor `wender/perceive/cdp_pipe.py:172` |
| `CdpPipe.wait_event` | `wa.perceive.cdp_pipe.CdpPipe.wait_event` | `wender/perceive/cdp_pipe.py:199`; anchor `wender/perceive/cdp_pipe.py:198` |
| `CdpPipe.attach_page` | `wa.perceive.cdp_pipe.CdpPipe.attach_page` | `wender/perceive/cdp_pipe.py:216`; anchor `wender/perceive/cdp_pipe.py:215` |
| `CdpPipe.navigate` | `wa.perceive.cdp_pipe.CdpPipe.navigate` | `wender/perceive/cdp_pipe.py:229`; anchor `wender/perceive/cdp_pipe.py:228` |
| `CdpPipe.evaluate` | `wa.perceive.cdp_pipe.CdpPipe.evaluate` | `wender/perceive/cdp_pipe.py:240`; anchor `wender/perceive/cdp_pipe.py:239` |

### `wender/perceive/ax.py`

| Public symbol | Direct anchor id | Definition / anchor location |
|---|---|---|
| `PRUNE_NAME` | AUSENTE | `wender/perceive/ax.py:31` |
| `PRUNE_VERSION` | AUSENTE | `wender/perceive/ax.py:32` |
| `MAX_NAME_BYTES` | AUSENTE | `wender/perceive/ax.py:33` |
| `CHASSIS_ROLES` | AUSENTE | `wender/perceive/ax.py:35` |
| `OPAQUE_ROLES` | AUSENTE | `wender/perceive/ax.py:45` |
| `STATE_ATTRS` | AUSENTE | `wender/perceive/ax.py:46` |
| `ACTIONABLE_ROLES` | AUSENTE | `wender/perceive/ax.py:48` |
| `MATCH_LEVELS` | AUSENTE | `wender/perceive/ax.py:53` |
| `AX_RETRIES` | AUSENTE | `wender/perceive/ax.py:54` |
| `AX_RETRY_WAIT_S` | AUSENTE | `wender/perceive/ax.py:55` |
| `normalize_name` | `wa.perceive.ax.normalize_name` | `wender/perceive/ax.py:59`; anchor `wender/perceive/ax.py:58` |
| `sha256_text` | AUSENTE | `wender/perceive/ax.py:71` |
| `raw_tree` | `wa.perceive.ax.raw_tree` | `wender/perceive/ax.py:76`; anchor `wender/perceive/ax.py:75` |
| `release` | AUSENTE | `wender/perceive/ax.py:94` |
| `prune` | `wa.perceive.ax.prune` | `wender/perceive/ax.py:119`; anchor `wender/perceive/ax.py:118` |
| `canonical_line` | `wa.perceive.ax.canonical_line` | `wender/perceive/ax.py:174`; anchor `wender/perceive/ax.py:173` |
| `canon` | AUSENTE | `wender/perceive/ax.py:179` |
| `snapshot_sha` | `wa.perceive.ax.snapshot_sha` | `wender/perceive/ax.py:186`; anchor `wender/perceive/ax.py:185` |
| `fingerprints` | `wa.perceive.ax.fingerprints` | `wender/perceive/ax.py:191`; anchor `wender/perceive/ax.py:190` |
| `stable_id` | `wa.perceive.ax.stable_id` | `wender/perceive/ax.py:200`; anchor `wender/perceive/ax.py:199` |
| `context_fingerprint` | `wa.perceive.ax.context_fingerprint` | `wender/perceive/ax.py:207`; anchor `wender/perceive/ax.py:206` |
| `context_matches` | AUSENTE | `wender/perceive/ax.py:218` |
| `describe` | AUSENTE | `wender/perceive/ax.py:239` |
| `match` | `wa.perceive.ax.match` | `wender/perceive/ax.py:246`; anchor `wender/perceive/ax.py:245` |
| `level_allows_irreversible` | AUSENTE | `wender/perceive/ax.py:298` |

### Browser, AT-SPI, ladder, bridge, and JavaScript

| Public symbol | Direct anchor id | Definition / anchor location |
|---|---|---|
| `browser.INPAGE_DIR` | AUSENTE | `wender/perceive/browser.py:12` |
| `browser.inpage_script` | AUSENTE | `wender/perceive/browser.py:15` |
| `browser.boxes` | `wa.perceive.browser.boxes` | `wender/perceive/browser.py:26`; anchor `wender/perceive/browser.py:25` |
| `browser.page_state` | `wa.perceive.browser.page_state` | `wender/perceive/browser.py:45`; anchor `wender/perceive/browser.py:44` |
| `atspi.ROOT_PATH` | AUSENTE | `wender/perceive/atspi.py:17` |
| `atspi.bus_address` | `wa.perceive.atspi.bus_address` | `wender/perceive/atspi.py:25`; anchor `wender/perceive/atspi.py:24` |
| `atspi.parse_children` | `wa.perceive.atspi.parse_children` | `wender/perceive/atspi.py:53`; anchor `wender/perceive/atspi.py:52` |
| `atspi.Index` | `wa.perceive.atspi.Index` | `wender/perceive/atspi.py:67`; anchor `wender/perceive/atspi.py:66` |
| `atspi.Index.__init__` | AUSENTE | `wender/perceive/atspi.py:70` |
| `atspi.Index.timeout` | AUSENTE | `wender/perceive/atspi.py:71` |
| `atspi.Index.address` | AUSENTE | `wender/perceive/atspi.py:73` |
| `atspi.Index.by_pid` | AUSENTE | `wender/perceive/atspi.py:74` |
| `atspi.Index.__bool__` | AUSENTE | `wender/perceive/atspi.py:87` |
| `atspi.Index.__contains__` | AUSENTE | `wender/perceive/atspi.py:90` |
| `atspi.Index.get` | AUSENTE | `wender/perceive/atspi.py:93` |
| `atspi.window_summary` | `wa.perceive.atspi.window_summary` | `wender/perceive/atspi.py:108`; anchor `wender/perceive/atspi.py:107` |
| `ladder.RUNGS` | AUSENTE | `wender/perceive/ladder.py:10` |
| `ladder.CAPABILITY_FOR_RUNG` | AUSENTE | `wender/perceive/ladder.py:17` |
| `ladder.choose_rung` | `wa.perceive.ladder.choose_rung` | `wender/perceive/ladder.py:21`; anchor `wender/perceive/ladder.py:20` |
| `vision_bridge.screen_identity` | `wa.perceive.vision_bridge.screen_identity` | `wender/perceive/vision_bridge.py:12`; anchor `wender/perceive/vision_bridge.py:11` |
| `vision_bridge.changed_regions` | `wa.perceive.vision_bridge.changed_regions` | `wender/perceive/vision_bridge.py:17`; anchor `wender/perceive/vision_bridge.py:16` |
| `toInt` | `wa.js.inpage.viewport.toInt` | `js/inpage/viewport.js:8`; anchor `js/inpage/viewport.js:7` |
| `rectToBox` | `wa.js.inpage.viewport.rectToBox` | `js/inpage/viewport.js:15`; anchor `js/inpage/viewport.js:13` |
| `wenderViewport` | `wa.js.inpage.viewport.wenderViewport` | `js/inpage/viewport.js:21`; anchor `js/inpage/viewport.js:20` |

### Supporting STATE surface

| Public symbol | Direct anchor id | Definition / anchor location |
|---|---|---|
| `schema.SCHEMA_ID` | AUSENTE | `wender/state/schema.py:11` |
| `schema.SOURCE_KINDS` | AUSENTE | `wender/state/schema.py:12` |
| `schema.NODE_KEYS` | AUSENTE | `wender/state/schema.py:15` |
| `schema.TOP_KEYS` | AUSENTE | `wender/state/schema.py:16` |
| `schema.StateError` | AUSENTE | `wender/state/schema.py:20` |
| `schema.dumps` | `wa.state.schema.dumps` | `wender/state/schema.py:25`; anchor `wender/state/schema.py:24` |
| `schema.build_state` | `wa.state.schema.build_state` | `wender/state/schema.py:43`; anchor `wender/state/schema.py:42` |
| `schema.validate_state` | `wa.state.schema.validate_state` | `wender/state/schema.py:92`; anchor `wender/state/schema.py:91` |

## Findings

These are code-review findings, not fixes or assertions of production incidents. Each proposed test is a review recommendation; observed isolated probes and their limits are recorded separately in the task report.

| Id | Exact site and review finding | Proposed test |
|---|---|---|
| S1-F1 | `wender/perceive/cdp_pipe.py:181` ignores the write count; `wender/perceive/cdp_pipe.py:184` starts the deadline afterward. A partial write can leave a truncated frame, and blocked lock/write time is not bounded by the reply timeout. | Replace `os.write` with a deterministic short-write fake and assert complete framing would be required; separately hold/block the write path and check what deadline the contract actually bounds. No real browser is necessary. |
| S1-F2 | `wender/perceive/cdp_pipe.py:99` allocates before `wender/perceive/cdp_pipe.py:109` launches, without a cleanup guard; `wender/perceive/cdp_pipe.py:97` also has no repeat-start guard. Failed launch or repeated start can leave lifecycle resources unaccounted for. | Fake allocation and `Popen` failure, checking cleanup calls for every allocated handle/profile; call `start` twice with fake process objects and verify a deliberate lifecycle policy. |
| S1-F3 | `wender/perceive/cdp_pipe.py:131` stores arbitrary reply ids while timeout at `wender/perceive/cdp_pipe.py:191` has no late-reply cleanup. The event bound does not bound reply storage. | Feed late replies for timed-out ids and unsolicited ids into a fake reader stream; assert a defined storage/expiry limit once the operator chooses one. |
| S1-F4 | `wender/perceive/cdp_pipe.py:231` removes another session's buffered load event. The shared connection supports many sessions, but this purge is global. | Queue load events for sessions A and B, navigate A with a fake sender, and assert B's event remains available to B. |
| S1-F5 | `wender/perceive/browser.py:46` enables AX, but cleanup is only at `wender/perceive/browser.py:53`. Failure during pruning, metadata/viewport decoding, or initial box setup bypasses disable. | Use a fake CDP object whose viewport evaluation or `DOM.getDocument` fails after AX acquisition; assert disable is attempted and the original failure remains visible. |
| S1-F6 | Unparseable `ChildCount` becomes `None` at `wender/perceive/atspi.py:62`, then a linked summary converts it to `tree=False`/rung 4 at `wender/perceive/atspi.py:121` and `wender/perceive/atspi.py:123`. Unknown detail is conflated with a known childless application. | Return a successful registry/PID lookup but missing/malformed `ChildCount`; compare with a true zero count and require the chosen unknown-vs-zero contract to distinguish them. Also cover a negative parsed count. |
| S1-F7 | `wender/perceive/ax.py:268` establishes L0 without a recorded exact fingerprint; `wender/perceive/ax.py:300` permits irreversible eligibility based on that level alone. This is an identity-proof review risk; no actual action is executed by these functions. | Match a role/name/ordinal-only entry and inspect `established`; exercise the level helper; for any future executor, test that the approved provenance/tie/context policy is checked independently before irreversible dispatch. |
| S1-F8 | `wender/perceive/ax.py:281` records only `others` in `ladder_tried`, but `wender/perceive/ax.py:285` can return an `at_ord` candidate. A successful L2 result can report zero attempted candidates beside one selected candidate. | Supply one role/name/ordinal match with intentionally wrong fingerprints; assert the selected result and diagnostic count agree under the clarified telemetry contract. |
| S1-F9 | `wender/state/schema.py:121` adds the current id before `wender/state/schema.py:122` tests parent membership, so a self-parent passes that specific check despite the error message requiring a preceding parent. | Build an otherwise valid STATE, set a node's parent to its own id, and assert a parent-order violation; keep ordinary `build_state` output as a valid control. |
| S1-F10 | Public callable anchors are missing at, for example, `wender/perceive/ax.py:94`, `wender/perceive/ax.py:218`, and `wender/perceive/ax.py:298`. The existing uniqueness test checks discovered ids, while the scanner only records anchor comments; neither checks all public definitions. | Compare AST/public-definition inventory with discovered anchors, including `release`, `context_matches`, and `level_allows_irreversible`; require explicit policy for class methods/constants before expanding the gate. Sources: `tests/test_repo_conventions.py:32`, `tools/anchors.py:33`. |
| S1-F11 | `wender/perceive/browser.py:50` evaluates the complete viewport source, whose guard at `js/inpage/viewport.js:34` assigns `module.exports` at `js/inpage/viewport.js:35` when that target-context binding exists and is truthy. A page's existing export object can be replaced during perception. | Evaluate the script in an isolated JavaScript context with a truthy `module.exports` containing a sentinel, then test the agreed target-global preservation policy. No browser or filesystem frames are needed. |
| S1-F12 | `wender/perceive/ax.py:170` joins state values without delimiter escaping. An attribute `busy='a,checked=b'` and the pair `busy='a', checked='b'` serialize identically despite distinct state fields; the same ambiguity reaches exact fingerprints at `wender/perceive/ax.py:194`. | Compare both tuples' canonical lines, stateful snapshot hashes, and exact fingerprints, then require distinct encoding or a documented accepted-value rejection policy. This recommendation does not prescribe a fix. |

## Open questions

| Question | Current boundary / source |
|---|---|
| What are the supported CDP start/restart/close states and complete cleanup guarantees? | **AUSENTE** beyond the concrete methods; no start guard/reset/join is implemented (`wender/perceive/cdp_pipe.py:97`, `wender/perceive/cdp_pipe.py:142`). |
| Should timeout cover lock, write, retries, and navigation as one operation? What bounds frames, pending replies, and event loss? | **AUSENTE**. The clock begins after writing, navigation creates separate waits, replies/deframer lack bounds, and only events have `maxlen` (`wender/perceive/cdp_pipe.py:40`, `wender/perceive/cdp_pipe.py:90`, `wender/perceive/cdp_pipe.py:131`, `wender/perceive/cdp_pipe.py:184`, `wender/perceive/cdp_pipe.py:233`). |
| What defines one coherent page snapshot and fresh layout boxes? | **AUSENTE**. AX, URL/title, viewport, and boxes are separate reads with no generation check (`wender/perceive/browser.py:46`, `wender/perceive/browser.py:48`, `wender/perceive/browser.py:50`, `wender/perceive/browser.py:52`). |
| How should ignored opaque nodes, duplicate/missing ids, multiple roots, cross-origin frames, and shadow-root coverage be specified? | Current handling is the raw-tree call and prune traversal; dedicated coverage/normalization beyond that is **AUSENTE** (`wender/perceive/ax.py:86`, `wender/perceive/ax.py:121`, `wender/perceive/ax.py:123`, `wender/perceive/ax.py:137`, `wender/perceive/ax.py:148`). |
| Are name limits before/after escaping, id collisions, and global ordinal shifts acceptable identity semantics? | Normalization cuts before escaping; ids truncate hashes and include global pair ordinals. An alternate collision or identity policy is **AUSENTE** (`wender/perceive/ax.py:67`, `wender/perceive/ax.py:68`, `wender/perceive/ax.py:160`, `wender/perceive/ax.py:203`). |
| Must context checks compare chassis hash/prune version, and should disabled nodes count as actionable? | Current context matching omits those fields and uses a role-only count; a unified policy is **AUSENTE** (`wender/perceive/ax.py:211`, `wender/perceive/ax.py:221`, `wender/state/schema.py:66`). |
| What evidence must accompany an L0/L1 irreversible decision? | Tie/provenance/freshness/authorization gating is **AUSENTE** in the level-only helper and stub executor (`wender/perceive/ax.py:268`, `wender/perceive/ax.py:298`, `wender/act/actor.py:54`). |
| How should AT-SPI unknowns, stale caches, family selection, and desktop STATE production behave? | Unknown detail can become false; cached results have no refresh; family fallback takes the first indexed PID; traversal-to-STATE is **AUSENTE** (`wender/perceive/atspi.py:62`, `wender/perceive/atspi.py:95`, `wender/perceive/atspi.py:115`, `wender/perceive/atspi.py:121`). |
| Should Python quad rounding and JavaScript rectangle rounding use a unified coordinate contract? | They currently round different inputs with different runtime operations. Cross-path equivalence, clipping, and safe-integer bounds are **AUSENTE** (`wender/perceive/browser.py:21`, `js/inpage/viewport.js:10`, `js/inpage/viewport.js:17`). |
| What are the frame/crop/engine interface, memory ownership, and output schema for rungs 3-4? | **AUSENTE**; both bridge entry points raise and their docstring marks the intended RAM-only integration as TARGET (`wender/perceive/vision_bridge.py:4`, `wender/perceive/vision_bridge.py:12`, `wender/perceive/vision_bridge.py:17`). |
| What STATE size budget, consumer truncation rule, and full validation policy should apply? | **AUSENTE** in the builder/dumper: neither caps serialized size; validation implements the checks listed above, not all semantic invariants (`wender/state/schema.py:25`, `wender/state/schema.py:43`, `wender/state/schema.py:92`). |
