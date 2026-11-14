# Proto1 Area C — bounded full-search GM injection

Status: local plugin contract in eval/v2x/proto1_gm_budget.py, **not** an upstream GM export. Live bridge/integration commit read through the connector: f998fb90cec9b51b2d83991f1843e7d8773607eb. The task card names an older pin 4293420cac9b5a29f94ceb3f961b71c7447926f5; that was NOT the observed branch head.

Producer blob pins: per_window_detector.py 4872165afdb5c3aeeb6dd4112c97eaf45071caf5; gm_bands_hv.py 1f40bbcddd740305d7fd83286efb5eff0519fc09. Byte SHA-256 in the same order: 34a526659b19e10d1ad2ccbbd6cc60d6d45261484aa2656f04ff8b8f9127d87e, 4254e3c9a17b92d0a53f6c702e272431775f376d472b9df4bc7747b0989cdebe. Revalidate parity whenever either producer changes: this adapter calls source-pinned private production helpers.

## Public API and ordered fields

Construct BudgetedGM(max_ops=<exact nonnegative Python int>) with an EXPLICIT maximum configured from caller baseline evidence. No hidden fallback cap. Pass as PerWindowDetector(gm=BudgetedGM(max_ops=...), fl=..., veto=...), retaining real FL and veto defaults if desired. The adapter implements estimate_global_motion(prev_luma, cur_luma, max_d), returning native (dx:int,dy:int,score:int) or None; and consensus_tiles(prev_luma, cur_luma, s, dx, dy), returning a Boolean (gh,gw) array. Its OPS integer and OPS_DETAIL dictionary describe the **last individual** producer call, not a cumulative aggregate. reset_ops() clears context-local accounting and a pending estimate debit.

Existing ordered Handle fields: kind, canonical_id, generation. Supported kinds: cdp_tab, x11_window, xvfb_screen, capsule, godot_window. Existing ordered ChangeResult fields: handle, epoch, settled, claims, ops; ops keys are gm, fl. PerWindowDetector.update(handle, tiles, rgb, *, epoch, frame_seq) requires a real uint8 HxWx3 RGB ndarray, the exact visioncore.desc.TILE_DTYPE, Grid shape and Morton order, and exact nonnegative integer epochs / frame_seq; changing geometry requires a new epoch (per_window_detector.py:140-181). Repeated, skipped, or stale input cannot inherit previous lane claims.

The intended registered-runtime generation policy is generation >= 1. **External PRODUCTION_DEFECT (outside our allowed writes):** per_window_detector.py:33-37 permits generation 0. Keep legacy low-level wire representations distinct from runtime admission; this GM adapter cannot silently repair Handle admission. The detector copies and freezes RGB and its luma; its real RGB24 pixel digest is sha256(rgb.tobytes(order="C")) (per_window_detector.py:147-158,191-202). Status-only output or missing RGB cannot mint a frame here; the adapter has no frame, transport, send or publication API.

## Exact bounded accounting

The original GM source has per-call context-local OPS, OPS_DETAIL and reset_ops (gm_bands_hv.py:14-47), _inputs (100-111), _prepare (185-197), _verify (248-293), _decide (411-455), projection candidates (457-533), and public estimate/consensus (537-619). The adapter invokes the same original functions, with **no dropped or shortened hypotheses** and no alternative score, mode veto or displacement calculation. Its staged budget reservations precede the original 5*H*W initial estimator work, full 17*H*W preparation, complete bounded projection candidate generation (including up to eight region scans), and all full-resolution candidate verification. The latter ceiling includes informative pixels, both dense and packed SAD, both tile reductions per proposal, repeated winner verification, sparse exact boundary support, runner passes and full viewport certification. DENSE_SAD_BUDGET=9 only limits the dense reduction kernel; it never limits total verifier work.

At each stage the adapter checks existing counted work + maximum potential leg work <= caller cap **before** running that leg. It reclaims unused conservative allowance after observing the real producer OPS. Independently, consensus admission checks the finite ceiling 4*H*W + ceil(H/s)*s * ceil(W/s)*s before performing any dense or padded work, and debits the measured estimate from the same context if immediately paired with an estimate. The public helpers estimate_op_envelope(height,width,max_d) and consensus_op_envelope(height,width,tile_side) give conservative planning ceilings; neither sets a default cap. The adapter's private-source coupling and mathematical envelopes are version-pinned above.

Canonical upstream exception classes remain DetectorInputError (ValueError) and MissingEvidenceError (DetectorInputError). **Provisional plugin-local messages, not upstream exports:**

| Condition | Exception | Exact message |
| --- | --- | --- |
| No configuration | MissingEvidenceError | GM budget not configured |
| Bool, float, negative or non-exact int cap | DetectorInputError | GM budget must be an exact nonnegative integer |
| Cap too low | MissingEvidenceError | GM operation budget exhausted before PHASE |
| Producer exceeds proved bound | MissingEvidenceError | GM operation envelope violated in PHASE |

PHASE is exactly estimate, prepare, candidates, verification or consensus, whichever is refused. Producer validation errors retain their canonical text: "luma must be uint8"; "frames must have the same nonempty H by W shape"; "max_d must be an integer"; "max_d must be nonnegative"; "displacements must be integers"; "tile side must be a positive integer". A failed call clears the adapter-visible per-call OPS; internal partial counts never constitute a successful receipt. PerWindowDetector catches failures and invalidates only that handle's stream (per_window_detector.py:159-181). A normal None is an abstention, **never** a budget-exhaustion signal.

## Verification, host evidence and limits

Exact VM gate: python3 -m unittest tests.test_p1_vc_detector_budget_unit. Focused tests use actual NumPy, real GM source, real detector, actual S2 descriptors, FL and veto; verify native full-search decisions and both OPS call counts, explicit missing/bool and one-below budgets, invalid RGB/geometry/Morton/epoch/sequence, hashlib RGB digest, lane failure resetting only its history, and concurrent ContextVar isolation with fixed seeds, bounded barriers, joined threads and propagated failures. An independent pixel/count oracle covers zero-displacement consensus; two local admission/accounting mutation probes follow green controls. Record the actual test outcome and mutation kills in the outbox report, not as an assumed pass.

TARGET, **not VM or hardware proof**: four windows of 640x360, at most four perception groups, and negotiated one or two retained frames. Raw RGB24 arithmetic is 691,200 bytes per window-frame, at most 5,529,600 bytes for 4 windows x 2 frames; peak RAM also includes padding, descriptors, queues and streams and is AUSENTE. **AUSENTE**: host wall-clock timing, actual host peak RAM, GPU/capture or transport performance, a four-window group scheduler, and the producer generation-zero runtime fix. OPS and RAM estimates do not establish latency. The plugin and focused tests use RAM media, with no X, Docker, sockets, keys, disk frames, or external network.
