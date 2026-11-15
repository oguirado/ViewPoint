# ViewPoint monorepo gates

## Source ownership

Answers: which scanner owns components; how does ViewPoint delegate imported tests.

`OWNERSHIP.json` is the shared source boundary for the root router, documentation
index and query reader, privacy sweep, network lifecycle scanner and process
audit. Root paths remain covered. Only declared component prefixes are delegated;
an unknown component path or invalid manifest fails closed. Root symlink refusal
and existing planted negative controls remain in force. The component suites are
mandatory in the local parent aggregate. A root-only result cannot certify them.

Imported privacy and process findings remain component policy findings. Moving
their checks across the boundary does not remediate those findings. Component
suite failures and missing policy coverage require explicit follow-up; they must
not be described as a green monorepo gate.

## Accepted history

Answers: what history do commit laws audit; where is the import baseline recorded.

`LAWS_BASELINE.json` records the peeled MetaViewer release tag, component pins and
accepted subtree merge commits. The merges are part of the accepted bootstrap:
excluding only the pins would still audit their pre-existing import subjects.
`tools/check_laws.py --owned` verifies every baseline object and its ancestry,
then audits commits reachable from HEAD after excluding that accepted universe.
Unrelated local branches and stash commits are outside this branch's history.
Identity, forbidden-message, imperative-subject, version and changelog rules stay
enabled. Missing or invalid baselines fail. Changes to the accepted baseline or
future component pins need operator review; they are not automatic exemptions.

## Local wrapper

Answers: how to run the arm64 parent gate; where are component results retained.

The workspace's `ctl/vp_gate.sh` invokes `tools/vp_gate.py`. With no arguments it
runs every root leg, then every inventoried component suite. `--leg L` runs a
root leg and `--components` runs the component step. The wrapper resolves the UI
kit pin from `ui/kit.lock.json`, makes a separate checkout from existing local
objects and verifies HEAD. The existing sync tool still verifies repository and
content hash. No fetch or download is performed.

The Wender resolver admits the imported subtree only after proving its tree ID
against `deps.lock`, checking suppressed index entries, hashing tracked bytes and
rejecting untracked executable or Python payloads. Standalone Git and wheel
provenance checks remain intact. The wrapper supplies the owner-fixture Wender environment
variable, a qualified disk-backed scratch directory, offline package settings,
bounded Cargo concurrency and a bounded cold-build deadline. Go retains its
separate short temporary-directory policy because its test names consume Unix
socket path space. The wrapper separately qualifies that directory as disk-backed;
an unqualified filesystem blocks execution. Runtime symlink refusal is preserved. The unchanged
reconciler fixture receives an owner-held artifact with group/other write bits
removed; no source bytes or test assertions change for this provisioning step.

Each test/build command runs in its own memory-capped scope. Native component
builds precede their consumers and retain the crate-local artifact paths those
consumers expect. Component-owned lock checks stay intact; the wrapper does not
add a lock-file requirement to an upstream build that has none. The full OpenSuperficies gate receives an outer budget longer than its own final
mutant stage; all memory limits and test assertions remain intact. Component commands
run in disposable pinned local checkouts under short physical `ctl/w/` paths, so imported source
trees stay untouched. Per-file health reports go outside those checkouts, preserving
the component tests' repository-scope controls. The peer wheel comes from the pinned vision component;
OpenSuperficies still checks its original wheel witness. Upstream heavy-file,
host-only and live-browser exclusions remain visible alongside separate full
discovery results. Bridge acceptance needs an active task selection and reviewed
commands from the operator. Missing live prerequisites are BLOCKED.
The OpenSuperficies CI race selection includes a live pointer test and runs
inside a dedicated Xvfb display; its original test and package selections remain
intact. Missing Xvfb blocks that suite.
State-budget runs as a module from its pinned component root. MemoryWeb runs its
self-contained `entry-check` command. TypeScript W2 runs from the component root
because its tests resolve their child CLI from that directory.
Short checkout paths also preserve the component's repository-local Unix socket
budget. No symlink alias is used. Logs and receipts remain under `ctl/runs/`.

Receipts under `ctl/runs/` retain every command, working directory, return code,
elapsed time, output hash and log path. A nonzero result or BLOCKED suite prevents
a successful aggregate. Root checks and component checks are separate evidence.

## Verification

Answers: which negative controls protect the ownership and laws changes.

`tests/test_monorepo_ownership.py` plants root and component violations, proves
only the root paths reach the scanners, refuses undeclared ownership, and retains
root symlink refusal. `tests/test_monorepo_laws.py` builds a real commit graph,
rejects a forbidden new commit, excludes an unrelated ref, and rejects a missing
or invalid baseline. `agents/tests/test_monorepo_subtree.py` rejects altered
bytes, modes, suppressed index entries and untracked Python payloads.

The mandate and reported conclusions are limited to the supplied local corpus
and retained arm64 execution evidence.
