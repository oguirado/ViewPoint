# Cross-Family Review: Sol 6.1 (`gpt-6.1-sol`) on `H6_r3` (`swarm/docs/` + `README.md`)

- **Model**: `gpt-6.1-sol` (`seat=sol`, `wave_id=81733408ae884aa9`)
- **Pack SHA-256**: `None`

**H6_r3 resolves the review-status defect. Remaining findings are BACKLOG; approval is supported by the supplied corpus.**

| Prior finding from `product/reviews/Sol_H6_r2.md` | Re-verification |
|---|---|
| `LIVE_PATH_MAJOR`: false collective approval claim | **Resolved.** `swarm/docs/ONE_PAGER_TEAM.md:36`, `README.md:112`, and `swarm/docs/PROOF.md:68–70` distinguish `Sol_H5_r1.md` as `REJECTED`, `Sol_H5_r2.md` as `APPROVED`, and `Sol_H6_r2.md` as `REJECTED`, with repairs attributed to `H5_r2` and `H6_r3`. |
| `BACKLOG`: provenance hashes unchecked | **Original mutation gap resolved; association checking remains incomplete.** `swarm/docs/test_docs.py:287–291` checks both filename and expected digest in `PROVENANCE.md`; mutation controls appear at `:465–485`. However, those are independent document-wide membership checks, so a digest attached to the wrong artifact can pass. |
| `BACKLOG`: duplicate rows and incomplete proof sets | **Resolved for the supplied tracked set.** `swarm/docs/test_docs.py:306–326` rejects duplicates and missing tracked paths; negative controls at `:771–798` exercise both cases. The `H6_r3` exception at `:325` permits an additional self-log row without requiring one before stream closure. |
| `BACKLOG`: placeholder deployment receipt | **Placeholder defect resolved; receipt validity remains incomplete.** `swarm/docs/OPERATOR_CHECKLIST.md:93–106` queries actual services and appends live health responses. However, `:90` creates the receipt before these operations succeed, while `:43` still makes receipt existence the deployment-status condition. |

The review identities agree with `PROVENANCE.md:72`:

| Report | Receipt | Recorded status |
|---|---|---|
| `product/reviews/Sol_H5_r1.md` | `ac78e147466040a6` | `REJECTED`; `4` `LIVE_PATH_MAJOR` [CITED] |
| `product/reviews/Sol_H5_r2.md` | `77367a51d8914d6c` | `APPROVED` |
| `product/reviews/Sol_H6_r2.md` | `58772a4c1bc34eb9` | `REJECTED`; `1` `LIVE_PATH_MAJOR` [CITED] |

**Hash and proof-set verification**

| Artifact set | Result against supplied receipts and `PROVENANCE.md` |
|---|---|
| Track H `.jsonl` rows, `swarm/docs/PROOF.md:11–34` | All `24` match [CITED]. |
| Track P `.jsonl` rows, `swarm/docs/PROOF.md:42–60` | All `19` match [CITED]. Together with Track H, the indexed paths exactly equal the supplied `43` tracked `.jsonl` paths [CITED]. |
| Review reports, `swarm/docs/PROOF.md:68–70` | All `3` hashes match the supplied receipts and `PROVENANCE.md:72` [CITED]. |
| Claude design inputs, `swarm/docs/PROOF.md:78–87` | All `10` absolute paths and hashes match the supplied receipts and `PROVENANCE.md:24–33` [CITED]. |

`swarm/proof/H6_r3_1538.jsonl`, supplied SHA-256 `80617e20bc8a5ecdcefb2bfa3410883f3813b94116a1d398129d7828bef13bb9`, is absent from both the supplied tracked set and `swarm/docs/PROOF.md`. Its expected self-log omission is accepted under the stated stream-closure exception.

**Remaining findings**

- **BACKLOG — `swarm/docs/test_docs.py:287–291`:** filename and digest membership does not establish their association within the same provenance record. The current supplied records nevertheless match.
- **BACKLOG — `swarm/docs/OPERATOR_CHECKLIST.md:43`, `:90`, `:93–106`:** a failed service query or health check can leave an existing, incomplete receipt. The receipt also omits the viewer’s deployed revision: `:101` records local `git log` metadata instead.

The supplied suite reports `18/18` passing tests, `rc=0` [CITED]. Its receipt assertions at `swarm/docs/test_docs.py:832–836` verify command text and placeholder removal, without testing failure handling.

**0 BLOCKER; 0 LIVE_PATH_MAJOR; 2 BACKLOG** [CITED: findings above]. This audit is confined to the supplied inline corpus; no tools were invoked.

**VERDICT: APPROVED**
