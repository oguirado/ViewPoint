# File routing for agents

This document describes the local file router vp_route as it stands at router closure (12 Nov 2026). The router is an optional evidence tool over one verified index generation. It is not the only way to read the repository, and nothing in this document changes that. Plain ripgrep (rg) and local dictionary tools remain valid ways to find code, and the router code itself is frozen: it changes only by an explicit operator ruling [CITED: CP-W3-router-closed.md L17].

The router holds the accepted main generation indexed from product commit c95bd41: generation g-59c0ee55d0912cbb50f7, with 11,820 files indexed and 9 excluded by declared policy; the full build took 284 s with 1.95 GB peak [CITED: CP-W3-router-closed.md L7]. A where query over the whole repository took 0.11-0.13 s (against 25.7 s with the earlier study router) [CITED: CP-W3-router-closed.md L8]. These figures are cited as recorded at closure, not fresh measurements by this document. The acceptance gate at closure was 11 of 11 suites OK [CITED: CP-W3-router-closed.md L6].

## What the router answers

The router exposes one command boundary with eight verbs. Seven verbs work on local code; papers queries an external research router. Lookups never rebuild the index: selection opens the verified generation once, and where, deps, task, papers and evidence never invoke refresh or publish [CITED: U08.md L17-22; cli.py L529-L530; cli.py L682]. The router is a thin layer over the index it was built from: it routes and reports, and never reconfigures the underlying index or runtime [AGT03.05, pack L91; L113-115].

- where ranks files and symbols for a query string. It is the entry point: find candidate coordinates first, then verify against source bytes.
- deps walks the static import and include graph from one path or symbol key. Edges state their authority (syntax_import, syntax_include, resolved_module, call_candidate); no edge proves runtime dispatch.
- changed reports commit-to-commit history with explicit base, target and optional parent object identities. It is the only verb that carries history; task packs point here instead of inventing history.
- task assembles a context kit: ranked anchors, declaration spans, heading anchors, static edge witnesses, cited neighbour files, matched tests and documentation, each independently cited. Paper scores never compete with local scores in the same ranking.
- evidence resolves compact citation references into full byte citations for one verified generation. It never fabricates spans and never rebuilds the index.
- refresh builds a new generation from explicit repository roots, pins, trees and versions. It is the only verb that writes.
- check verifies generation integrity and supplied pins without rebuilding.
- papers queries the local PapersHub research router for paper context. Paper hits carry upstream mount, index, id and locator instead of a local citation, and upstream abstention is preserved rather than overridden.

Two selection modes serve every lookup verb. Accepted-current mode requires an explicit pin for every repository in the generation; pinned mode names an exact generation. Combining both rejects. A requested pin that differs from the selected generation yields STALE carrying the generation's original citations, and a moved repository HEAD yields STALE even when the old pin is named explicitly [CITED: U08.md L24-30]. Without explicit repository roots, only the accepted pins establish freshness. The router root is never inferred as a source repository, and no dirty-worktree lookup mode exists.

## How answers are shaped

Every lookup returns one canonical JSON object on stdout, with timing on stderr. Six statuses are frozen, each with its own exit code.

| Status | Meaning | Exit |
|---|---|---|
| OK | Complete within the declared indexed scope | 0 |
| NO_MATCH | No indexed match; never a claim of source absence | 1 |
| ERROR | Backend or verification failure; hits empty, explicit gap | 2 |
| PARTIAL | Useful evidence with explicit gaps, including empty results | 3 |
| STALE | Requested accepted identity differs from selected evidence | 4 |
| BUDGET_EXHAUSTED | Required output exceeds rows or bytes; explicit gap | 5 |

Status mapping is frozen; argument and generation-verification failures before a verified manifest emit only the diagnostic, never an invented generation or pin [CITED: U08.md L88-102; spec.md status table].

