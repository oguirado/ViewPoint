# P1 Area C — generation-safe vision score join

Source: **oguirado/vision-engine**, ref **bridge/integration**, observed commit **f998fb90cec9b51b2d83991f1843e7d8773607eb**.
The task card references older commit bef2717c528c26e0426547a8d3c4d091afe2f748; it is not the observed head.

## Public contract

* **Handle(kind, canonical_id, generation)** admits a supported kind, nonblank ID and exact integer generation >= 1. The identity is the entire ordered triple. **canonical_handle_key** uses the unambiguous form **kind:ID_length:ID:generation**. Runtime registration refuses mixed or duplicate incarnations. Legacy adapter tuple and kind:ID inputs are read only for generation 1, not promoted into incarnation-independent registrations.
* Immutable **WindowEvidence** positional fields are **handle, scene, epoch, frame_seq, tp, fp, fn, tn, ops, frame_supported, dropped, exact, terminal, expected_terminal, seal, violations**. **TrialReport** stays **rows, outcomes, faults**; **Scorecard** stays **windows, pooled, metrics, violations, passed**.
* Rows bind full handle incarnation, nonnegative epoch, strictly increasing nonnegative sequence within a stream, exact nonnegative integer confusion counters, boolean status flags and a consistent terminal pair. Navigation/resize in **run_trial** must provide an explicitly newer epoch. Unregistered event targets do not create registrations.
* **FrameRef** admits copied, contiguous, immutable RGB24 pixels with the **real SHA-256 digest** of their bytes (64 lowercase hex characters). A 64-character digest is not exempt from checking. Status flags never produce pixels or prove native host capture. Bare numpy RGB arrays remain low-level **WindowFrames** fixtures rather than host attestations.
* **canonical_bytes(report)** uses UTF-8, compact JSON separators, sorted object keys and rows sorted by (kind, canonical_id, generation, scene, epoch, frame_seq). Diagnostic wall/host timing does not change identity. Bool counters, finite/infinite floats, stringified non-string dict keys, sets and unsupported objects are refused; Boolean flags and nulls remain valid JSON status types.

## Content-derived sealing

**seal_trial(TrialReport)** returns a new report whose rows carry the same SHA-256 of canonical row-set and manifest contents. Independent preimage construction:

1. Manifest: {"schema":"vqa.multiwin_manifest/v1","registered_handles":[...],"row_refs":[...],"outcomes":...,"faults":...}. Registered triples and row references are sorted; references include full incarnation, scene, epoch, frame_seq.
2. Payload: {"schema":"vqa.multiwin_evidence_seal/v1","manifest":manifest,"rows":[...]}. Include full rows in canonical identity order, temporarily replacing each seal with the empty string to break self-reference.
3. Digest: sha256(json.dumps(payload, sort_keys=True, separators=(",",":"), allow_nan=False).encode("utf-8")).hexdigest().

**score_windows** recomputes this full preimage and refuses arbitrary nonempty, copied or stale seals as **E_SEAL_MISMATCH**. **validate_scorecard** independently recomputes the witness using **Scorecard.pooled["seal_manifest"]**, and checks the pooled seal and row seals. This SHA-256 checksum is an integrity witness against *unresealed* modification; it is not an authenticated third-party signature or a proof of measurement provenance.

## Scoring, isolated replay and refusals

**score_windows** sums integer tp/fp/fn/tn *before* precision, recall, F1 and accuracy. Each mandatory metric carries exact numerator and positive denominator or **denominator=None, fraction=None, status="AUSENTE", passed=False**. The nine mandatory metrics are cross_window_bleed, safety_violations, frame_exactness, frame_drops, operation_totals, eligibility_stalls, starvation, terminal_completeness and pooled_confusion. No missing denominator can satisfy a mandatory gate. **validate_scorecard** catches altered pooled sums, averaged ratios and fabricated zeros.

**ops["isolated_ops"]** (or solo_ops) must be supplied from a separate production trajectory: **run_trial** never manufactures a comparator by copying interleaved counters. Missing/malformed comparators refuse **E_ISOLATED_OPS**; mismatches cause **operation_totals** and the overall gate to fail. A reported comparator cannot by itself prove that an external run was independent, so the tests run the actual solo trajectory and join its counters by full identity, epoch and sequence.

Typed refusal codes include **E_WINDOW_IDENTITY** (mixed/foreign handles), **E_FRAME_SEQUENCE** (sequence or RGB digest), **E_SEAL_MISMATCH** (modified canonical payload), **E_TERMINAL_VALIDATION** (invalid terminal pair), **E_ISOLATED_OPS** (absent independent counters), **E_CANONICAL_FLOAT** (any float in canonical evidence), **E_CANONICAL_TYPE** (unsupported type), and **E_MUTANT_POOL**, **E_MUTANT_AVERAGE_RATIO**, **E_MUTANT_NULL_ZERO** (forged Scorecard). Exception or false scorecard is never acceptance publication.

## Source coordinates and verification design

The bound **bridge/integration** implementation had canonical_handle_key at line 255 (generation omitted), FrameRef validation at lines 363–375 (conditional digest verification), WindowEvidence at line 645, score_windows at line 884 (string-equality seals near 944–948, isolated fallback 1182–1189), validate_scorecard at line 1502, and canonical_bytes at line 1686 (finite floats accepted near 1610–1614). The bound **eval/gui_synth/score.py** remains unmodified. Source multiwin SHA-256 was **762257327ec75d31eca5bcd63ffeff46bb4f3a852fd0cefe9b8f4e511b3fbbc2**.

Focused **tests.test_p1_vc_score_join_unit** uses real production **run_trial** for four handles with two events each, separately runs every target in isolation, joins explicitly supplied comparator counts and reconstructs an independent compact JSON + hashlib oracle for exact pooled values, canonical report bytes and content seals. Positive controls precede mutants. Negative controls include swapped IDs, ABA, tampered row/manifest, fake uniform seals, absent/unequal isolated comparators, averaged ratios, fabricated zero denominators, every mandatory metric gate, strict RGB pixel digest, geometry epoch changes, canonical bool/float/type refusals and no publication after refusal. A fixed-seed, bounded three-thread barrier test joins all threads and propagates failures without sleeping.

## VM, TARGET and AUSENTE

The required VM gate is **python3 -m unittest tests.test_p1_vc_score_join_unit**. The configured **bridge-gate** GitHub-hosted Ubuntu runner executes this exact command for the PR; its recorded output, not the existence of a commit, determines green. TARGET workload: four 640x360 windows, at most four perception groups, negotiated one or two retained frames. RAM bytes and operation budgets are not host latency. Native capture, measured host speed/latency, G-T4/G-T5 and host throughput remain **AUSENTE** until independently measured. The focused gate requires no X, Docker, network, sockets, keys, disk-frame output or external execution.
