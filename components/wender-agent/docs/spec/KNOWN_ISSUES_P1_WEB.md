# Known issues from the first real-site P1 runs (2026-10-19)

Source runs: P1 acceptance runs T1-T3 (internal acceptance campaign; logs not published).
- T1 used Google AI mode with a fresh profile.
- T2 and T3 used ChatGPT in the operator's logged-in Chrome over `wender/perceive/cdp_port.py`.

Each issue is pinned by an `expectedFailure` test in `tests/test_known_issues_p1_web.py`. A fix removes the decorator.

| ID | Issue | Observed | Wanted |
|---|---|---|---|
| WP1-01 | DOM selectors are fragile | The first ChatGPT waiter looked for `[data-message-author-role]`, which the current UI no longer renders. It counted 0 messages and never finished. | Waiters and extractors read the AX tree only (roles and names), never site CSS or data attributes. |
| WP1-02 | No AX completion signal | The "copy message" button of the USER turn triggered a premature "done". | A generic `wait_done(rules)` in perceive. For ChatGPT: no `button "Detener"`, and the last `heading "ChatGPT dijo:"` followed by its `button "Copiar mensaje"`. Rules come from data (site profile), not code. |
| WP1-03 | JS click does nothing on pointer-driven menus | `this.click()` on Radix `menuitem "Potencia"` did not open the effort slider. `Input.dispatchMouseEvent` at the box centre worked. | The executor tries a real pointer click (box centre, non-irreversible only) when a JS click leaves the AX snapshot unchanged. It also needs `press` and `insert_text` ops: T1 to T3 used raw `Input.*`. |
| WP1-04 | Extraction keeps UI chrome | The AX text dump of an answer ended with "ChatGPT puede cometer errores... Última respuesta... Respuesta completada". | `perceive.text.extract(region_start, region_end)` returns only the answer subtree: StaticText grouped by paragraph, heading, listitem, row and code, with a trailer cut from the site profile. |
| WP1-05 | Bot walls are not a state | Google returned `/sorry/` ("unusual traffic") on the first AI-mode query. | A STOP state detector: `/sorry/` URL, reCAPTCHA or Turnstile frames, "unusual traffic" text. It hands control back to the human; never solve or evade. |
| WP1-06 | One tab per tool, attach-only | The tools attach to an already-open tab; a missing tab returns rc 2 (MESH JQ1 S1 lost one attempt). Sites cannot run in parallel tabs. | A tab manager in `cdp_port`: `ensure_tab(url_prefix, open_url)` opens or reuses one tab per site through `Target.createTarget`, then navigates straight to the site route. Workers then use several tabs in parallel (one per site, one question per tab at a time) under a per-site lease. Operator idea, 2026-10-19. |


## Resolved: WA-RAMFIX — unbounded mocked CDP timeout loop

The operator reported 86 GiB RSS after approximately nine minutes [CITED,
WA-RAMFIX incident brief] while running the Python suite. The production harvest
test passed a seconds-valued float clock into CdpPipe, whose deadlines use integer
monotonic nanoseconds. Its mocked Condition.wait advanced that clock by the wait
argument (seconds), so a half-second deadline barely progressed; MagicMock kept a
call record on every iteration. A capped reproduction stopped after eleven waits
with only five integer nanoseconds of progress [MEASURED, bounded reproduction].

The test now separates the seconds clock required by HarvestBroker from an integer
nanosecond CDP clock. Its wait callback converts seconds to nanoseconds, permits
only one wait per command, and is installed directly without accumulating mock
call history. A negative control verifies that a repeated wait raises immediately.
CdpPipe._now_ns (also inherited by CdpPort) rejects non-integer clock values with
TypeError before send writes or waits; integer seconds cannot be distinguished
from nanoseconds by their value, so test-side loop bounds remain necessary.

The retention test preserves every original FIFO/buffer assertion. It additionally
checks that issued timed-out replies stay rejected, and exercises unsolicited
FIFO diagnostics on a separate connection where those identifiers were never
issued, consistent with the current transport contract.

Rule: every fake clock passed to CdpPipe/CdpPort must return monotonic nanoseconds;
mocked wait loops must be bounded. HarvestBroker's own clock still uses seconds.
Run timeout-loop tests in a hard memory-capped scope as an additional host guard.
