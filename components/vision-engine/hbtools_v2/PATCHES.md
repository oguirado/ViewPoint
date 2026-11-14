# PATCHES (hbtools_v2 copy)

Historical load-site patches are listed first; HBT2 producer-boundary changes follow below. Defaults retain the sealed artifact bytes. Original patch kinds:

1. env roots: HBV2_ROOT / HBV2_SUBJECT / HBV2_BENCH (+ vendored tokenizer/validator fallback in L.load_mod)
2. HBV2_SRC_DIRS path-prefix filter (L.is_src, scope_files, trace test selection)
3. HBV2_BOOK_SUB replacing the literal "book" in os.path.join (default book)
4. hb_visual import made optional (the file is excluded from the copy)

Not patched (known limit): other literal `tools/` filters in card_briefs, card_collect, census2, baseline_b2, mini_hb, hb_gates2 (g_anchor/g_claim rank gates).
Vendored (unpatched copies from the source subject/tools): `hbtools_v2/vendor/route_v2.py`, `jsonschema_lite.py` (the tokenizer and validator the tools load from the subject tree).

| file | kinds | before sha256 (source) | after sha256 (copy) |
|---|---|---|---|
| hbtools_v2/baseline_b0.py | 3 | db7a73885f0603b903e56510fbf15a079f7e0b857e6643e5a3ff443b1aab9927 | 1d71598f03156f3a4c33c53af36eab7f57ccfd5f6f5c43e2a513a933c1dab0a6 |
| hbtools_v2/baseline_b1.py | 3 | 5458e33f10a2e8e26d9cb43b3289c4ffc82e5b05d482d719ff7a7cc7cb45027c | 60ef625451874b5870754df4d3b31e7d1854f69d16e91faf30e87aa07a38909f |
| hbtools_v2/baseline_b2.py | 3 | 5f0453109830e1a8b4d9804c8408902a31d2b9c289e8efeff823c51029c1fc33 | 97924711fc13bfb2c3fd24e62bb248d3c3dc9600bc1d38543abb72ca8e3aa3e4 |
| hbtools_v2/card_collect.py | 3 | 5d0a02269ebffec1b500e6c0c063d91437df765604129afeb7c9edf1e9913943 | 5ebb8de2da2ff074de7ea73f9b607a32940008fe8fff128fc448c56299f1e3af |
| hbtools_v2/devaug.py | 3 | e76c7149ef54af480a73833a4309da88c6ebe481ca32342908f1089569c0b910 | 99094ad0d972cf257515818f866c2f6843bfeb606143513c9b373c298ff29455 |
| hbtools_v2/hb_build.py | 1,2,3,4 | 5a2fb3be0a3492fdf6ee8736b6f0a2b7f7eb5a737ae3f062ee7f1b82493fd222 | aacdd7761212a0f19ad068d231c14fc1c5e8575b019411ac89243208095004db |
| hbtools_v2/hb_eval.py | 1,3 | 6bc6adee984dabc025ebfb850c5a508449a0f0231a6ada0ca4db1629ec80337d | 3c9ee0566763d9ba41757f6cce68d6d89e4ebc721e8ae7f43215a07fc233b035 |
| hbtools_v2/hb_extract2.py | 2 | 30c93c937fbd54ba08dd4528c2a1cdd7da50eae80b0681efd0df3c0d73360725 | 452f24b8f9a341b57200b87765a9df7794a415c3a364ff49422f4c5fb15b825a |
| hbtools_v2/hb_gates2.py | 1,3 | b5afd639c27b615ec7d6227abf675e4871b85c4643ed596d8098057fd2bfe467 | 3c3444744d032cf781160e9ccb585a414a3a60fb2f35f716411241ea8c07290c |
| hbtools_v2/hb_index.py | 1,3 | 2d04560eb3fdf38a481cbf8ec7e6eac92d9f1ba4d466eedb7a4db9df2b36ca5b | 48322291554e3bfeb2d1ef1836332654a0c946d1d088e40cc24570bfe054dafe |
| hbtools_v2/hb_query.py | 1,3 | bb833041265e69eabd9534803d36324b1c46897ae7670a4138440017d03a84e2 | 925a27ef7c7848e8974c1119022def5487e2733ab31bb337a8d3467006c886ed |
| hbtools_v2/hb_scaffold2.py | 3 | c37f80edb36de988821dcc2ca502d135e3b4fe59e8875ae1afb774e31b03158e | 80dc1e5fe8cdb85006f20c2047fbe1a79e1c20ac5464e14aa9aee82de3191f9d |
| hbtools_v2/hb_trace.py | 2 | c319178e052d3df20e8d612427f03c5e7f9f3cb732667ccd805c924c75d577c5 | a61b3da06d2437e59ec84cf9ad1f9511c81757e75ee0752444ae1519ccc8f6cc |
| hbtools_v2/hb_tune.py | 1 | 4868e56cd4a63d123695a96525493492a6358295ff3aee8e57c7318552dec1c0 | 0f81914976363033b7a8d39d36e7e498f004809c42de1ca5b9dd037cc4d66a97 |
| hbtools_v2/hblib2.py | 1,2,3 | 2484ee3cd41ec016ff3f0f518498af9228fa2a794c00f1be3883997204dffe39 | 2ccd5e11a6b7ad28546d8144f103b92374fa5c8e681227d28a2b4ec7f783abda |
| hbtools_v2/merge_fixture.py | 3 | d6618d47622fe3f4a7a95c3373bf389fc8790baacac5bebf0a28523b2dac64fb | 482d435253df0d493113e8be051906d01618a14282c92fa85a0781333d4b20df |
| hbtools_v2/mini_hb.py | 3 | f5e8e093513dae003694de82d43159267f20553d54d775fa98c4fdf272b1744c | 6d1218177b5e3cfae8778de7a075d0b958d9381d559a95ffcbef5cc8f21a3e65 |

## HBT2: language-neutral symbol bundles

`hb.symbols.bundle/v1` is defined in `schema/hb.symbols.bundle.v1.json`.
Language identifiers use `^[a-z][a-z0-9_+-]{0,15}$` for all producers.
`id` identifies a symbol (SCIP-like); anchors describe occurrences and roles;
relationships use `src`, `dst`, `kind`. Line spans are inclusive and one-based,
so they are deliberately not SCIP's zero-based range encoding. Go identities use
`path::<Recv.>Name`; optional `receiver`, `pkg_path`, `exported`, `is_test` retain
producer metadata. Signature hashes remain producer-defined: Python retains its
historical AST signature hash rather than hashing the displayed signature.

Extract, build and index accept `--symbols-bundle FILE`, with
`HBV2_SYMBOLS_BUNDLE` as the environment equivalent. An explicit bundle supplies
all symbols, text fields and static edges; no symbol extractor or fixture symbol
parser runs. Build/index consume `out/symbols_bundle.json` and
`out/symbols_raw.jsonl` when available, rejecting disagreement. For old callers
without extraction output, a bounded subprocess runs the Python producer into a
temporary directory; build/index never re-extract in-process. The existing
Python extraction now also emits `out/symbols_bundle.json`.

Optional `legacy` rows and `source_b64` snapshots preserve sealed Python output
bytes, including AST hashes, byte slicing, subanchors, text columns and generated
provenance. File snapshots are authenticated against their declared SHA-256.
A minimal foreign bundle may omit byte-level evidence: unavailable symbol byte
spans/hashes and extraction call-site metrics remain JSON null, and source
lengths render as AUSENTE. Those records can build/index from supplied symbols
and texts; source-byte/AST validation requires producer evidence and must not
interpret null as a successful byte-level check. This adapter does not extend
the sealed Python-only hb.symbol.v2 validator outside this card's surface.
Non-Python subanchors must be supplied by the producer; the Python AST packer
is never applied to foreign source snapshots.

