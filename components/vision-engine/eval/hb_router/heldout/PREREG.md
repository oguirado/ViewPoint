# PREREG: held-out router evaluation for the HB-VC corpus migration (sealed 2026-10-18 03:27, before any of the 8 remaining books is migrated)
Author: Gemini 4 Argon (medium), blind to hyperbooks/ (code + git history only); conductor validation: 120 rows (70 FIND, 50 ABSENT), all FIND gold symbols exist, split TEST, 1 ABSENT text contains an identifier-like token (ALL-A41, kept, declared).
sha256 cd9bc2dd880c38ddcba1a9d92012fe411fa408e6228fb35b5eee939b8950d80d. Coverage: all visioncore stages, vmem, vadapt ramws/rtring, vnative.
Method: same as ../PREREG.md (hb_eval v2 vs B1, recall@1/@5, MRR, ABSENT P/R/F1, paired sign-flip p<0.05 to claim). Runs: before = current corpus (1 book migrated: HB-VC-s1-desc), after = all 9 books migrated. Claim wording: "held-out TEST, blind author".
