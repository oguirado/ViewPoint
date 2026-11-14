# Author Notes: Blind OpenSuperficies Query Set (`eval/hb_os/`)

## 1. Blindness Attestation
- **Author Model**: Gemini 4 Argon (medium), operating in W5a Room 1 Lane E.
- **Strict Blindness Invariant**: Zero files under `/home/user/OpenSuperficies/hyperbooks/**`, `/home/user/OpenSuperficies/contracts/**`, `/home/user/OpenSuperficies/contrato/**`, or `/home/user/OpenSuperficies/docs/**` were read, listed, or inspected during authoring. No HyperBook prose, section titles, claims, or search blocks existed or were consulted.
- **Zero TEST Evaluation**: Neither `hb_eval.py` nor any retrieval index was executed against `hb_test.jsonl` (or `hb_dev.jsonl`) during authoring.

## 2. Complete Read List
### Campaign Specification & Context Pack (`/home/user/Música/TeamWork_Antigravity2/campaigns/os-cu-0/w5/a/`)
1. `CARDS_W5A.md`
2. `AMEND_W5A.md`
3. `ADVOCATE_CARDS_W5A.md`
4. `packs/W5A_E.md`

### OpenSuperficies Repository (`/home/user/OpenSuperficies/` at `4cc6765b9c03351fa4775dce2446862313c634c2`)
- **Git History**: `git rev-parse HEAD` and `git log -n 15 --oneline -- src/cubook cmd/srf-cu-*`
- **Source Files Read (`src/cubook/**` and `cmd/srf-cu-*/**` only)**:
  1. `cmd/srf-cu-exec/main.go`
  2. `cmd/srf-cu-exec/wl.go`
  3. `cmd/srf-cu-mcp/main.go`
  4. `cmd/srf-cu-snap/main.go`
  5. `cmd/srf-cu-wlcap/main.go`
  6. `src/cubook/action/decode.go`
  7. `src/cubook/action/types.go`
  8. `src/cubook/ajson/ajson.go`
  9. `src/cubook/capture/analyzer.go`
  10. `src/cubook/capture/errors.go`
  11. `src/cubook/capture/fbsrc.go`
  12. `src/cubook/capture/guard.go`
  13. `src/cubook/capture/livedriver.go`
  14. `src/cubook/capture/quarantine.go`
  15. `src/cubook/capture/rawjson.go`
  16. `src/cubook/capture/ring.go`
  17. `src/cubook/capture/runargs.go`
  18. `src/cubook/capture/schema.go`
  19. `src/cubook/capture/snap.go`
  20. `src/cubook/capture/snapper_observer.go`
  21. `src/cubook/capture/xfreezer.go`
  22. `src/cubook/capture/xwd.go`
  23. `src/cubook/exec/bounds.go`
  24. `src/cubook/exec/exec.go`
  25. `src/cubook/exec/fake.go`
  26. `src/cubook/exec/replay.go`
  27. `src/cubook/exec/safety.go`
  28. `src/cubook/exec/types.go`
  29. `src/cubook/exec/wlplan.go`
  30. `src/cubook/exec/wlplan_v3.go`
  31. `src/cubook/exec/xtest.go`
  32. `src/cubook/mcp/astgate.go`
  33. `src/cubook/mcp/bridge.go`
  34. `src/cubook/mcp/caller.go`
  35. `src/cubook/mcp/cas.go`
  36. `src/cubook/mcp/dispatch.go`
  37. `src/cubook/mcp/editor.go`
  38. `src/cubook/mcp/errors.go`
  39. `src/cubook/mcp/guard.go`
  40. `src/cubook/mcp/registry.go`
  41. `src/cubook/mcp/server.go`
  42. `src/cubook/mcp/types.go`
  43. `src/cubook/prodgate/prodgate.go`
  44. `src/cubook/prodgate/wire.go`
  45. `src/cubook/rc/rc.go`
  - Plus AST identifier/stem extraction across the 41 `*_test.go` files in `src/cubook/**` and `cmd/srf-cu-*/**` (86 `.go` files total) performed by `eval/hb_os/leak_check.py`.

## 3. Leakage Prevention Methodology
- Using Go's `go/parser` and `go/ast`, every `ast.Ident` and `.go` file stem across all 86 `.go` files in `src/cubook/**` and `cmd/srf-cu-*/**` was extracted, case-folded, and split on underscores and camelCase boundaries (`1,875` distinct tokens of length `>= 4` chars) [MEASURED].
- Every `FIND`, `ABSENT`, and `CLAIM` query `text` in `hb_dev.jsonl` (60 rows) and `hb_test.jsonl` (100 rows) was verified by `eval/hb_os/leak_check.py` to have **0 overlapping tokens** (`>= 4` chars) with the 1,875 Go identifier/stem tokens, and every `ABSENT` query `text` was verified to have **0 lexical hits** (`>= 4` chars) against the entire 4,056-word Go source vocabulary [MEASURED].