`--hb-id` / `HBV2_HB_ID` select identity for extract/build/scaffold. Scaffold also
reads `hb_id` or `book_id` from its slots. Build reads book configuration from
`seed/slots.json`; an existing manifest pins its historical identity first.
The sole legacy constant lives in `hb_lib_bundle.LEGACY_FIXTURE_ID` and is used
for configless router/mini fixtures. Existing sealed manifests retain that
historical ID even where authored section IDs differ. New configured books use
their configured ID; an explicit flag overrides it. External bundle identity is
used as configuration unless an explicit identity overrides it.

### Verification and scope

All existing books and the assembled eval/hb_router corpus were rebuilt with
the saved pre-HBT2 tools, with the new default pipeline, and with the Python
bundle fed back through the flag. The table below lists SHA-256 for every
checked artifact [MEASURED]. Router inputs include book.md, nodes.jsonl,
symbols.jsonl, anchors.jsonl, graph.json and all index columns/postings/vocabulary.
The artifact hashes and aggregate book hashes are asserted by
`tests/test_hbtools_bundle.py` for the repository books; assembled-corpus
measurements are retained in `build/room_S/router-{before,after,roundtrip}.json`.
Baseline controls reproduce the old CLI rejecting the bundle flag, the old
extractor returning no Go symbols, and the old scaffold ignoring a custom ID
[MEASURED]. The Go fixture checks populated doc/com/lit columns and graph edges;
negative controls reject malformed schema, duplicate identities, dangling
edges, checksum errors, language drift and stale raw/bundle disagreement.

Only the files listed below are implementation/test changes. PROVENANCE.json
pinned the old copied hashes outside CARD_HBT2's enumerated surface;
HBT2B refreshes those copied hashes below while retaining source_sha256. No sealed router
receipt or book artifact was modified.

| File | Before SHA-256 [MEASURED] | After SHA-256 [MEASURED] |
|---|---|---|
| hbtools_v2/hb_extract2.py | 452f24b8f9a341b57200b87765a9df7794a415c3a364ff49422f4c5fb15b825a | 275ddfcab7f3bc86a3ac0ad4a32557846508ef9056cb79d207923fc49b29a0af |
| hbtools_v2/hb_build.py | aacdd7761212a0f19ad068d231c14fc1c5e8575b019411ac89243208095004db | 8e9f4481abb04073f6be963b19cc1bf86e0da88071ac3b0e2930ccf870d7194a |
| hbtools_v2/hb_index.py | 48322291554e3bfeb2d1ef1836332654a0c946d1d088e40cc24570bfe054dafe | 8e97667d34fca82d4322bcb17ac41e8b585d3e51fdf242db6ac71545542f25c8 |
| hbtools_v2/hb_scaffold2.py | 80dc1e5fe8cdb85006f20c2047fbe1a79e1c20ac5464e14aa9aee82de3191f9d | 338814b4ae22f04f90ded116a9eae1a6c7ce638339b64ccfc9cd8877967dbbbb |
| hbtools_v2/hb_lib_bundle.py | AUSENTE (new file) | 6a5931e2fc86c7818167b89500313ea6cdaffa2b2ac6caa1b7b278a600b98068 |
| hbtools_v2/schema/hb.symbols.bundle.v1.json | AUSENTE (new file) | b4905c2bb6b6ed6fad091f8bcbb88aa2f2c719d1322060b43fa61bd5a92bfe59 |
| tests/test_hbtools_bundle.py | AUSENTE (new file) | 6a441a2cfe3b9d2d3abc923ace0bec8ee62dcc45c241b3510b97d0edf6381bb9 |

### Artifact byte-equivalence SHA table [MEASURED]