Local hits use the full worker pack v1: schema, generation, query, intent, mode, repositories, versions, status, producer_status, hits, coverage_gaps, continuation, budget and tokens. Each hit carries group, key, an integer score, authority, a byte citation and an upstream field; local hits have upstream null [CITED: spec.md wire contracts]. Scores compare within their group only. Tokens stay null: no exact o200k adapter was introduced, so token counts are never estimated from bytes [CITED: U08.md L65-71]. Budgets default to 20 rows and 2,000 canonical UTF-8 bytes [TARGET]. These are ceilings, not a capacity promise: twenty minimal shortlist rows alone need 3,601 bytes [MEASURED], and typical compact pages hold 3-4 rows [CITED: U12.md smoke table]. Request more rows or bytes, or follow the continuation cursor: every nonfinal page binds its position, and row overflow becomes another page rather than exhaustion [LX-0025, pack L263; L267; L279-281].

where and deps default to the compact shortlist v2, with each hit reduced to key, score, authority, repository, path, start and end lines, and one full citation ID. The compact reference does not authenticate source bytes; evidence resolves it into the full citation row. The full v1 hit form remains available on request and is the default for task, papers and evidence [CITED: U12.md L20-25; spec.md shortlist section].

Every coverage line carries a type prefix. Lines beginning policy: state declared scope and exclusions and do not downgrade a complete result; lines beginning gap: state real omissions and force PARTIAL. Real gaps prevent an otherwise clean OK or NO_MATCH; STALE, backend ERROR and output-budget failure retain their distinct outcomes [CITED: spec.md U13c section; vp_router cli.py:590-591]. Unknown untyped diagnostics become gaps, never policy notes. An empty result after budget exhaustion, an empty result with no indexed match, and an empty result with omitted evidence are three different answers; this document keeps them distinct and never merges them [LX-0025, pack L288-C4; L283-287].

## Exclusions

Two separate exclusion systems apply. Lookup traffic filtering is selected by the generation's versions.policy. Under vp-path-policy-v1, a first path segment equal to .wavemind, .agents, .codex or .claude is agent traffic. Under vp-path-policy-v2, those exact, case-sensitive names match any path segment, including beneath components. Unprefixed bridge, agents, codex and claude do not match this traffic rule. Default scope excludes the matching occurrences before lexical ranking; exact path and declaration keys can override it, and --scope all includes them with a policy note [CITED: U12.md L17-19; L27-30; spec.md scope section]. Existing generations retain their recorded policy. An exact bare path, a repository-qualified path, or an exact symbol key bypasses the exclusion; a repository key alone does not. Scope all includes traffic explicitly and reports the inclusion [CITED: U12.md L17-19; L27-30; spec.md scope section; vp_router rank.py:104; vp_router rank.py:108-112; vp_router rank.py:585-593; vp_router cli.py:322-348]. Dependency edges with excluded traffic endpoints are omitted unless the endpoint itself is an exact requested key, and unresolved references remain limitations [CITED: spec.md scope section; vp_router cli.py:322-348].

The guards apply a second, privacy-level policy before any byte is read: denied names, credentials and semilla files, symlinks and aliases are refused before access, and root containment plus pinned membership precede every read [CITED: spec.md ownership table]. Declared member exclusions for denied names, non-regular files and oversize files are reported as policy lines; guard refusals whose reason is unknown are real gaps [CITED: spec.md U13c section].

Zero-byte occurrences keep membership and path postings but never receive a fabricated span: a matched empty file yields PARTIAL with a repository and path gap, not NO_MATCH and not a zero-length citation [CITED: U08.md L34-38; spec.md generation schema]. Unmatched lexical terms remain gaps, and empty indexed postings prove no indexed match, never absence from source [LX-0025, pack L288; FA-0012, pack L47-L49; L73-75].

## Known limits

These limits are part of the closed contract. They are not defects in the verbs above; they bound what any answer means.

