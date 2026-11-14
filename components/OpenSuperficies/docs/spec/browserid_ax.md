# Identity accessibility shim

The identity Chromium parent is `python3 -I /usr/local/bin/ax-shim.py`.
It adds the sole `--remote-debugging-pipe` flag and wires Chromium read/write
file descriptors 3/4 [CITED: launch implementation]. There is no TCP debugging
listener. The existing sandbox/profile/proxy flags stay on the browser launch.
Later Chromium commands reuse that browser without acquiring a debugging pipe.

The service binds `/run/newton-tenant/ax.sock`, mode 0600, under uid 10001
[CITED: main implementation]. Its single operation accepts one newline JSON
object, closes the connection after one newline JSON response, and never
accepts client CDP methods:

```json
{"op":"ax","window_title_or_index":"Exact title - Chromium","frame_seq":7,"text_container":"0123456789abcdef01234567"}
```

The sequence is the caller's existing frame sequence, echoed without allocating
or capturing a frame. `text_container` is optional. All other keys are refused.
The native title must resolve uniquely in the entrypoint's identity window map,
and must match exactly one page target. Logical window ids and zero-based
indices are also accepted internally. Changed launch geometry refuses.

Success is the bare `window,frame_seq,scroll,elements,truncated,skipped_frames` object consumed
by `srf-identity-surface ax`. Text mode additionally returns
`text:{container_id,bytes,text}`. Errors contain only `error` and `code`:
readiness 4, malformed framing/pipe data 5, operation/privacy refusal 6
[CITED: snapshot/main implementation]. No response contains a `value` key.
Textbox/combobox `value_len` counts Unicode code points (not bytes or grapheme
clusters), only after privacy checks. Password,
credit-card and one-time-code fields, including descendants, are omitted. This
includes `type=password`, autocomplete `current-password`, `new-password`,
`one-time-code`, and every token starting `cc-`, even in a multi-token list;
requesting text of their ancestor refuses. Hidden nodes are excluded. Text is
visible static AX leaf text in document order, without editable values, bounded
to 64 KiB [TARGET]; UTF-8 truncation preserves complete code points.

Policy is sealed by `ax-allowlist.json` and a matching independent code set.
Browser calls are Target.getTargets and Target.attachToTarget; only the unique
page selected from this identity's native window may be attached, with flatten.
Session methods are only Accessibility.enable/getFullAXTree,
DOMSnapshot.captureSnapshot and Page.getLayoutMetrics. Snapshot styles are exactly display,
visibility, opacity. All other methods and unbound sessions refuse. A policy,
framing or browser-response violation poisons the pipe and terminates the
browser process group. High-level ambiguous selectors or sensitive text return
a refusal without granting raw transport. Logs carry fixed events and counts.

Messages are bounded to 16 MiB, elements to 300, names to 120 Unicode code points
[TARGET]. Ids are SHA-256 prefixes of logical window id and backend DOM node id.
Nodes follow DOMSnapshot document order. Above the cap, actionable nodes rank
first, then descending clipped area. Landmark region and repeated container
ancestry come from the document tree and AX roles. Checked mixed maps to null;
boolean checked strings normalize to booleans. Applicable focus/enabled states
are nullable booleans.

## Geometry boundary and validation scope

Identity Openbox windows have no decorations; device scale is forced to one.
Every snapshot measures Page CSS visual viewport width/height and the current
native rectangle. For an equal-width, unzoomed viewport, its origin is native
x=0 and y=native height minus viewport height. DOMSnapshot document boxes are
translated by document scroll and this measured chrome offset, clipped inward
to the viewport, and returned in local window pixels. Unequal widths, viewport
pan/zoom, changed geometry and scrollbar-dependent width differences refuse.
Only the first DOMSnapshot document (the main frame) supplies nodes, text and
geometry. Other documents and their AX backend nodes are skipped, for same-origin
and cross-origin frames alike. `skipped_frames` counts additional captured
documents plus main-document iframe/frame owners without a captured document.
No child-frame offsets or content are projected. An ancestor chain exceeding
256 nodes [TARGET] returns refusal 6 [CITED: map_snapshot], without poisoning. No fixed toolbar-height guess is used.

Frame/AX temporal atomicity is AUSENTE — no inventar. The service echoes the
current frame sequence supplied by the client; page animation or scrolling can
occur between RAM capture and accessibility sampling. Real Chromium geometry
and title decoration have not been exercised by this offline lane.

`python3 -B scripts/identity/test_ax_shim.py` uses socketpair fake CDP peers and a
short-lived Python child to verify actual descriptor wiring. It exercises
mapping, privacy, clipping, limits and full collection without Chromium.
`python3 -B scripts/identity/test_image.py --selftest` checks image wiring,
closed methods, projection keys and closed logging AST, including forbidden
Runtime/Network, value disclosure and name-logging mutants. Image recipe seals
include shim, policy, tests and this contract; rebuilding needs the updated
recipe approval under the existing image build procedure.

## URL privacy and recovery

Public `window.url` is HTTP(S) `scheme://host[:port]/path`, without userinfo,
query or fragment; invalid or non-HTTP(S) locations become empty. HTTP(S) tokens
inside the public title receive the same filtering. Raw native titles remain
private selectors. The Go client independently filters URL/title metadata;
frame metadata derived from titles follows the same rule.

Each CDP call has a 0.5-second deadline [TARGET], below the client socket's
5-second window [TARGET]. A fully written request timing out returns readiness
4 [CITED: Pipe.call], retains its id/session, and permits a subsequent request.
Late replies for those exact ids are discarded; events are discarded too.
Unknown/future/duplicate reply ids, session mismatches, malformed data, EOF and
partial-write timeouts poison the pipe (5) [CITED: Pipe.call]. At most 16 expired
ids [TARGET] may await draining; exhaustion poisons. Unexpected exceptions reply
with fixed `malformed`/5 and poison [CITED: respond].

A poisoned shim terminates its browser group and exits. The capsule must be
restarted through its existing lifecycle to create a fresh browser/pipe; no
automatic in-place browser restart or retry is performed. A timeout while
attaching can leave an unused browser session until lifecycle restart.
Offline regressions: `python3 -B scripts/identity/test_ax_review.py`.

This mandate is limited to the supplied repository and campaign corpus.