| Corpus / artifact | Before | Default after | Python bundle after |
|---|---|---|---|
| HB-VC-a0-arch/out/symbols_raw.jsonl | b69cba6dcb9036e7995667a962186f61a579ad79ae97fc4ad66eb85dc7c593c5 | b69cba6dcb9036e7995667a962186f61a579ad79ae97fc4ad66eb85dc7c593c5 | b69cba6dcb9036e7995667a962186f61a579ad79ae97fc4ad66eb85dc7c593c5 |
| HB-VC-a0-arch/manifest.json | 5d850a8929a83c5be3e1f588f8729e82c03c4f5b5469cf060aebb0c8b748e8c0 | 5d850a8929a83c5be3e1f588f8729e82c03c4f5b5469cf060aebb0c8b748e8c0 | 5d850a8929a83c5be3e1f588f8729e82c03c4f5b5469cf060aebb0c8b748e8c0 |
| HB-VC-a0-arch/book.md | 05252ed926c8d29c1728e7c095b1bc2b3b43cb9b6cea77fc3292ea27592a8b5b | 05252ed926c8d29c1728e7c095b1bc2b3b43cb9b6cea77fc3292ea27592a8b5b | 05252ed926c8d29c1728e7c095b1bc2b3b43cb9b6cea77fc3292ea27592a8b5b |
| HB-VC-a0-arch/symbols.jsonl | ca4e6a98c9078f8ffa11baea1ed02d81cb0443131063462c1d64e512726f0be5 | ca4e6a98c9078f8ffa11baea1ed02d81cb0443131063462c1d64e512726f0be5 | ca4e6a98c9078f8ffa11baea1ed02d81cb0443131063462c1d64e512726f0be5 |
| HB-VC-a0-arch/nodes.jsonl | 4eb4489acfafbc64f70f7e1b25466ce780f79097ae90fd02c495ad33388697df | 4eb4489acfafbc64f70f7e1b25466ce780f79097ae90fd02c495ad33388697df | 4eb4489acfafbc64f70f7e1b25466ce780f79097ae90fd02c495ad33388697df |
| HB-VC-a0-arch/anchors.jsonl | 4cc0aef20cbc2605f081f0a77ac161041ee17af9039c129e17a547b6811d8b83 | 4cc0aef20cbc2605f081f0a77ac161041ee17af9039c129e17a547b6811d8b83 | 4cc0aef20cbc2605f081f0a77ac161041ee17af9039c129e17a547b6811d8b83 |
| HB-VC-a0-arch/graph.json | ee8f091b4d0d1164c0afb8d1b2d976510feb7a0924609ba9ab8da5d7db186632 | ee8f091b4d0d1164c0afb8d1b2d976510feb7a0924609ba9ab8da5d7db186632 | ee8f091b4d0d1164c0afb8d1b2d976510feb7a0924609ba9ab8da5d7db186632 |
| HB-VC-a0-arch/index/cols.json | 3c7b1544f2214dcfb74ef21ed55dc2435bbf7bf1778e1f1535da3a284e2cc759 | 3c7b1544f2214dcfb74ef21ed55dc2435bbf7bf1778e1f1535da3a284e2cc759 | 3c7b1544f2214dcfb74ef21ed55dc2435bbf7bf1778e1f1535da3a284e2cc759 |
| HB-VC-a0-arch/index/post.json | 8a6a5388660d02e420663c97e40fa72e43dd5788d5cf5d06a286ea9cba4e27c4 | 8a6a5388660d02e420663c97e40fa72e43dd5788d5cf5d06a286ea9cba4e27c4 | 8a6a5388660d02e420663c97e40fa72e43dd5788d5cf5d06a286ea9cba4e27c4 |
| HB-VC-a0-arch/index/vocab.json | 164557e809e9bef76e18a7e44b9a9648baab6861ab00f32a9c8686dc7ed2cb04 | 164557e809e9bef76e18a7e44b9a9648baab6861ab00f32a9c8686dc7ed2cb04 | 164557e809e9bef76e18a7e44b9a9648baab6861ab00f32a9c8686dc7ed2cb04 |
| HB-VC-a0-arch/index/ID | ca56b708f0e6434ca5cc58403327b73e26894316f2510a4720451c77d954e8cf | ca56b708f0e6434ca5cc58403327b73e26894316f2510a4720451c77d954e8cf | ca56b708f0e6434ca5cc58403327b73e26894316f2510a4720451c77d954e8cf |
| HB-VC-s0-grid/out/symbols_raw.jsonl | adee417f54d96f7f0eea1c4da04793bb2ac5829d3f10ae6834a2538fa14fdc80 | adee417f54d96f7f0eea1c4da04793bb2ac5829d3f10ae6834a2538fa14fdc80 | adee417f54d96f7f0eea1c4da04793bb2ac5829d3f10ae6834a2538fa14fdc80 |
| HB-VC-s0-grid/manifest.json | 5b9f700711fda6bc1912d5e81dc4873b5a0b2e67704892fd3276d5e8b8f9aaf4 | 5b9f700711fda6bc1912d5e81dc4873b5a0b2e67704892fd3276d5e8b8f9aaf4 | 5b9f700711fda6bc1912d5e81dc4873b5a0b2e67704892fd3276d5e8b8f9aaf4 |
| HB-VC-s0-grid/book.md | c2badbacf1103982b081abeede7924c6851d64c4b6f48f7db5a2d68a1599d548 | c2badbacf1103982b081abeede7924c6851d64c4b6f48f7db5a2d68a1599d548 | c2badbacf1103982b081abeede7924c6851d64c4b6f48f7db5a2d68a1599d548 |
| HB-VC-s0-grid/symbols.jsonl | 63b45d7625d93321470cd907178f763f992f34035bafed04e62cd1594e0e3dd8 | 63b45d7625d93321470cd907178f763f992f34035bafed04e62cd1594e0e3dd8 | 63b45d7625d93321470cd907178f763f992f34035bafed04e62cd1594e0e3dd8 |
| HB-VC-s0-grid/nodes.jsonl | 8cdd2e61014f482fb30208efbe52dd13c753dfdc928b88c595e353508d47b83d | 8cdd2e61014f482fb30208efbe52dd13c753dfdc928b88c595e353508d47b83d | 8cdd2e61014f482fb30208efbe52dd13c753dfdc928b88c595e353508d47b83d |
| HB-VC-s0-grid/anchors.jsonl | 17ef7efa62aa8b6ff50bb683677cfdfd0e7094353739c9cf61d55b4e7a9998b3 | 17ef7efa62aa8b6ff50bb683677cfdfd0e7094353739c9cf61d55b4e7a9998b3 | 17ef7efa62aa8b6ff50bb683677cfdfd0e7094353739c9cf61d55b4e7a9998b3 |
| HB-VC-s0-grid/graph.json | fe4349a7fed8504d24cb09b8574e8e56c93094edc5e2a7a78d96a516fe67e4a3 | fe4349a7fed8504d24cb09b8574e8e56c93094edc5e2a7a78d96a516fe67e4a3 | fe4349a7fed8504d24cb09b8574e8e56c93094edc5e2a7a78d96a516fe67e4a3 |
| HB-VC-s0-grid/index/cols.json | 18990cc428244cc2a87d532690d61d7fc9423f6eb0b2eaad0175527292afbd4d | 18990cc428244cc2a87d532690d61d7fc9423f6eb0b2eaad0175527292afbd4d | 18990cc428244cc2a87d532690d61d7fc9423f6eb0b2eaad0175527292afbd4d |
| HB-VC-s0-grid/index/post.json | 374248ae943bd5b2f42f30976a96f1da2a5f39b58cc252cd3fb294f66fb1f2e0 | 374248ae943bd5b2f42f30976a96f1da2a5f39b58cc252cd3fb294f66fb1f2e0 | 374248ae943bd5b2f42f30976a96f1da2a5f39b58cc252cd3fb294f66fb1f2e0 |
| HB-VC-s0-grid/index/vocab.json | 04db832d14a50ca278118ee8307dd0b40745fb0b27caf6736ee62483d97081b2 | 04db832d14a50ca278118ee8307dd0b40745fb0b27caf6736ee62483d97081b2 | 04db832d14a50ca278118ee8307dd0b40745fb0b27caf6736ee62483d97081b2 |
| HB-VC-s0-grid/index/ID | 5ed76d06c5ec5775862b1b08d3703518fa8fe356e31789720b45687afcfc4d27 | 5ed76d06c5ec5775862b1b08d3703518fa8fe356e31789720b45687afcfc4d27 | 5ed76d06c5ec5775862b1b08d3703518fa8fe356e31789720b45687afcfc4d27 |
| HB-VC-s1-desc/out/symbols_raw.jsonl | 2bea60acedcf645d894b644e32fb34290bbf56a17f315e94f64c746efc86550e | 2bea60acedcf645d894b644e32fb34290bbf56a17f315e94f64c746efc86550e | 2bea60acedcf645d894b644e32fb34290bbf56a17f315e94f64c746efc86550e |
| HB-VC-s1-desc/manifest.json | 34a41fb72d4da1e76cc3a140eb3144de1a4d4496ac605d9e191846b3d45723df | 34a41fb72d4da1e76cc3a140eb3144de1a4d4496ac605d9e191846b3d45723df | 34a41fb72d4da1e76cc3a140eb3144de1a4d4496ac605d9e191846b3d45723df |
| HB-VC-s1-desc/book.md | 400b9471ec0fb02e8dd826c9e8f956da6c65a5c677a3d002e58765465149585b | 400b9471ec0fb02e8dd826c9e8f956da6c65a5c677a3d002e58765465149585b | 400b9471ec0fb02e8dd826c9e8f956da6c65a5c677a3d002e58765465149585b |
| HB-VC-s1-desc/symbols.jsonl | 246c83f3e98c8c581e2dd23f36823a3976951e844cdff3a7cc1c1b4e997b4006 | 246c83f3e98c8c581e2dd23f36823a3976951e844cdff3a7cc1c1b4e997b4006 | 246c83f3e98c8c581e2dd23f36823a3976951e844cdff3a7cc1c1b4e997b4006 |
| HB-VC-s1-desc/nodes.jsonl | a73baa3127dba99be6e60e531deceb7547a191d60713e7b03dc34ab3a23f7c64 | a73baa3127dba99be6e60e531deceb7547a191d60713e7b03dc34ab3a23f7c64 | a73baa3127dba99be6e60e531deceb7547a191d60713e7b03dc34ab3a23f7c64 |
| HB-VC-s1-desc/anchors.jsonl | fbdc8a4d05bd8c1807c214f5462cfe080a1bfa1a1186ca3d3e7d8cc7aa1ead19 | fbdc8a4d05bd8c1807c214f5462cfe080a1bfa1a1186ca3d3e7d8cc7aa1ead19 | fbdc8a4d05bd8c1807c214f5462cfe080a1bfa1a1186ca3d3e7d8cc7aa1ead19 |
| HB-VC-s1-desc/graph.json | ae4eebfb1310bbe8c5ba7fc0325015890ef31968c3e35e3032abf8445128896c | ae4eebfb1310bbe8c5ba7fc0325015890ef31968c3e35e3032abf8445128896c | ae4eebfb1310bbe8c5ba7fc0325015890ef31968c3e35e3032abf8445128896c |
| HB-VC-s1-desc/index/cols.json | 19af62e467bb47aec07a0e40023e6306acfc7491ed6db2a5d4be3fa5a856f562 | 19af62e467bb47aec07a0e40023e6306acfc7491ed6db2a5d4be3fa5a856f562 | 19af62e467bb47aec07a0e40023e6306acfc7491ed6db2a5d4be3fa5a856f562 |
| HB-VC-s1-desc/index/post.json | eb0b55cdd384c4adfde4e46409af6187acaec4615a18a851f54279d39a18ca53 | eb0b55cdd384c4adfde4e46409af6187acaec4615a18a851f54279d39a18ca53 | eb0b55cdd384c4adfde4e46409af6187acaec4615a18a851f54279d39a18ca53 |
| HB-VC-s1-desc/index/vocab.json | 56ec3fb56ec1bb4ebf97ad2255c02420ac80b3607f93ebde04867b55a22061d4 | 56ec3fb56ec1bb4ebf97ad2255c02420ac80b3607f93ebde04867b55a22061d4 | 56ec3fb56ec1bb4ebf97ad2255c02420ac80b3607f93ebde04867b55a22061d4 |
| HB-VC-s1-desc/index/ID | 58038ba6dda453545437b13ddf4d3dead0557f7ce9fc52bbb5ddbc080fc7a597 | 58038ba6dda453545437b13ddf4d3dead0557f7ce9fc52bbb5ddbc080fc7a597 | 58038ba6dda453545437b13ddf4d3dead0557f7ce9fc52bbb5ddbc080fc7a597 |
| HB-VC-s2-change/out/symbols_raw.jsonl | a274936f9e7869792fcb2d871166978f059e13a9a64350628acdf794b8ac396f | a274936f9e7869792fcb2d871166978f059e13a9a64350628acdf794b8ac396f | a274936f9e7869792fcb2d871166978f059e13a9a64350628acdf794b8ac396f |
| HB-VC-s2-change/manifest.json | 83e961717a127132469b6f27b23f09e40b10e25f3d9cae24598df5f01014f258 | 83e961717a127132469b6f27b23f09e40b10e25f3d9cae24598df5f01014f258 | 83e961717a127132469b6f27b23f09e40b10e25f3d9cae24598df5f01014f258 |
| HB-VC-s2-change/book.md | 19750f8ed817703b6df9769586d8ceae3e5695d412a3af0fc6c57d913b800bae | 19750f8ed817703b6df9769586d8ceae3e5695d412a3af0fc6c57d913b800bae | 19750f8ed817703b6df9769586d8ceae3e5695d412a3af0fc6c57d913b800bae |
| HB-VC-s2-change/symbols.jsonl | 1f27f0e378ced8a55dd2af238581fb52dc773d4e004c0883e3a66bc7b62d235b | 1f27f0e378ced8a55dd2af238581fb52dc773d4e004c0883e3a66bc7b62d235b | 1f27f0e378ced8a55dd2af238581fb52dc773d4e004c0883e3a66bc7b62d235b |
| HB-VC-s2-change/nodes.jsonl | 9b903bb032ec76fb41e95ba838519c1bef525fb81902c79f2fbadd01be589fd9 | 9b903bb032ec76fb41e95ba838519c1bef525fb81902c79f2fbadd01be589fd9 | 9b903bb032ec76fb41e95ba838519c1bef525fb81902c79f2fbadd01be589fd9 |
| HB-VC-s2-change/anchors.jsonl | 6562236033cfdbe84e32d59e34401c19cebad84546698319d803ae9017cd4398 | 6562236033cfdbe84e32d59e34401c19cebad84546698319d803ae9017cd4398 | 6562236033cfdbe84e32d59e34401c19cebad84546698319d803ae9017cd4398 |
| HB-VC-s2-change/graph.json | 14034a95ed0d1b456a284c349a1d260d57951d54631e639e73e6b144f903c524 | 14034a95ed0d1b456a284c349a1d260d57951d54631e639e73e6b144f903c524 | 14034a95ed0d1b456a284c349a1d260d57951d54631e639e73e6b144f903c524 |
| HB-VC-s2-change/index/cols.json | 8161351036bdbff9bfbefae248a794bae78e16a241be014602af5472d6763fa8 | 8161351036bdbff9bfbefae248a794bae78e16a241be014602af5472d6763fa8 | 8161351036bdbff9bfbefae248a794bae78e16a241be014602af5472d6763fa8 |
| HB-VC-s2-change/index/post.json | f64978d1c58ea245bf6148c69cefc958516f7ed1ddc4c68b2db60a803ea345ae | f64978d1c58ea245bf6148c69cefc958516f7ed1ddc4c68b2db60a803ea345ae | f64978d1c58ea245bf6148c69cefc958516f7ed1ddc4c68b2db60a803ea345ae |
| HB-VC-s2-change/index/vocab.json | eed3eb151bbed73a95d5728cb2344fae1c3c9bfbfe5f86c8665b6a2b15309f08 | eed3eb151bbed73a95d5728cb2344fae1c3c9bfbfe5f86c8665b6a2b15309f08 | eed3eb151bbed73a95d5728cb2344fae1c3c9bfbfe5f86c8665b6a2b15309f08 |
| HB-VC-s2-change/index/ID | 56456078d882e56ed3b08dc6cfb5a6eca0c67538739b81e6a96449dc82fef08b | 56456078d882e56ed3b08dc6cfb5a6eca0c67538739b81e6a96449dc82fef08b | 56456078d882e56ed3b08dc6cfb5a6eca0c67538739b81e6a96449dc82fef08b |
| HB-VC-s3-pyramid/out/symbols_raw.jsonl | 6d5ba15f10ed6d6c43667abc23cb852d8c39212f68d2a79ed8fdc239adb802c3 | 6d5ba15f10ed6d6c43667abc23cb852d8c39212f68d2a79ed8fdc239adb802c3 | 6d5ba15f10ed6d6c43667abc23cb852d8c39212f68d2a79ed8fdc239adb802c3 |
| HB-VC-s3-pyramid/manifest.json | 2edc2975808fd60935d41b59938b61d218e5a4d321f1dd132a82f640da276a92 | 2edc2975808fd60935d41b59938b61d218e5a4d321f1dd132a82f640da276a92 | 2edc2975808fd60935d41b59938b61d218e5a4d321f1dd132a82f640da276a92 |
| HB-VC-s3-pyramid/book.md | 55a0e43e567d87d3576d20bbcf3b0adeca7f44f2d5b976a82d7f1e6f5bbaa325 | 55a0e43e567d87d3576d20bbcf3b0adeca7f44f2d5b976a82d7f1e6f5bbaa325 | 55a0e43e567d87d3576d20bbcf3b0adeca7f44f2d5b976a82d7f1e6f5bbaa325 |
| HB-VC-s3-pyramid/symbols.jsonl | a20614e19c9f35d44f867a7eddec181e26bb768afa119405329ae41160b6e1bd | a20614e19c9f35d44f867a7eddec181e26bb768afa119405329ae41160b6e1bd | a20614e19c9f35d44f867a7eddec181e26bb768afa119405329ae41160b6e1bd |
| HB-VC-s3-pyramid/nodes.jsonl | 6a21e7ebf96a0d6053e856d136eb68e3da095645b808fe29582e0fd8fb634d75 | 6a21e7ebf96a0d6053e856d136eb68e3da095645b808fe29582e0fd8fb634d75 | 6a21e7ebf96a0d6053e856d136eb68e3da095645b808fe29582e0fd8fb634d75 |
| HB-VC-s3-pyramid/anchors.jsonl | 313c6516b98a195317c038eac70ca0ecc82abf12e70ebbc4604101e62d919c38 | 313c6516b98a195317c038eac70ca0ecc82abf12e70ebbc4604101e62d919c38 | 313c6516b98a195317c038eac70ca0ecc82abf12e70ebbc4604101e62d919c38 |
| HB-VC-s3-pyramid/graph.json | cf04efda003b8a6b42b9279beef686d65fc1e109ec45a1c2a587ac8e4df53a15 | cf04efda003b8a6b42b9279beef686d65fc1e109ec45a1c2a587ac8e4df53a15 | cf04efda003b8a6b42b9279beef686d65fc1e109ec45a1c2a587ac8e4df53a15 |
| HB-VC-s3-pyramid/index/cols.json | 64ced7dd88c852a024f7ac3f91abb719b8203a62051094ffc77a7c2449056aca | 64ced7dd88c852a024f7ac3f91abb719b8203a62051094ffc77a7c2449056aca | 64ced7dd88c852a024f7ac3f91abb719b8203a62051094ffc77a7c2449056aca |
| HB-VC-s3-pyramid/index/post.json | 7dea62b8a27ecd2ff6236ca1f9cff9b0d6d4ce1adfdd747646ae9e853fcacfd6 | 7dea62b8a27ecd2ff6236ca1f9cff9b0d6d4ce1adfdd747646ae9e853fcacfd6 | 7dea62b8a27ecd2ff6236ca1f9cff9b0d6d4ce1adfdd747646ae9e853fcacfd6 |
| HB-VC-s3-pyramid/index/vocab.json | 0ab9602ed007119b2a4a2f0603701981ae62de3fa2269eba3af2e4a4e56d0a6d | 0ab9602ed007119b2a4a2f0603701981ae62de3fa2269eba3af2e4a4e56d0a6d | 0ab9602ed007119b2a4a2f0603701981ae62de3fa2269eba3af2e4a4e56d0a6d |
| HB-VC-s3-pyramid/index/ID | 613e776308aae769634db7b46d4534784533615d8ffe9c5238700bcbae8581a6 | 613e776308aae769634db7b46d4534784533615d8ffe9c5238700bcbae8581a6 | 613e776308aae769634db7b46d4534784533615d8ffe9c5238700bcbae8581a6 |
| HB-VC-s4-regions/out/symbols_raw.jsonl | 1bf43e40ee58d75bc21f99dcd3d7dc976803661d03892a1b3e92f46da939b361 | 1bf43e40ee58d75bc21f99dcd3d7dc976803661d03892a1b3e92f46da939b361 | 1bf43e40ee58d75bc21f99dcd3d7dc976803661d03892a1b3e92f46da939b361 |
| HB-VC-s4-regions/manifest.json | 3b1527e8f18d7206b47416e499fd20420c14b29a56e520226b3a847e6c2cb4ef | 3b1527e8f18d7206b47416e499fd20420c14b29a56e520226b3a847e6c2cb4ef | 3b1527e8f18d7206b47416e499fd20420c14b29a56e520226b3a847e6c2cb4ef |
| HB-VC-s4-regions/book.md | 930672ab5336ba1d60e9a66944540edcc1aa19eeecf5d139a61bf24bd2b14b03 | 930672ab5336ba1d60e9a66944540edcc1aa19eeecf5d139a61bf24bd2b14b03 | 930672ab5336ba1d60e9a66944540edcc1aa19eeecf5d139a61bf24bd2b14b03 |
| HB-VC-s4-regions/symbols.jsonl | a9b8ac8a7bde2f39cbe5c7295fd3bb8dd9597e20c0071b767118b44c6db96801 | a9b8ac8a7bde2f39cbe5c7295fd3bb8dd9597e20c0071b767118b44c6db96801 | a9b8ac8a7bde2f39cbe5c7295fd3bb8dd9597e20c0071b767118b44c6db96801 |
| HB-VC-s4-regions/nodes.jsonl | 95efd02057ebae3994a267e60823821dd4469b4970b393e9541f1bd329b02414 | 95efd02057ebae3994a267e60823821dd4469b4970b393e9541f1bd329b02414 | 95efd02057ebae3994a267e60823821dd4469b4970b393e9541f1bd329b02414 |
| HB-VC-s4-regions/anchors.jsonl | 1dee58b9fc2e58500850af22100818c296ba475d69f38fb544708ff922db39e4 | 1dee58b9fc2e58500850af22100818c296ba475d69f38fb544708ff922db39e4 | 1dee58b9fc2e58500850af22100818c296ba475d69f38fb544708ff922db39e4 |
| HB-VC-s4-regions/graph.json | d2ddf226bee7c072069f0d1965276ba17aa4acb349a0d380ffd5fd6262512c8f | d2ddf226bee7c072069f0d1965276ba17aa4acb349a0d380ffd5fd6262512c8f | d2ddf226bee7c072069f0d1965276ba17aa4acb349a0d380ffd5fd6262512c8f |
| HB-VC-s4-regions/index/cols.json | ce49093c46b3e4bc9e3f2470b59dfff24dbaf70c761e43c3af0c3140163f0d66 | ce49093c46b3e4bc9e3f2470b59dfff24dbaf70c761e43c3af0c3140163f0d66 | ce49093c46b3e4bc9e3f2470b59dfff24dbaf70c761e43c3af0c3140163f0d66 |
| HB-VC-s4-regions/index/post.json | bf927f1ff1da5e6f087230983b5cf4ef9506838f6a618dc54f459f228f3490be | bf927f1ff1da5e6f087230983b5cf4ef9506838f6a618dc54f459f228f3490be | bf927f1ff1da5e6f087230983b5cf4ef9506838f6a618dc54f459f228f3490be |
| HB-VC-s4-regions/index/vocab.json | 1b0496310e3187164c5f333cbf6ce48a3d347e67cbc3a42c3ace88bd4a40caa7 | 1b0496310e3187164c5f333cbf6ce48a3d347e67cbc3a42c3ace88bd4a40caa7 | 1b0496310e3187164c5f333cbf6ce48a3d347e67cbc3a42c3ace88bd4a40caa7 |
| HB-VC-s4-regions/index/ID | ac11a2bfe5d831b528145e11194d642e474c51daf1ca777b0d6cc74487773641 | ac11a2bfe5d831b528145e11194d642e474c51daf1ca777b0d6cc74487773641 | ac11a2bfe5d831b528145e11194d642e474c51daf1ca777b0d6cc74487773641 |
| HB-VC-s5-gop/out/symbols_raw.jsonl | b2cc3f75637e845ccb25eda095fc0e68ee232bcc7a58998d57f3bb090e8085c4 | b2cc3f75637e845ccb25eda095fc0e68ee232bcc7a58998d57f3bb090e8085c4 | b2cc3f75637e845ccb25eda095fc0e68ee232bcc7a58998d57f3bb090e8085c4 |
| HB-VC-s5-gop/manifest.json | 1d0e73578d8b980d1472b76c0130c2c0ffeb78f2d96570b0e1e7fe49eb3849e2 | 1d0e73578d8b980d1472b76c0130c2c0ffeb78f2d96570b0e1e7fe49eb3849e2 | 1d0e73578d8b980d1472b76c0130c2c0ffeb78f2d96570b0e1e7fe49eb3849e2 |
| HB-VC-s5-gop/book.md | 756c0a1f3fabebae825cc8e3076fd3835fc910871cbbc0ac59fb5129d8b24a31 | 756c0a1f3fabebae825cc8e3076fd3835fc910871cbbc0ac59fb5129d8b24a31 | 756c0a1f3fabebae825cc8e3076fd3835fc910871cbbc0ac59fb5129d8b24a31 |
| HB-VC-s5-gop/symbols.jsonl | a1609e5eaef5eeb150d00f3c14a6dcb2fd551b3934a82be03082a398b9de0acc | a1609e5eaef5eeb150d00f3c14a6dcb2fd551b3934a82be03082a398b9de0acc | a1609e5eaef5eeb150d00f3c14a6dcb2fd551b3934a82be03082a398b9de0acc |
| HB-VC-s5-gop/nodes.jsonl | 529aefad7e94602d392f0bd86857378edfbdbb1f31e831315fedf541ddf098ee | 529aefad7e94602d392f0bd86857378edfbdbb1f31e831315fedf541ddf098ee | 529aefad7e94602d392f0bd86857378edfbdbb1f31e831315fedf541ddf098ee |
| HB-VC-s5-gop/anchors.jsonl | d7deb38d87a0bec473e2f619cf0d9b87ad26737538d5da3bab8828014e77b2bb | d7deb38d87a0bec473e2f619cf0d9b87ad26737538d5da3bab8828014e77b2bb | d7deb38d87a0bec473e2f619cf0d9b87ad26737538d5da3bab8828014e77b2bb |
| HB-VC-s5-gop/graph.json | 1c2bb0c9d0dbcf3f3cea453b4c4d1682fa205c110b09e84c29cdde8c0be50768 | 1c2bb0c9d0dbcf3f3cea453b4c4d1682fa205c110b09e84c29cdde8c0be50768 | 1c2bb0c9d0dbcf3f3cea453b4c4d1682fa205c110b09e84c29cdde8c0be50768 |
| HB-VC-s5-gop/index/cols.json | b06dc0f974c5ad8a393286f069b98b750af9226de27ee7d4d9671b3acd681f18 | b06dc0f974c5ad8a393286f069b98b750af9226de27ee7d4d9671b3acd681f18 | b06dc0f974c5ad8a393286f069b98b750af9226de27ee7d4d9671b3acd681f18 |
| HB-VC-s5-gop/index/post.json | 2c0832fd2595e77fc27a80e8c2f1997a09c2fc1df6a1e0e173cc1331e475c597 | 2c0832fd2595e77fc27a80e8c2f1997a09c2fc1df6a1e0e173cc1331e475c597 | 2c0832fd2595e77fc27a80e8c2f1997a09c2fc1df6a1e0e173cc1331e475c597 |
| HB-VC-s5-gop/index/vocab.json | 4e94377a7c1cecb90a498d48affdb843476000e18f66017ae222bc2322342710 | 4e94377a7c1cecb90a498d48affdb843476000e18f66017ae222bc2322342710 | 4e94377a7c1cecb90a498d48affdb843476000e18f66017ae222bc2322342710 |
| HB-VC-s5-gop/index/ID | 134ab45d8d575df70f3ea5207be5374fb14ed5ddb37b7eaaf0da732529666e18 | 134ab45d8d575df70f3ea5207be5374fb14ed5ddb37b7eaaf0da732529666e18 | 134ab45d8d575df70f3ea5207be5374fb14ed5ddb37b7eaaf0da732529666e18 |
| HB-VC-s6-pack-cite/out/symbols_raw.jsonl | 915371c2cf481de8da5c5c00120f2a1d6cf19f9a152667e83057c790d7812ec3 | 915371c2cf481de8da5c5c00120f2a1d6cf19f9a152667e83057c790d7812ec3 | 915371c2cf481de8da5c5c00120f2a1d6cf19f9a152667e83057c790d7812ec3 |
| HB-VC-s6-pack-cite/manifest.json | 443fa412e9e9afe4421412491ba6a365422ba027e418e5072964a9bbea12218a | 443fa412e9e9afe4421412491ba6a365422ba027e418e5072964a9bbea12218a | 443fa412e9e9afe4421412491ba6a365422ba027e418e5072964a9bbea12218a |
| HB-VC-s6-pack-cite/book.md | a12049cd48504a6d91da0748c91822f63681fde055b71397e27be1a6093ab124 | a12049cd48504a6d91da0748c91822f63681fde055b71397e27be1a6093ab124 | a12049cd48504a6d91da0748c91822f63681fde055b71397e27be1a6093ab124 |
| HB-VC-s6-pack-cite/symbols.jsonl | 484709806e5b7de044e22559a1690c85f9cc15101372973ba9c782edbf661108 | 484709806e5b7de044e22559a1690c85f9cc15101372973ba9c782edbf661108 | 484709806e5b7de044e22559a1690c85f9cc15101372973ba9c782edbf661108 |
| HB-VC-s6-pack-cite/nodes.jsonl | 65dda75cf5ebb17ef90b9364bb213909a1666e9e5176b39f5a8c196b6b83c65f | 65dda75cf5ebb17ef90b9364bb213909a1666e9e5176b39f5a8c196b6b83c65f | 65dda75cf5ebb17ef90b9364bb213909a1666e9e5176b39f5a8c196b6b83c65f |
| HB-VC-s6-pack-cite/anchors.jsonl | 7f9105c97df340f5875e337bd6fb3230452c8ee03dfa5ec57f8cf5dd52322aba | 7f9105c97df340f5875e337bd6fb3230452c8ee03dfa5ec57f8cf5dd52322aba | 7f9105c97df340f5875e337bd6fb3230452c8ee03dfa5ec57f8cf5dd52322aba |
| HB-VC-s6-pack-cite/graph.json | 17395db5edbdda15a6e27515b6f4d5c2813b319747a4ef1ab7bc67ea49c066f2 | 17395db5edbdda15a6e27515b6f4d5c2813b319747a4ef1ab7bc67ea49c066f2 | 17395db5edbdda15a6e27515b6f4d5c2813b319747a4ef1ab7bc67ea49c066f2 |
| HB-VC-s6-pack-cite/index/cols.json | bdb3dbbe78f298fe8914682f13b2f27d95b53ba26d75b78483c8df46d4c59b12 | bdb3dbbe78f298fe8914682f13b2f27d95b53ba26d75b78483c8df46d4c59b12 | bdb3dbbe78f298fe8914682f13b2f27d95b53ba26d75b78483c8df46d4c59b12 |
| HB-VC-s6-pack-cite/index/post.json | 51a30ea39ebbf0260cb0160d204ef4ee5490059c62627fc8ab9c9a77c71fd12a | 51a30ea39ebbf0260cb0160d204ef4ee5490059c62627fc8ab9c9a77c71fd12a | 51a30ea39ebbf0260cb0160d204ef4ee5490059c62627fc8ab9c9a77c71fd12a |
| HB-VC-s6-pack-cite/index/vocab.json | a96698abdd9e733ed46249c3f43b8dd1e0e59a954894538617530c684b283be1 | a96698abdd9e733ed46249c3f43b8dd1e0e59a954894538617530c684b283be1 | a96698abdd9e733ed46249c3f43b8dd1e0e59a954894538617530c684b283be1 |
| HB-VC-s6-pack-cite/index/ID | dedb333ca79e211b64cab39fa0352c9b9c9b01ea5c6d18f1fda5e134d1664771 | dedb333ca79e211b64cab39fa0352c9b9c9b01ea5c6d18f1fda5e134d1664771 | dedb333ca79e211b64cab39fa0352c9b9c9b01ea5c6d18f1fda5e134d1664771 |
| HB-VC-s7-vmem/out/symbols_raw.jsonl | bcaea3464bf81320ee4f5c86d16343274f6a2041871fb300e9e6c4fb4bf98664 | bcaea3464bf81320ee4f5c86d16343274f6a2041871fb300e9e6c4fb4bf98664 | bcaea3464bf81320ee4f5c86d16343274f6a2041871fb300e9e6c4fb4bf98664 |
| HB-VC-s7-vmem/manifest.json | 5a4c7702d5c3e26643be3cd2955fd62d22b3c31e36dc0825a2f557a6c8dfd432 | 5a4c7702d5c3e26643be3cd2955fd62d22b3c31e36dc0825a2f557a6c8dfd432 | 5a4c7702d5c3e26643be3cd2955fd62d22b3c31e36dc0825a2f557a6c8dfd432 |
| HB-VC-s7-vmem/book.md | 3e6f727492ad15b7f59853ce51265e0c4bca426d8699affc4869ea52a79c713a | 3e6f727492ad15b7f59853ce51265e0c4bca426d8699affc4869ea52a79c713a | 3e6f727492ad15b7f59853ce51265e0c4bca426d8699affc4869ea52a79c713a |
| HB-VC-s7-vmem/symbols.jsonl | 311e1a8750bb99a6cdaf90855f7815e0f0420b2be1d8c4c8f892aeeea9f7287d | 311e1a8750bb99a6cdaf90855f7815e0f0420b2be1d8c4c8f892aeeea9f7287d | 311e1a8750bb99a6cdaf90855f7815e0f0420b2be1d8c4c8f892aeeea9f7287d |
| HB-VC-s7-vmem/nodes.jsonl | 09c82cd242ad202f5b853bd6685d6b03e4bb727525846512f6fe5e482e88e2b2 | 09c82cd242ad202f5b853bd6685d6b03e4bb727525846512f6fe5e482e88e2b2 | 09c82cd242ad202f5b853bd6685d6b03e4bb727525846512f6fe5e482e88e2b2 |
| HB-VC-s7-vmem/anchors.jsonl | 60f01835579ce10a14f37f929966befae7e608aaed20273cc55ea5b5474b6cd6 | 60f01835579ce10a14f37f929966befae7e608aaed20273cc55ea5b5474b6cd6 | 60f01835579ce10a14f37f929966befae7e608aaed20273cc55ea5b5474b6cd6 |
| HB-VC-s7-vmem/graph.json | f8f11b187c62e5a1c4d58e5c97620d39f8c62daeda01a986e47c5be23c1041f1 | f8f11b187c62e5a1c4d58e5c97620d39f8c62daeda01a986e47c5be23c1041f1 | f8f11b187c62e5a1c4d58e5c97620d39f8c62daeda01a986e47c5be23c1041f1 |
| HB-VC-s7-vmem/index/cols.json | f45f7546ecc53ff9d55d2929e63a51b1c2ca42cc602a5d891e3d6b666400e957 | f45f7546ecc53ff9d55d2929e63a51b1c2ca42cc602a5d891e3d6b666400e957 | f45f7546ecc53ff9d55d2929e63a51b1c2ca42cc602a5d891e3d6b666400e957 |
| HB-VC-s7-vmem/index/post.json | fc8a2d4e0596b2e74891214acba410c6a7689aa43d8ba2b1ac25e670e24a7033 | fc8a2d4e0596b2e74891214acba410c6a7689aa43d8ba2b1ac25e670e24a7033 | fc8a2d4e0596b2e74891214acba410c6a7689aa43d8ba2b1ac25e670e24a7033 |
| HB-VC-s7-vmem/index/vocab.json | e8dd047d70602ea85715eeb6d1ad327a3bced5b6be28a70da7123299cd450c88 | e8dd047d70602ea85715eeb6d1ad327a3bced5b6be28a70da7123299cd450c88 | e8dd047d70602ea85715eeb6d1ad327a3bced5b6be28a70da7123299cd450c88 |
| HB-VC-s7-vmem/index/ID | 5b9c607951a49ff31b851c7fee7a7f4d5c7dc23167889cb234ae73b6234b41d2 | 5b9c607951a49ff31b851c7fee7a7f4d5c7dc23167889cb234ae73b6234b41d2 | 5b9c607951a49ff31b851c7fee7a7f4d5c7dc23167889cb234ae73b6234b41d2 |
| HB-VN-s0s1/out/symbols_raw.jsonl | 2861fec1b9bddb733f4f0c326f5b918f84b6cf655bc129f1ef062724cbf670dc | 2861fec1b9bddb733f4f0c326f5b918f84b6cf655bc129f1ef062724cbf670dc | 2861fec1b9bddb733f4f0c326f5b918f84b6cf655bc129f1ef062724cbf670dc |
| HB-VN-s0s1/manifest.json | 510866d20576b7876713ac1714c6172ba989639a7c7babdfbedc1817b363bddd | 510866d20576b7876713ac1714c6172ba989639a7c7babdfbedc1817b363bddd | 510866d20576b7876713ac1714c6172ba989639a7c7babdfbedc1817b363bddd |
| HB-VN-s0s1/book.md | 5f1ab3f5f596268024e606498e82a1fa9b0272f895cdd18d788dad81b1f0d9a9 | 5f1ab3f5f596268024e606498e82a1fa9b0272f895cdd18d788dad81b1f0d9a9 | 5f1ab3f5f596268024e606498e82a1fa9b0272f895cdd18d788dad81b1f0d9a9 |
| HB-VN-s0s1/symbols.jsonl | f47f9c1b851487e70bafc449425b3bf0f83bb8d5e18c530ade8a3c5d4acba511 | f47f9c1b851487e70bafc449425b3bf0f83bb8d5e18c530ade8a3c5d4acba511 | f47f9c1b851487e70bafc449425b3bf0f83bb8d5e18c530ade8a3c5d4acba511 |
| HB-VN-s0s1/nodes.jsonl | 635f91a5f659580c2e5644bddfa794a832c6b2fd49d8710eb1feb75fd5b4fbe9 | 635f91a5f659580c2e5644bddfa794a832c6b2fd49d8710eb1feb75fd5b4fbe9 | 635f91a5f659580c2e5644bddfa794a832c6b2fd49d8710eb1feb75fd5b4fbe9 |
| HB-VN-s0s1/anchors.jsonl | 2e0b9e09ad58ae25fa081cb3855c7f6ccc7deb49e8109fdc3c0be8cca5945550 | 2e0b9e09ad58ae25fa081cb3855c7f6ccc7deb49e8109fdc3c0be8cca5945550 | 2e0b9e09ad58ae25fa081cb3855c7f6ccc7deb49e8109fdc3c0be8cca5945550 |
| HB-VN-s0s1/graph.json | a3b22ca10b8a01fc2e494fe9c491377874728926511e3b755fb176f39a837d51 | a3b22ca10b8a01fc2e494fe9c491377874728926511e3b755fb176f39a837d51 | a3b22ca10b8a01fc2e494fe9c491377874728926511e3b755fb176f39a837d51 |
| HB-VN-s0s1/index/cols.json | 8f67fb0adb3b6dfa7c7022d0ed613ac3520bef4e800bfd0fbfd6249c7e6a9711 | 8f67fb0adb3b6dfa7c7022d0ed613ac3520bef4e800bfd0fbfd6249c7e6a9711 | 8f67fb0adb3b6dfa7c7022d0ed613ac3520bef4e800bfd0fbfd6249c7e6a9711 |
| HB-VN-s0s1/index/post.json | 01d671791478f55309938056cbd0a74f60571a3c40686cc6c4e46f59081bce29 | 01d671791478f55309938056cbd0a74f60571a3c40686cc6c4e46f59081bce29 | 01d671791478f55309938056cbd0a74f60571a3c40686cc6c4e46f59081bce29 |
| HB-VN-s0s1/index/vocab.json | 1f7f53e0c4503b6b3e4f973db7d6944e25999316e035f25044cc135942dd72f6 | 1f7f53e0c4503b6b3e4f973db7d6944e25999316e035f25044cc135942dd72f6 | 1f7f53e0c4503b6b3e4f973db7d6944e25999316e035f25044cc135942dd72f6 |
| HB-VN-s0s1/index/ID | 5d1ccdc4c8b2184aab4481473180e3038879b52d09cdda6e0d2424329dc105c3 | 5d1ccdc4c8b2184aab4481473180e3038879b52d09cdda6e0d2424329dc105c3 | 5d1ccdc4c8b2184aab4481473180e3038879b52d09cdda6e0d2424329dc105c3 |
| eval/hb_router/out/symbols_raw.jsonl | e11120b29b7bfac1f538c369a766f73cb59c870b8e4dcac6e1fc70f5b00911e8 | e11120b29b7bfac1f538c369a766f73cb59c870b8e4dcac6e1fc70f5b00911e8 | e11120b29b7bfac1f538c369a766f73cb59c870b8e4dcac6e1fc70f5b00911e8 |
| eval/hb_router/manifest.json | 02d2cc8ddbac96c9c2f4834af999b1d39a094e18d6df279677604882763658ba | 02d2cc8ddbac96c9c2f4834af999b1d39a094e18d6df279677604882763658ba | 02d2cc8ddbac96c9c2f4834af999b1d39a094e18d6df279677604882763658ba |
| eval/hb_router/book.md | 9e969e1719524dfd202e2b6fac695d5c0c5f6e4cbb30ddd494a2822b1c8a7696 | 9e969e1719524dfd202e2b6fac695d5c0c5f6e4cbb30ddd494a2822b1c8a7696 | 9e969e1719524dfd202e2b6fac695d5c0c5f6e4cbb30ddd494a2822b1c8a7696 |
| eval/hb_router/symbols.jsonl | e83afadbbb1aa09bfea757556788d42e41f15ee25bb5dd6f4ee87ae6439f3a52 | e83afadbbb1aa09bfea757556788d42e41f15ee25bb5dd6f4ee87ae6439f3a52 | e83afadbbb1aa09bfea757556788d42e41f15ee25bb5dd6f4ee87ae6439f3a52 |
| eval/hb_router/nodes.jsonl | b051726f5c7508eff7471f79cc05e3775cf83afc59681889173e2960fdc4a531 | b051726f5c7508eff7471f79cc05e3775cf83afc59681889173e2960fdc4a531 | b051726f5c7508eff7471f79cc05e3775cf83afc59681889173e2960fdc4a531 |
| eval/hb_router/anchors.jsonl | ec3797181047f69cd19f7f122b8178cefedd6a6a81e824607029ee5fda237965 | ec3797181047f69cd19f7f122b8178cefedd6a6a81e824607029ee5fda237965 | ec3797181047f69cd19f7f122b8178cefedd6a6a81e824607029ee5fda237965 |
| eval/hb_router/graph.json | abc8dfcab68e395d10104f454b4b3092991737fe40a063e6493dd89c3e0ff416 | abc8dfcab68e395d10104f454b4b3092991737fe40a063e6493dd89c3e0ff416 | abc8dfcab68e395d10104f454b4b3092991737fe40a063e6493dd89c3e0ff416 |
| eval/hb_router/index/cols.json | 9919ece0b3629bf265ae5067369ac07b9f89999dc940f08afce9fd26ad9fcfd3 | 9919ece0b3629bf265ae5067369ac07b9f89999dc940f08afce9fd26ad9fcfd3 | 9919ece0b3629bf265ae5067369ac07b9f89999dc940f08afce9fd26ad9fcfd3 |
| eval/hb_router/index/post.json | 7ca9dbb617d25d4aa652717b096fd8a7be7b0b4ea6a925b1ed087b4498f7c90e | 7ca9dbb617d25d4aa652717b096fd8a7be7b0b4ea6a925b1ed087b4498f7c90e | 7ca9dbb617d25d4aa652717b096fd8a7be7b0b4ea6a925b1ed087b4498f7c90e |
| eval/hb_router/index/vocab.json | 1ef4df7ba876db407d857a077ee55ef953836c587ad147758fb650261826ab65 | 1ef4df7ba876db407d857a077ee55ef953836c587ad147758fb650261826ab65 | 1ef4df7ba876db407d857a077ee55ef953836c587ad147758fb650261826ab65 |
| eval/hb_router/index/ID | 43c4df9d626314d8547d6b5fe5e58742273ee87c18023b4c23bcc488544fef8f | 43c4df9d626314d8547d6b5fe5e58742273ee87c18023b4c23bcc488544fef8f | 43c4df9d626314d8547d6b5fe5e58742273ee87c18023b4c23bcc488544fef8f |