- No usefulness number exists. The sealed usefulness bench (24 model attempts) was never run: its runner failed its first live smoke and was abandoned unmerged, so ranking quality and accuracy claims are AUSENTE [CITED: CP-W3-router-closed.md L12; U12.md L156]. Order is not evidence delivery: rank, candidate set and delivered bytes are separate measurements [LX-0025, pack L263; L267].
- No automatic refresh. Refresh is manual after each accepted batch [CITED: CP-W3-router-closed.md L15]. A generation remains evidence for its recorded commits. In accepted-current mode, supplied repository roots let the router compare current HEAD/tree with that generation and report STALE on drift [CITED: vp_router cli.py:202-226]. Without those roots it compares only supplied identities; it does not discover a moved branch automatically. Pinned mode intentionally selects historical evidence. Preserve STALE as its own outcome; do not relabel it PARTIAL. Distinguish current accepted code from historical routes, and treat missing evidence as a PARTIAL omission, never as zero [FA-0016, pack L32-L34; HB-0005, pack L38-L40].
- PapersHub export of the product corpus is parked. Exporting this corpus would collide in a four-digit id space at 11,820 files, and remains unmerged [CITED: CP-W3-router-closed.md L14].
- The papers adapter caps its upstream shortlist at 20 rows with no continuation, so completeness is unproven and a full upstream shortlist never supports a global absence claim [CITED: cli.py L448-450; U08.md L118].
- PARTIAL never rounds up. A lookup hit, a resolved symbol and a plausible sentence together do not make a verified claim: every factual statement in reusable documentation still needs its scope, class, source identity, inspected coverage and preserved limitations checked against source, not only its lookup hit [LX-0041, pack L220; L232; L218]. Qualifications travel with the claim; omitting them makes an accurate sentence inapplicable [SI-0032, pack L136; L154-C5; L146].
- Historical figures in router design notes are not current evidence. Every number in this document is tagged [MEASURED], [CITED] or [TARGET]; a bare number is rejected [VG-TOOL-0214, pack L302-305].

## Invocation and change navigation

The vp_route command in this section is an optional external tool. VP_ROUTER_CODE names the accepted router checkout supplied by the caller, INDEX_ROOT owns its router generation store, SOURCE_ROOT is the source worktree, and REPO_ID, COMMIT, TREE and GENERATION come from the admitted manifest. These are caller-supplied placeholders, not installed defaults. Pins and trees require full lowercase object identities; the abbreviated historical product commit in the introduction is not a valid CLI pin [CITED: vp_router cli.py:101-116]. Templates below are not claimed successful runs:

    python3 -B "$VP_ROUTER_CODE/vp_route" where "<query>" --root "$INDEX_ROOT" --pin "$REPO_ID=$COMMIT" --repository-root "$REPO_ID=$SOURCE_ROOT"
    python3 -B "$VP_ROUTER_CODE/vp_route" where "<query>" --root "$INDEX_ROOT" --mode pinned --generation "$GENERATION"
    python3 -B "$VP_ROUTER_CODE/vp_route" evidence "<citation_id>" --root "$INDEX_ROOT" --mode pinned --generation "$GENERATION"
    python3 -B "$VP_ROUTER_CODE/vp_route" check --root "$INDEX_ROOT" --generation "$GENERATION" --pin "$REPO_ID=$COMMIT"
    python3 -B "$VP_ROUTER_CODE/vp_route" changed --root "$SOURCE_ROOT" --repository "$REPO_ID" --base "$BASE" --target "$TARGET"

Repeat --pin for each repository in accepted-current mode. --root names the generation store for lookup verbs and the source Git worktree for changed; --generation alone is not a valid default-mode lookup [CITED: vp_router cli.py:124-160; vp_router cli.py:250-254; vp_router cli.py:659-671]. Use --max-rows and --max-bytes to change ceilings; pass the returned continuation object as JSON through --cursor while retaining the bound request options [CITED: vp_router cli.py:124-160; vp_router cli.py:278-303]. --format full requests full hits for where/deps. For dependency traversal use --direction in|out|both and --depth; task --include-history reports a limitation and directs you to changed. task --papers and papers --mount select research context [CITED: vp_router cli.py:124-160].

