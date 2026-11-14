# browserid/inject — serialised input per display (contract)

Source: ChatGPT task os-f-bid-03-injector a1 (code and tests from PR #83, outbox blocked by the platform); integrated
and reviewed locally by os-cu-0.

- `Injector.Submit(ctx, Request)` serialises actions per display: a per-display lane (binary semaphore) spans window
  activation, focus verification and the input itself; displays run in parallel.
- No Key/Type/Click is sent unless the Executor reports the target window focused after activation; otherwise the action
  fails closed (`focus_mismatch`) with no input sent.
- Requests are validated: keysym 1-32 of letters, digits, underscore, plus; text up to 2000 printable chars; clicks only
  inside the target window placement (tiling port), button 1..3; a placement change between plan and send fails.
- Bounded outstanding queue per injector (refuses when full); context cancellation and executor errors fail closed.
- Every action is recorded (`surface_id`, kind, `input_mono_ns` from the injected clock, `focus_verified`, result).
- The Executor port has typed methods only (no shell strings); production wiring to xdotool lives in the capsule lane.
Tests: 9 race-clean cases (ordering, parallel displays, focus mismatch, invalid keysym/text, click bounds, queue full,
placement change, cancellation, executor error).