## HBT2B: provenance refresh and multi-language anchor standard

The HBT2 implementation hashes below are copied_sha256 refreshes only;
source_sha256 stays unchanged. The HBT2 entries and artifact SHA table above
remain historical evidence. New HBT2 files have no source copy and therefore
retain their AUSENTE source entries above.

| File | Previous copied SHA-256 [MEASURED] | Refreshed copied SHA-256 [MEASURED] |
|---|---|---|
| hbtools_v2/hb_build.py | aacdd7761212a0f19ad068d231c14fc1c5e8575b019411ac89243208095004db | 8e9f4481abb04073f6be963b19cc1bf86e0da88071ac3b0e2930ccf870d7194a |
| hbtools_v2/hb_extract2.py | 452f24b8f9a341b57200b87765a9df7794a415c3a364ff49422f4c5fb15b825a | 275ddfcab7f3bc86a3ac0ad4a32557846508ef9056cb79d207923fc49b29a0af |
| hbtools_v2/hb_index.py | 48322291554e3bfeb2d1ef1836332654a0c946d1d088e40cc24570bfe054dafe | 8e97667d34fca82d4322bcb17ac41e8b585d3e51fdf242db6ac71545542f25c8 |
| hbtools_v2/hb_scaffold2.py | 80dc1e5fe8cdb85006f20c2047fbe1a79e1c20ac5464e14aa9aee82de3191f9d | 338814b4ae22f04f90ded116a9eae1a6c7ce638339b64ccfc9cd8877967dbbbb |