Authorized refresh uses the same placeholders: python3 -B "$VP_ROUTER_CODE/vp_route" refresh --root "$INDEX_ROOT" --repository-root "$REPO_ID=$SOURCE_ROOT" --pin "$REPO_ID=$COMMIT" --tree "$REPO_ID=$TREE" --versions "$VERSIONS_JSON". All repository mapping sets must match and the versions object must explicitly name extractor, rank, policy, tokenizer and docs [CITED: vp_router cli.py:671-684]. These examples do not authorize implementation changes; router implementation changes still require an operator ruling.

The product's python3 route/route_query.py interface remains a separate tool described in route/README.md; its any, show and ROUTE_OK protocol is not the external vp_route wire contract, and route_query.py check must not be substituted for generation verification. Router implementation changes concern the separate accepted checkout (vp_router cli.py, rank.py, guards.py, generation.py, update.py and their tests). Refreshing either index is an owner action. In citations in this document, bare cli.py, rank.py, spec.md, U08.md and U12.md names refer to that separate accepted vp_router checkout, not to this product tree. The closure checkpoint is identified by its file name CP-W3-router-closed.md outside the product tree.

## Rebuilding and verifying

Rebuild only with matched inputs: every repository needs an explicit root, pin and tree, plus an explicit versions object naming extractor, rank, policy, tokenizer and docs. Refresh delegates to the generation lifecycle, which stages, verifies artifacts and publishes exactly once [CITED: U08.md L120-125; cli.py L675; L682]. Existing generations keep their recorded policy; refreshing to nested traffic filtering requires vp-path-policy-v2 to be selected explicitly in the versions object [CITED: U12.md L18; vp_router rank.py:104-112].

Check verifies the generation and compares supplied pins without rebuilding [CITED: U08.md L120-125]. Refresh and check run by hand after each accepted batch of changes; the hub export is registered by the papers owner, not by the router [CITED: CP-W3-router-closed.md L15]. The router never refreshes on lookup and never edits an index that a running flight reads.

The generation this document describes is pinned above as g-59c0ee55d0912cbb50f7. If a future generation is admitted, this document is stale until its pins, figures and limits are updated. Update this guide when the accepted generation changes; lookup freshness follows the explicit identities, selection mode and optional repository-root witnesses described above [CITED: vp_router cli.py:202-226; vp_router cli.py:590-591; FA-0016, pack L32-L34].

## When to use rg instead

Use plain ripgrep when the router cannot add value:

- One literal string in a small known file, or a known short line range: a direct text tool is faster and returns raw lines without envelope overhead.
- Work outside the indexed generation. The router answers only for repositories and commits inside its pinned generation; a file added after the last manual refresh is invisible until the next batch [CITED: CP-W3-router-closed.md L15].
- When the exact bytes matter, bypass the shortlist. Compact references do not authenticate source bytes, so read the file or resolve evidence before quoting it [LX-0041, pack L218-L224].
- A NO_MATCH, PARTIAL or BUDGET_EXHAUSTED is not an answer about the repository: rerun with wider scope, larger budgets, or rg, and keep the three outcomes distinct [LX-0025, pack L288-C4].
- Do not cite router hits as evidence in dated documentation without re-verifying against source. Historical ranking and latency numbers set no current guarantee [LX-0025, pack L292-C8; SI-0032, pack L134].

## Amendments

Amendments to the router itself require an operator ruling; amendments to this document update the pins and figures above instead. Related improvement register entries are IMP-29 (router quality, latency and deferred references) and IMP-43 (router refresh, commit isolation and ref hygiene) [CITED: improvements register].
