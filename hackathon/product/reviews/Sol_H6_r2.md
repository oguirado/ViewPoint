# Cross-Family Review: Sol 6.1 (`gpt-6.1-sol`) on `H6_r2` (`swarm/docs/` + `README.md`)

- **Model**: `gpt-6.1-sol` (`seat=sol`, `wave_id=58772a4c1bc34eb9`)
- **Pack SHA-256**: `None`

The supplied corpus supports **0 BLOCKER and 1 LIVE_PATH_MAJOR** [CITED: findings below]. The remaining major issue is a false approval claim about `Sol_H5_r1`.

| Classification | Exact location | Finding and reasoning |
|---|---|---|
| LIVE_PATH_MAJOR | `swarm/docs/ONE_PAGER_TEAM.md:36`; `README.md:112` | Both name `product/reviews/Sol_H5_r1.md` and `product/reviews/Sol_H5_r2.md`, then describe them collectively as `APPROVED`, `0` blockers, `0` live-path defects. `PROVENANCE.md:72` explicitly records `Sol_H5_r1` as `REJECTED` with `4 LIVE_PATH_MAJOR`, receipt `wave_id: ac78e147466040a6`; only `Sol_H5_r2`, receipt `wave_id: 77367a51d8914d6c`, is `APPROVED`. The prose must distinguish the initial rejection from the successful repair review. |
| BACKLOG | `swarm/docs/test_docs.py:277–279`; `swarm/docs/test_docs.py:295–297` | `validate_proof_index` checks whether filenames appear in `PROVENANCE.md`; it does not compare their recorded hashes. Thus the D2 description at `swarm/docs/test_docs.py:397` overstates the automated provenance check. The supplied hashes agree manually, but the previously demonstrated provenance-hash mutation remains undetected. |
| BACKLOG | `swarm/docs/test_docs.py:260–261`; `swarm/docs/test_docs.py:693–700` | Minimum and per-track row counts do not establish proof-set equality or uniqueness. Replacing a missing proof row with a duplicate can preserve the expected counts. This leaves part of `P4f_H6.md` finding `BL-8` unresolved. |
| BACKLOG | `swarm/docs/OPERATOR_CHECKLIST.md:85–93` | The instruction promises deployed URLs **and service configuration**, but the receipt example writes placeholder URLs and a date only. Since `swarm/docs/OPERATOR_CHECKLIST.md:43` uses receipt existence to retire `[TARGET]`, following the example literally could produce a misleading deployment receipt. This also leaves the placeholder concern from `P4f_H6.md` `BL-9` unresolved. |

The classifications and counts above are derived from the supplied text, without tool execution.

**Prior review corrections**

`P4f_H6.md` `LP-1` is resolved: `swarm/docs/ONE_PAGER_TEAM.md:6` now gives kernel `7.0.0-1019-nvidia`, matching the supplied host receipt. `LP-2` is resolved at `swarm/docs/ONE_PAGER_IDEA.md:52` and `README.md:132`: the audit command is `projector.py --finalize`, including the module invocation `python3 -m swarm.projection.projector --finalize` in IDEA.

The requested `T038` `OVERCLAIM` / `STALE` corrections are present:

- Problem framing removes “historically” at `swarm/docs/ONE_PAGER_IDEA.md:5`.
- Broker claims describe checking and logged spill at `swarm/docs/ONE_PAGER_IDEA.md:12` and `README.md:103`.
- Projection is explicitly best-effort at `swarm/docs/ONE_PAGER_IDEA.md:19`.
- Orchestrator naming is corrected at `swarm/docs/ONE_PAGER_IDEA.md:24`.
- Gemini authorship uses cited proofs at `swarm/docs/ONE_PAGER_IDEA.md:25`.
- Take results disclose `JOB_ERROR`, `VERIFIED_WITH_SPILL`, preserved `Cube`, and `CLEAN` with anomalies/uncertain-event counts at `swarm/docs/ONE_PAGER_IDEA.md:49–61` and `README.md:130–137`.
- Host wording and scope policy are corrected at `swarm/docs/ONE_PAGER_TEAM.md:6–7`.
- Authorship separation, housekeeping commit `f667745`, and qualified review philosophy appear at `swarm/docs/ONE_PAGER_TEAM.md:12`, `:39`, `:43`, and `swarm/docs/PROOF.md:37`.

The requested `T042` corrections are resolved: licence wording appears at `swarm/docs/ONE_PAGER_IDEA.md:36` and `swarm/docs/ONE_PAGER_TEAM.md:8`; TaskLight (`context-wall`) remains a companion projection target at IDEA `:19`, `:44`; orchestrator naming is corrected at IDEA `:24`; Cloud Run remains `[TARGET]` at IDEA `:30` and `swarm/docs/OPERATOR_CHECKLIST.md:41–43`.

**Proof and integrity checks**

All **42 indexed `.jsonl` hashes and 2 indexed review-report hashes** [CITED: `swarm/docs/PROOF.md:11–33`, `:41–59`, `:67–68`] match the supplied on-disk SHA-256 receipts and corresponding `PROVENANCE.md` entries.

The review receipts are:

- `product/reviews/Sol_H5_r1.md`: `1af95609eb60082c2484fc95a32e88783975f85d695822dc1c0b24bfec896f3e`
- `product/reviews/Sol_H5_r2.md`: `d10dc06d12a18f66e66d36c18bad77b462abee3982b59c0b3098d47e7d3e7bd8`

The design-input hashes at `swarm/docs/PROOF.md:76–85` match `PROVENANCE.md:24–33`. Their current individual on-disk receipts are **AUSENTE — no inventar** in the supplied re-hash list; the historical `P4f_H6.md` audit reports their successful verification.

The expected self-log omission is accepted: `swarm/proof/H6_r2_1527.jsonl`, final SHA-256 `de070cd4dcce5dcfb005cd78c4f982fc3ab58088d95eb012d54c4cecaf205a27`, could not be finalized inside its own generating stream.

`HONEST_BOUNDARY_STATEMENT` is verbatim at `swarm/docs/ONE_PAGER_IDEA.md:40` and `README.md:116`. `PROJECTION_GUARANTEE_STATEMENT` is verbatim at IDEA `:44` and README `:118`.

The reviewed Markdown and README hackathon section contain **0 banned-phrase matches** [CITED: inline text and supplied suite]. Every original heading listed at `swarm/docs/test_docs.py:52–62` remains in `README.md`. The supplied suite reports **16 passing tests, rc=0** [CITED]; these passes do not detect the false review-status claim.

This verdict is confined to the supplied inline corpus.

**VERDICT: REJECTED**