### Standards patches

Reason: multi-language HyperBooks, U-DEC-0018 / os-cu-0 W5a. Add go, rust,
typescript, javascript, c, cpp and wasm to the anchor language enum while
retaining every previous value, including other. The schema gate accepts a
verbatim copy or an exact standards patch row binding path, kind, source hash
and copied hash; missing records, tool records and incorrect hashes fail.

| File | Kind | Old/source SHA-256 [MEASURED] | New/copied SHA-256 [MEASURED] |
|---|---|---|---|
| standards/schemas/hb.anchor.v2.schema.json | standards patch | e8231a634665c757812a9b78c5790837931bc64bdc97759b7bdb3f003731c056 | 507b3ceb0c953be7356d2f1a8dce52f7fa54939f0b416217928b9ac61e58b513 |

### HBT2B verification changes

The Go fixture supplies explicit producer byte spans and AST-hash evidence,
then builds and validates persisted anchors with resolved standard schema
references. It rejects unknown language values and checks the full retained
and expanded enum. Sparse producer evidence remains absent; no extractor
default or book artifact changes. The schema negative mutates a scratch copy
and refreshes its copied hash, so the standards-patch requirement is exercised
independently of the basic file-hash check.

| File | Previous SHA-256 [MEASURED] | Current SHA-256 [MEASURED] |
|---|---|---|
| tests/test_hbv2_wiring.py | 84653b9d401593bd09f5de8cd11e72443558c5656bf38b2d716935b58df6fad6 | b5bdc8f7ffe7ec17f045500e56bc158dce32a320bbddbd57a3ea578f3ea722f7 |
| tests/test_hbtools_bundle.py | 6a441a2cfe3b9d2d3abc923ace0bec8ee62dcc45c241b3510b97d0edf6381bb9 | 7cbdad02c55c6e296f3c4d4a862b1de0b0fbd8198572277af803f8ef6e355480 |
