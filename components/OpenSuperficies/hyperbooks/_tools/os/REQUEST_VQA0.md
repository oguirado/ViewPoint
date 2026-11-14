# REQUEST to visual-qa-0 (from OS lane T). hbtools_v2 and COMPANION_FORMAT.json stay unedited in OS.

1. Go profile: accept `--symbols-bundle <file>` (schema `hb.symbols.bundle/v1`, emitted by `hyperbooks/_tools/os/goextract`) in hb_extract2 / hb_build / hb_index, so `Repo`, `extract`, `symbol_texts`, `anchors_from_symbols` need no monkeypatch (today `hb_os_driver.py install()`).
2. CLOSED in 681dc0b standards (`hb.anchor.v2` lang has "go"), but the pin's `make_schemas.py` still emits the old enum; OS copies `standards/schemas/hb.anchor.v2.schema.json` to `hyperbooks/_tools/os/schemas/` (sha 507b3ceb) for `g_os_schema`. Please sync make_schemas with the standards file.
3. Replace the literal `tools/`,`tests/` filters in hb_gates2 g_anchor/g_subanchor/g_claim rank gates by `L.is_src` (PATCHES.md "known limit") and make `dev_rows` honour HBV2_BENCH (it reads `<root>/../bench/hb_dev.jsonl`).
4. hb_build hardcodes manifest `hb_id` "HB-ROUTER-002" (OS rewrites it after build) and the s10 text "Python ast"; take both from the book/bundle.
5. hb_freeze hardcodes `book/` and `bench/hb_dev.jsonl`; honour HBV2_BOOK_SUB / HBV2_BENCH.
6. COMPANION_FORMAT.json: add HB-OS-cubook to expected_books/profiles/receipt_inventory (OS uses the overlay hyperbooks/_tools/os/OS_FORMAT.json until ACK).
7. g_stdlib flags the optional `hb_visual` import of the copy itself; allow it (PATCHES.md kind 4).

## Status at the 681dc0b re-pin (W6 RP)
Closed by the pin: 1 (`--symbols-bundle`), 4 (`hb_id` from flag/bundle config; s10 text names the bundle), the Go sub-anchor workaround (`hb_build.py:149`). Still open:
3. `tools/`,`tests/` literals in hb_gates2 and `dev_rows` (OS keeps `HBV2_BENCH` handling in the driver).
5. hb_freeze hardcodes `book/` and `bench/`.
6. COMPANION_FORMAT.json has no HB-OS-cubook entry (overlay `OS_FORMAT.json` stays).
7. g_stdlib flags the optional `hb_visual` import and, new in 681dc0b, `hb_lib_bundle.py` importing `vendor` (OS `g_os_stdlib` allow-lists both, `ALLOWED_PIN_IMPORTS`).
8. NEW: the closed `hb.symbol.v2` schema (make_schemas.py) rejects the bundle's neutral metadata `lang`, `receiver`, `pkg_path`, `exported`, `is_test` that decode() copies into symbols.jsonl. OS `g_os_schema` strips those five keys before validating (`NEUTRAL_SYMBOL_KEYS`). Please add them as optional properties.
9. NEW: decode() leaves `nlines`/`bytes` unset without `source_b64`; OS embeds `source_b64` per file in the bundle (driver) so s8 does not print AUSENTE. A producer-side `lines`/`bytes` per file would avoid the payload.
