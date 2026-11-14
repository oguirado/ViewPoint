# GUI synthetic benchmark preregistration

The machine-readable seal is PREREG.json, schema `vqa.gui_synth.prereg/v1`.
`python3 -m eval.gui_synth.prereg seal` refuses until FA and FC post DONE.
Existing seals require `seal --reseal --reason <text>`. FXP1 waits for FA and FC
DONE; the original e738181 seal remains the pinned first history entry. Each
reseal appends its predecessor SHA, new SHA, source-tree SHA, reason, and
content-linked entry digest to SEAL_HISTORY.jsonl. Verify rejects a broken
chain, a last-entry mismatch, and unknown prereg references in result evidence
(including gzip JSON).

JP1 authorizes the third link with reason `JP1 fix: FLICKER strata + split-scoped claims`
only from the exact judged FXP1 predecessor `bd3896cd5cf5379ae7230ceaba30e6f9f945a0fb6980b1a83cb8df29b179e089`.
This transition does not depend on the historical FA/FC global-status pulses.

Seal hashes all generator, label, split, runner, scorer and sign-flip sources.
`python3 -m eval.gui_synth.prereg verify` rejects any source or partition drift.

DEV seeds are [0,64), GATE [1000000,1000032), TEST [2000000,2000032)
[CITED: CARDS_P1]. Tuning uses DEV exclusively. GATE and TEST require a valid
seal. Captures with identical SHA across splits are refused. The seal records
the SHA of the complete DEV frame-hash table at the bulk resolution.

The claim unit is the (kind, parameter value) stratum mean. Scene-level p is
descriptive. Comparisons filter scene ids by the requested split prefix; claims
are allowed only for explicit GATE or TEST comparisons. DEV and pooled
comparisons always report "no claim". Split-owned parameter sets are recorded in the seal; velocities
are integer sixteenths of the grid size. DEV, GATE, and TEST use disjoint tuned
parameter values; FPS remains the common structural axis. Default blink FPS
is selected per seed, while an explicit runner --fps overrides it.

Alpha is 1/20 [CITED: CARDS_P1].
Sign-flip testing is two-sided: exact through 20 nonzero pairs, otherwise
100000 seeded draws [CITED: eval/hb_router/compare.py]. Primary metrics are
MOVED recall for scrolling, FLICKER recall for blink, and exact motion for
caret movement/typing. Guards compare static/jitter false positives,
CHANGED recall, and engine runtime with the preregistered v1 baseline.
FROZEN and DRIFT are unexercised. Synthetic glyphs do not validate Chromium.

Frames are generated scene-by-scene in a bounded `vadapt.ramws` workspace,
tool `gui_synth`, under `/dev/shm/vqa-ramws/`. Result JSON, optional label
JSONL and hash evidence may persist; frame pixels may not. Workspace context
exit and signal cleanup use the existing RAM workspace lifecycle. Benchmarks
use CPUs 5-9 and at most 5 workers [CITED: CARDS_P1].

TEST is refused unless `--engine-version v2 --candidate <id>` accompanies
`--unseal-test <current PREREG.json sha>`. The runner locks and appends
`eval/results/gui_synth/test_access.jsonl` before the first TEST capture;
repeated access by that candidate is refused, including retries after failure.
No TEST access is needed for this wave.

The mandate is limited to the supplied P1 card and repository corpus.
