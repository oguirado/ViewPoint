# C3: W8 coordination, replay and receipt collection on OpenSwitchboard

## Status, source binding and precedence

This is a proposed host-side protocol and an executable, stdlib-only interface model. It does not implement or certify a deployed Switchboard adapter, capsule runtime, consumer, billing provider or memory service. Production control-plane code is Go; Rust requires a measured hot path; no new VMM is proposed. The base design makes this same host/runtime distinction (`docs/design/W8_WORKER_BANK.md:28-34`).

Every repository citation in this document is relative to the bound commit **`c8cb7dc8c0946be08edcb2a419629b6debd33c96`**, resolved from `bridge/integration` through the GitHub connector. All bound file blobs fetched at that ref were also compared with explicit reads at this commit. Citations to packs describe what those bound packs report; they do not assert that their external absolute-path implementations were inspected. Those paths are provenance, not additional inputs (`docs/design/w8d/README.md:25`).

Numeric convention: **[CITED]** marks inherited constants, **[TARGET]** marks proposed limits, arithmetic and synthetic fixtures, and **[MEASURED]** is reserved for observations actually made and reported in the collection report. All numeric literals in proposed wire examples, identities and reference-model fixtures are [TARGET] unless expressly identified as [CITED]. Version names, question labels, hashes, task identifiers and citation coordinates are identifiers, not measured quantities. No timing or throughput benchmark is claimed.

A4 takes precedence over older packs and router proposals (`docs/design/w8d/README.md:7-13`). The immutable W8 schema and W8 Worker Bank v3 remain unchanged. Transport metadata belongs in a sidecar: the existing `speech_act`, `receipt` and `scheduler_lease` objects reject additional properties (`contracts/worker/os.cu.worker-task.v1.schema.json:1739-1878`, `:2896-2994`, `:2995-3079`).

### Security boundary and resolved input conflicts

The only runtime plan is the A4 hardened base image, a verified per-task tmpfs clone, host-only inference, text-only tar-over-exec extraction, rootless or dedicated-user daemon, read-only rootfs, `cap_drop ALL`, no-new-privileges, no bind mounts, env-files or runtime socket, bounded resources and no egress by default. `/dev/kvm` is allowed only in the later microVM phase. Frames remain exclusively in `/dev/shm`; no frame or encoded frame is an admissible result body. The warm dependency cache is a named volume read-only to tasks; it is not a host bind (`docs/design/w8d/W8_AMENDMENTS.md:22-27`; `docs/design/W8_WORKER_BANK.md:117-147`, `:233-236`; `AGENTS.md:14-16`).

The host seat extracts complete, approved text and builds receipts from its grant and observations. Guests have no Switchboard connection, credentials, identity signing keys or inference client. Unix peer credentials identify the host process/VMM, not an application inside the guest; the broker binds an endpoint to its trusted launch record (`docs/design/W8_WORKER_BANK.md:185-207`; `docs/design/w8d/research/R6.md:58-82`). UTF-8, a permitted extension and a matching digest prove transport shape/integrity, not semantic absence of secrets or encoded pixels. Export only host-approved typed evidence and byte anchors; raw stdout/stderr remains in bounded RAM rings. Actual extraction/sanitizer and STORAGE LAW proof remains a runtime gate (`docs/design/w8d/packs/D6.md:45`).

| Input conflict | Resolution in this contract |
|---|---|
| Per-commit image proposals in `docs/design/w8d/router.json:291,311` and the older amendments | Superseded by A4; record base-image, recipe, cache and verified commit identities separately. |
| Truncation proposed as injection protection in `docs/design/w8d/packs/D4.md:100` | Preserve complete typed bytes; treat message bodies as untrusted data. Preview truncation cannot protect or establish evidence. |
| Guest TLS keys proposed in `docs/design/w8d/research/R6.md:74-83` | No guest credential exception. Authenticate the host seat and launch endpoint, respecting A4. |
| Capsule-wide revocation reported in `docs/design/w8d/packs/D4.md:90` | Require a destination-scoped C2 fence transaction before reassignment; sibling targets remain live. This scoped extension is proposed, not claimed to exist. |
| Native role names `verify`/`critique` reported alongside a closed act catalog in `docs/design/w8d/packs/D4.md:42-45` | A pinned adapter must prove the supported intersection of role and act. Unknown combinations fail closed; no invented native act. |
| Native stagnation return code **1 [CITED]**, evidence error **64 [CITED]**, and router budget exit **64 [CITED]** | Preserve the native value as evidence, normalize the W8 outcome, and never return those values from W8 code (`docs/design/w8d/packs/D4.md:43,52`; `docs/design/w8d/router.json:169`). |

## Routing and the unchanged W8 payload

**Mailbox is the default.** An admitted attempt has a private origin mailbox and a single wave collector. A topic room is granted only when an authenticated finding will retarget related leases, or when a bounded evidence review requires multiple authorized participants. The grant identifies topic, repo/commit, affected targets, membership, expiry and allowed acts. Joining does not grant an actuator, writer capability or a new billing reservation. Finishing a room conversation does not finish an attempt (`docs/design/w8d/W8_AMENDMENTS.md:13-14`; `docs/design/w8d/packs/D4.md:9-17`).

| Unchanged W8 `act` | Native act | Required interpretation |
|---|---|---|
| `finding` | `claim` | Candidate manifestation with canonical `repro_sha256`; not a ratified finding. |
| `repro` | `evidence` | Complete source evidence plus an authenticated verifier score. |
| `affected` | `query` | Proposed retarget paths; C2 separately authorizes any reassignment. |
| `status` with `working` or `done` | `commit` | Progress or terminal candidate; `done` is not consumer acceptance. |
| `status` with `blocked` or `stuck` | `yield` | Stop fresh work and report the typed reason; do not silently retry. |

This mapping preserves the native catalog rather than adding the W8 names to it (`docs/design/w8d/packs/D4.md:42-46`). The W8 body is the exact received UTF-8 JSON byte sequence, including any valid original whitespace. Parsing validates it; transport does not pretty-print, normalize strings, append fields or reserialize it. Its closed field set remains `schema, act, wave_id, task_id, attempt_id, lease_id, seq, repro_sha256, affected, status, body`. Inherited caps are **2048 [CITED] Unicode characters** for `body`, **64 [CITED] affected paths**, and **1000000 [CITED]** for `seq`. Finding/repro require a repro hash; the `status` key is always present and is non-null only for status acts; affected requires a nonempty list (`contracts/worker/os.cu.worker-task.v1.schema.json:2995-3147`).

Full evidence exceeding the W8 `body` cap travels as additional approved text objects in the host extract manifest. It never enters a new W8 payload field. Length limits reject an entire object; they never produce an apparently complete prefix. The existing evidence allowance is **102400 [CITED] bytes per attempt**, receipt first (`docs/design/W8_WORKER_BANK.md:147`). Allocation for envelopes, indices and replay state is additional, explicit host admission, not free memory.

### Native fields and C3 sidecar

The native framing reported in the pack is `lamport, msg_id, from, ts, kind, env`; its envelope reports `v: ETP/1`, `id`, `act`, `pulse`, `sender`, `receiver`, and typed `nodes` with `body_ref`. Canonical message identity is position-bound, while a body reference hashes complete body bytes. A body must exist before a record names it (`docs/design/w8d/packs/D4.md:23-38`).

The proposed host adapter has explicit `publish_mailbox`, `publish_topic`, `fetch_complete_body` and `read_after_cursor` operations. These are C3 interface names, not established native CLI parameter names. Each native body reference must resolve to the complete immutable W8 object or to a typed C3 sidecar that references that object. The selected native codec pins which node carries the sidecar and proves byte parity before deployment; an unsupported node shape or absent full-body retrieval is `unavailable`.

| C3 sidecar information | Encoding and validation |
|---|---|
| `binding` | Full lease tuple below, resolved against a host-owned active grant. Never supplied as authority by the worker. |
| `source_body_ref`, `source_body_bytes` | Native `body_ref` resolved as `sha256:` plus **64 [CITED] lowercase hexadecimal characters** and exact UTF-8 byte length. Validate by full match, not a regex that admits a trailing newline. Fetch/hash/length/schema checks precede use. |
| `extract_manifest_ref`, `evidence_refs` | Complete text object identities and lengths, extraction assertion record, and trusted host provenance. A summary cannot substitute for an evidence object. |
| `route`, `topic`, `receiver` | Private mailbox or an explicit current topic grant; recipient must be authorized for the same wave/commit. |
| `sender_extension`, `native_act` | Chosen by the trusted host adapter from the authenticated seat and mapping above. Caller values must agree exactly. |
| `attestation_ref` | Authenticated evidence-verifier record, bound to body, repro identity and the full lease tuple. Required for evidence-sensitive native acts. |
| `bus_epoch`, `origin_id`, `cursor`, `previous_digest`, event identity | Collector replay chain and checkpoint fields defined below; native Lamport and original W8 `seq` remain in their respective source records. Transport positions never determine receipt or billing identity. |
| `request_id`, `attempt_key`, `receipt_id` | Separate stable identities for requested work, paid execution and terminal bytes; omitted where that event kind does not use them. |

The content-reference grammar and native body addressing are reported in `docs/design/w8d/packs/D4.md:27-30`. Room reads can truncate to **240 [CITED] runes**, and whisper to **180 [CITED] runes** (`docs/design/w8d/packs/D4.md:56`). Neither preview is a collection source. The adapter must obtain the complete bytes through `body_ref`; if it cannot, it returns `unavailable`. The fixture deliberately uses a valid W8 body larger than either preview.

## Sender, score and evidence authentication

The lease sidecar binds:

`request_id, logical_request_sha256, wave_id, task_id, attempt_id, lease_id, target_id, worker_id, epoch, source_commit, task_sha256, lease_nonce, seat_grant_sha256, backend_surface`.

The admitted identity snapshot also binds authenticated `host_id`, numeric native `extension`, hard lease `expires_ms` and bounded host-text `collect_until_ms`. These are immutable admission fields, not wall-clock readings refreshed on retransmission. `epoch` is the target fencing epoch; `bus_epoch` is a different namespace. No epoch field is inserted into the existing closed `scheduler_lease`. The receipt's existing task/attempt/lease/seat/digest fields remain the authoritative cross-checks (`contracts/worker/os.cu.worker-task.v1.schema.json:1745-1768`; `contracts/worker/os.cu.worker-task.v1.schema.json:2896-2994`).

The native wrapper's numeric `SBD_EXTENSION` check is a syntax requirement, not authentication (`docs/design/w8d/packs/D4.md:44`). Admission authenticates a host connection, looks up its current grant, checks expiry and epoch, then supplies the native sender itself. A worker cannot obtain authority by selecting an extension, reconnecting, choosing a different room, or copying another task's lease JSON. Host credentials remain outside capsules and outside payloads. Host-broker/VMM endpoint mapping is trusted only within that explicit boundary (`docs/design/w8d/research/R6.md:63-83`).

The verifier attestation contains the complete evidence/body identities, canonical repro hash, full binding, verifier principal, verifier policy/build identity, score, and freshness scope. The collector authenticates the attestation through the pinned host verifier channel or a verified host signature. A claimed `verified: true`, Merkle-looking string, or high numeric score is not proof. Evidence quotes must be byte-anchored to complete approved extracted text, not to a worker-written summary; the bound pack explicitly warns about score-only acceptance (`docs/design/w8d/packs/D6.md:22,44`).

Internally the proposed score is integer `score_milli` with an accepted evidence range **800..1000 [TARGET]**. The host formats it as an exact decimal string for the native **0.80..1.00 [CITED]** gate; it does not use binary floating-point or round a failing value upward (`docs/design/w8d/packs/D4.md:43`). The adapter must also provide the authenticated evidence root in the native verifier's validated Merkle-reference grammar; a SHA-shaped body reference is not automatically a Merkle proof. Missing root semantics or field mapping fails unavailable. The actual native score producer, checker build, end-to-end authentication and role mappings remain deployment prerequisites, not facts established by a schema or by this model.

## Admission, flood control and reassignment

Admission serializes and bounds the complete record before reserving space. Each authenticated worker's ordinary-message allowance is shared across its mailbox, topics and reconnections. A host monotonic clock selects the pulse; a caller-supplied timestamp or pulse cannot refill quota. Counter increment and append authorization are atomic. At the same clock tick, send number `limit + 1` **[TARGET]** is refused before body publication or bus append. A watcher that discovers a flood later cannot satisfy this rule.

The reference tests use a deliberately small synthetic quota and fake clock. They pin exact-limit success, first-excess refusal before time advances, refusal just before the pulse boundary, admission at the exact boundary, no room-switch/reset bypass, and no state mutation on denied publication. Configuration is finite and operator/admission-owned; fixture numbers are [TARGET], not an inferred production knee. Native watcher samples and energy thresholds cannot establish these immediate semantics (`docs/design/w8d/packs/D4.md:50-57,114`; `docs/design/w8d/math/M8.md:33-47`).

`vigia` consumes authenticated counters and progress records. Malformed, saturated or missing progress cannot open a gate. A stopped sender loses ordinary send authority, but reserved finalization and authenticated control paths remain available. A status of `HOLD`, `SATURATED` or unknown cannot be treated as success merely because a saturated number compares equal to a threshold (`docs/design/w8d/packs/D4.md:50-57,73`).

Duplicate work means a redundant active assignment for the same task/commit and verified canonical repro identity, not merely a similar text or histogram. C2 journals the old target's interrupted outcome, retains its cost/resource liability, advances that target's fence, and issues a replacement attempt only with an independently admitted reservation. The old worker cannot publish a fresh finding or actuate after the fence. Historical terminal bytes already extracted remain collectible under their old tuple through a collection-only path using the retained authenticated grant and extraction record from that epoch; they do not revive its execution authority or pass fresh-message admission. A sibling target keeps its own lease and epoch. Finding dedup preserves all contributing workers, evidence refs, paid attempts and failures (`docs/design/w8d/packs/D4.md:57,88-90,115`; `docs/design/w8d/synth/S3_FOLDIN.md:33`).

## Canonical repro identity and exact dedup

Repro identity groups an explicitly defined normalized manifestation under a pinned environment. It is not a proof of semantic root-cause equivalence. Crash observations, coverage, patch candidates and evaluation jobs need different identities (`docs/design/w8d/research/R5.md:1-11`). The canonicalizer is a versioned pure function; its input fields and order are frozen by the inline golden vectors.

Include normalized W8 outcome, exact ordered gate argv, repository-relative cwd, ordered stack/assertion records, affected repository paths, full immutable commit, base-image digest, base recipe, cache manifest, and microVM guest-kernel/rootfs identities where applicable. Include a normalizer/policy version and extraction-policy identity. Record types distinguish absent values from empty values. Environment additions are [TARGET] refinements motivated by the existing separate runtime pins (`docs/design/W8_WORKER_BANK.md:156-158,167,235`).

Only typed volatile metadata is omitted: a separately identified PID, timestamp or address, and an authenticated workspace prefix on a structured source path. Preserve assertion bytes, embedded numbers or hashes, symbol names, line identity, argv order, frame order and meaningful path components. No global regex strips every number, address-like token or hash from raw stderr. Ambiguous input, non-integer numbers, duplicate JSON keys, invalid Unicode or invalid relative paths is refused. This narrows the aggressive stripping suggested in `docs/design/w8d/packs/D6.md:7` to avoid false merges.

The proposed identity is SHA-256 of a closed canonical JSON record whose required domain/version field is `normalizer: w8-repro/1`. Serialize UTF-8 with sorted keys, compact comma/colon separators, `ensure_ascii=false`, no trailing newline and no non-integer JSON numbers. Typed JSON structure and quoted strings make the field encoding injective: adjacent values cannot collide by concatenation, arrays retain order, and null differs from an empty string. This deliberately uses the existing canonical JSON convention instead of the alternative length-prefix representation proposed in `docs/design/w8d/packs/D6.md:7,14`; it does not change the immutable W8 task digest rule (`contracts/worker/os.cu.worker-task.v1.schema.json:5`).

Dedup requires equal strong digest **and equal canonical bytes**. FNV, Bloom filters, edit distance and histograms can select candidates only; they cannot merge identities, accept findings, remove billing rows or manufacture missing evidence. A byte-equal duplicate adds provenance to an existing group; a new normalized assertion, gate, commit or environment pin creates a separate group. Equal token counts with reordered frames remain distinct (`docs/design/w8d/packs/D6.md:36-37`; `docs/design/w8d/math/M6.md:49-57`).

Golden fixtures store literal canonical bytes and literal expected hashes rather than computing expected output with the implementation under test. They cover non-ASCII UTF-8, ambiguous adjacent field boundaries, typed volatile variation, assertion literals, ordered traces, environment changes and unknown normalizer versions. The companion tests are the normative executable encoding examples; production Go must reproduce their bytes and digests before joining the bus. Test-generated identities are not evidence of a real failure.

The exact normalized field set in the reference model is `normalizer, repo, commit, base_image_digest, base_recipe_sha256, cache_manifest_sha256, guest_kernel_sha256, guest_rootfs_sha256, extract_policy_sha256, gate_argv, gate_cwd, failure_kind, normalized_exit, affected, assertion, frames, repro_steps`. A normalized frame is `path, symbol, line`. The input-only fields `workspace_prefix, pid, time_ms` and each frame's typed `address` are omitted. Guest kernel/rootfs must be both present or both null. The complete non-ASCII repro golden has **761 [TARGET] bytes** with literal digest `4e4d3b0b2679041be017a73ffb043bec9674aecf368dc57392e3b03b5d316934`; its bytes are held inline, independently of the normalizer, in `test_repro_golden_vector`.

## Terminal receipts, acknowledgement and expiry

The unchanged `os.cu.worker-receipt/v1` is built by the trusted host. A successful observed local receipt requires extraction, gate/observation checks and verified purge; known local failures also preserve the schema's cleanup obligations. An indeterminate receipt instead records honest incomplete cleanup/quarantine and remains collectible without claiming successful teardown. The schema binds task, attempt, lease, task digest, seat, nonce and grant digest, with real runtime, evidence, cleanup and extraction records. Web-route observations/extraction remain null with not-applicable cleanup (`contracts/worker/os.cu.worker-task.v1.schema.json:1739-1878`; `docs/design/W8_WORKER_BANK.md:162,185,199-211`).

The proposed `receipt_id` commits a receipt domain tag, the full lease binding and the SHA-256 of exact terminal receipt bytes. It excludes pulse, native message ID, bus epoch, cursor and Lamport, so retransmission remains the same receipt. A different terminal body for the same settled attempt is a conflict for reconciliation, not a second settlement. `ack_id` commits a separate domain, receipt ID, complete binding, authenticated collector identity and disposition. An ACK from the wrong collector, epoch, target, attempt, body or surface cannot free another reservation.

| Disposition | What it authorizes |
|---|---|
| `READY` | A hint to fetch candidate output. No acceptance, credit release or billing settlement follows. |
| `COLLECTED` | Exact candidate bytes and receipt provenance are committed in trusted retained text storage; retransmit can stop under this receipt credit. This is not a verifier verdict. |
| `VALID` | The pinned existing consumer returned an authenticated acceptance decision for these bytes. Only this can ratify the appropriate result. |
| `REJECTED` | Preserve the exact consumer rejection class and evidence. The execution outcome remains independently recorded. |
| `INDETERMINATE` | Publish/collection outcome is unknown. Preserve identity, reservation and query state; do not re-execute the external action. |

The ACK model may combine collection and a known verdict into a single immutable disposition, but it must never reinterpret transport success as verification. The reference model calls the collection-only ACK stage `staged` and the ready hint `READY_ONLY`; its in-memory transition models a trusted durable-staging assertion, not an actual fsync or crash-recovery proof. Computing the public ACK hash is never authentication: the collector channel principal must also match. Resubmission queries or reuses the same receipt ID; it does not allocate a new paid attempt. Backend surface comes from the actual adapter, not copied worker text: the W8 codec requires `os-capsule` for live local output and refuses parity-only publication. The bound design says that canonical writer support is still absent (`docs/design/W8_WORKER_BANK.md:175-179,325`). This collection PR itself runs on `chatgpt-mcp`; it does not claim an OS execution surface.

### Credits, STOP and bounded progress

At task admission reserve a bounded terminal-receipt slot, complete text-byte allowance, collector work allowance and finalization funds. Ordinary finding/status traffic uses a separate bounded queue. A worker cannot consume the terminal reservation with chatter. Credit is a host reservation ID with a state transition, not an unsigned count supplied in a message; a repeated matching ACK is a no-op and cannot mint credit.

STOP has an independent reserved control lane. It is authenticated, destination-scoped and coalesced per target/epoch. Repeated STOP does not create an unbounded priority queue or starve terminal collection. Drain ordinary/terminal origins round-robin with bounded per-origin work; reserve a control service step without allowing endless control replays to monopolize it. Test progress while one origin floods and another has a pending receipt. All queues, pending receipts, body refs, dedup entries and ACK histories count against the reservation (`docs/design/w8d/synth/S3_FOLDIN.md:11`; `docs/design/w8d/math/M7.md:35-43`).

One absolute lease expiry covers execution, extraction, teardown and normal terminal production. Begin draining before expiry using reserved headroom. Extract approved text into host-controlled retained storage, then purge and prove cleanup, then finalize the terminal receipt. Capsule purge never waits on remote receipt acceptance. A missing consumer or late ACK cannot extend the capsule lifetime. In the reference model, `expires_ms` is this hard execution/teardown deadline: fresh sends are fenced and STOP becomes due at equality. The later `collect_until_ms` bounds only host-side retention/query/ACK reconciliation of already extracted text; it authorizes no capsule operation. Normal finalization must fit the hard expiry; unproven teardown or an unresolved external outcome is indeterminate, and retains liability. Retained host text can survive as bounded quarantine metadata until its collection deadline. If the host loses non-durable evidence, declare a gap rather than reconstructing it from a summary (`docs/design/W8_WORKER_BANK.md:185,202-211`; `docs/design/w8d/math/M9.md:37-45`).

## Stable requests, paid attempts and budget stop

The host journals identities before the corresponding effect. Request identity is stable across intentional retries; attempt identity is unique for each separately authorized paid execution; receipt identity is stable across transport retries of one terminal result. Neither repro hash nor native message ID is a charging key. The full W8 task digest includes attempt and lease fields, so it may legitimately change on retry (`contracts/worker/os.cu.worker-task.v1.schema.json:496-579`). Keep a distinct `logical_request_sha256` over the versioned retry-invariant work projection: schema, task ID, repo/commit, objective, sources, write paths, exact gate, evidence policy, consumer, host metric and optional policy. Attempt/lease and bridge dispatch mirrors are excluded only after ordinary task/bridge validation; they still bind each full attempt. Absence of optional policy is explicit. Changing work cannot reuse the same logical request record.

| Identity / event | Rule |
|---|---|
| `request_id` | A host-issued persistent ID bound to wave, task ID, `logical_request_sha256`, immutable source commit and requested operation. Intentional retries retain it; another logical work item gets a distinct ID. The same request cannot silently change its work projection. |
| `attempt_key` | Binds request, distinct attempt ID, lease tuple and admitted reservation. A paid retry gets a new attempt key even when repro and result match. |
| Provider invocation key | Bound to that paid attempt and provider operation. Query an uncertain invocation using this key; do not issue an unreserved replacement. |
| Debit / settlement | Unique per admitted paid attempt. Identical redelivery is a no-op; a different amount or request under the same key is a conflict. |
| `receipt_id` | Terminal result identity described above. Repeated receipt delivery cannot debit the attempt again. |
| `verified_coverage` | Consumer-authenticated coverage key scoped to task/commit and coverage definition; count once. It is distinct from crash/repro identity. |
| `verified_repro` | A ratified canonical repro plus provenance. Duplicate worker evidence adds attribution, not another unique-find count. |
| `budget_stop` | Fences new inference, retries and paid dispatch. STOP, bounded collection, cancellation reconciliation and cleanup may use only funds already reserved for finalization. |

Money and resource units are integers with explicit units and checked arithmetic. Admission requires current spend plus outstanding upper bounds plus reserved finalization to fit the approved cap. Uncertain cancellation retains the provider liability until authoritative resolution; no optimistic refund. A replacement succeeding does not erase the previous attempt's failure or paid cost. Yield reports both verified novelty and all incurred/pending cost; high message count is not yield. The shared request/retry question is explicit in `docs/design/w8d/synth/S3_FOLDIN.md:46`, and the router separately requires budget-aware wave control (`docs/design/w8d/router.json:156-171`). All provider liability bounds remain deployment evidence to obtain, not assumed refund semantics.

## Replay-safe bus cursor, memory delivery and revocation

Keep the native Lamport clock and content-addressed message ID, but do not use either as a global exactly-once execution promise. The authoritative memory stream has a single fenced collector origin for its wave. Its cursor is `(bus_id, bus_epoch, origin_id, cursor, event_ref)`, where `bus_id` is the wave ID and `cursor` is that collector stream's contiguous sequence. Original workers retain their W8 `seq` and lease-scoped ingress replay checks. The model runs one collector origin per replay-consumer instance; it does not claim an atomic multi-origin vector transaction. `binding.epoch` fences target execution; `bus_epoch` identifies an authorized log generation; neither may be substituted for the other.

The exact model record is `schema, bus_id, bus_epoch, origin_id, cursor, previous_digest, lease_digest, kind, target, source_body_ref, source_body_bytes, bounded_digest, digest_bytes, dropped_events, overflow, repro_sha256, verdict_generation`. The separately verified event identity hashes the complete record. The fixture bounds the encoded record at **2048 [TARGET] bytes**, and `digest_bytes` describes **64 [TARGET] ASCII bytes** of the hexadecimal digest. A trusted ratification input models the separately authenticated verifier verdict/policy boundary; a boolean copied from untrusted JSON is not ratification. The record is serialized before admission. An oversized record is refused whole; if it cannot be delivered, retain an explicit overflow/gap and source identity rather than claiming complete evidence (`docs/design/w8d/math/M7.md:35-43`).

Within an origin, accept the next sequence only when its predecessor agrees with the committed checkpoint. Identical replay is a no-op. Same position with different identity is tampering; an ahead sequence is a gap, not progress. ACK only the contiguous prefix, never across a missing event. A bounded holdback may request missing bodies or an authenticated full checkpoint, but cannot synthesize them. Torn native records and conflicts require reconciliation before further authoritative appends (`docs/design/w8d/packs/D4.md:34-38`; `docs/design/w8d/packs/D6.md:9,39`; `docs/design/w8d/math/M9.md:37-45`).

A larger bus epoch is accepted only with a trusted checkpoint/rotation authorization covering the old complete prefix and new generation. Merely presenting a larger integer is insufficient. Historical events remain auditable under their old generation; they cannot actuate a current lease. Restart restores cursor, receipts, charging keys, credit transitions and revocations atomically from the host journal; an unknown external effect is queried, never blindly redone. Production reuse should reconcile with the existing lease/WAL components reported by the packs, not invent a parallel authority (`docs/design/w8d/packs/D4.md:88-92`; `docs/design/w8d/packs/D6.md:28-29`).

Memory receives dynamic information only as appended Z3 events. Pinned Z1/Z2 bytes and their hashes remain unchanged; no live prompt prefix is rewritten to insert a newer room digest (`docs/design/w8d/router.json:111-127,312`). The memory consumer fetches and verifies complete source bodies and requires a current authenticated `VALID`/ratified verdict, canonical repro identity and verifier policy reference. Room consensus or a high native score alone cannot ratify a finding.

Revocation is an authenticated, append-only tombstone naming the canonical finding, affected verdict generation, source identity and revoker authority. Never delete or rewrite the earlier event. A tombstone defeats stale adds and repeated old `VALID` events, including when delivered first; reinstatement requires a separately authenticated newer verdict generation. A bare grow-only finding set cannot express this rule (`docs/design/w8d/packs/D6.md:40`). Historical coverage observations and every billing event remain auditable; the derived current finding view excludes revoked findings, and currently verified coverage excludes any item whose only supporting proof was revoked. An independently valid coverage proof may retain that item's novelty. Historical observed yield and currently verified yield are separate counters.

## Receipt aggregator and the existing verifier boundary

The aggregator authenticates origin and grant, fetches full text, verifies hash/length/manifest/schema and cross-document bindings, resolves replay, journals the immutable candidate, then passes the unchanged task/receipt/result bytes to the existing verifier through a pinned adapter. It may derive a finding graph and provenance index, but it does not change bridge fields or replace the verifier. The schema alone cannot prove a gate ran or authenticate a host (`contracts/worker/os.cu.worker-task.v1.schema.json:5`; `docs/design/w8d/packs/D6.md:44`).

**The real `auto_ingest` implementation, deployed version, executable entry point and live `os-capsule` writer authorization are ABSENT in the bound evidence.** The consumer schema names only `auto_ingest`; the base design explicitly records the missing implementation and canonical surface addition. Parser compatibility is an open parity gate (`contracts/worker/os.cu.worker-task.v1.schema.json:543-553`; `docs/design/W8_WORKER_BANK.md:52,175-179,286,325`; `docs/design/w8d/packs/D6.md:32`; `docs/design/w8d/router.json:313`).

Before live use, an operator-supplied trusted capability record must pin consumer repository and full commit, executable/content digest, exact argv or module/function entry point, contract version, native codec/checker digest, allowed writer surfaces, and the digest of a real paired parity report. It must also identify the authenticated query/idempotency contract for uncertain publication. Empty values, a worker-authored manifest, unsupported surface or absent parity evidence fail `unavailable`. The reference fixture uses clearly synthetic pins and a fake verifier; it cannot certify these absent deployed values.

The required real parity campaign supplies good and must-FAIL task/receipt pairs to both routes and compares exact verdict and rejection classes, with exact-byte round-trip evidence. Include stale tuple, nonce/grant mismatch, wrong commit, altered argv, forged observations, missing purge/extract, wrong surface, preview-only body, replay conflict, disallowed artifact and indeterminate publish. Preserve every observed raw consumer outcome. Unknown or interrupted publication is `INDETERMINATE`, not `VALID` or a reason to retry a paid task. A ready signal triggers collection only.

## Outcome codes and implementation boundary

W8 process exits are **0 and 2..7 [CITED]**, never **1 [CITED]**. Preserve raw native/provider exit, signal and rejection evidence separately. Interpret outcome using context, not an arithmetic mapping: success only after required checks; invalid schema/shape is invalid input; known setup/execution failure is distinct from prerequisite unavailable; verification/containment failure remains failure; uncertainty quarantines; operator action remains explicit (`AGENTS.md:16`; `docs/design/W8_WORKER_BANK.md:213-225`).

The Python suite is a reference contract with inline synthetic valid and must-FAIL fixtures. It launches no capsule, model, browser, VMM or network client and writes no frames. Direct execution maps a test failure to an allowed non-success exit. The orchestrator's requested unittest command remains its own collection gate; its standard test-runner behavior is not a W8 runtime outcome. Production integration still needs real native checker, authentication, crash/WAL, cgroup, teardown, consumer and cost-provider validation.

## S3 answers

The subsections below answer every C3 or shared C2/C3 question in the bound synthesis. All named tests are in `contracts/worker/tests/test_w8d_c3_switchboard.py`; they pin this proposed interface with inline fixtures, including refusals, rather than certify production integrations.

### Receipt credit, back-pressure, independent STOP and fairness

**Question:** `docs/design/w8d/synth/S3_FOLDIN.md:11`.

**Decision:** Reserve terminal capacity at admission, separate it from ordinary traffic and keep an authenticated coalesced STOP lane. Return credit only once for the exact reservation/receipt/collector binding. Round-robin bounded drains must advance another origin even while one floods; control replay cannot monopolize draining.

**Test:** `test_terminal_credit_stop_lane_fair_drain_and_bound_ack`, `test_public_ack_hash_does_not_authenticate_collector_or_mint_credit`, and `test_repeated_stop_cannot_starve_other_terminal_or_extend_execution` pin reserved terminal capacity, independent coalesced STOP, fair draining, authenticated ACK and no credit inflation. `test_first_excess_send_throttles_at_same_tick_before_append` pins ordinary admission separately.

**Cites:** `docs/design/w8d/math/M7.md:35-43`; `docs/design/w8d/packs/D6.md:39`; `docs/design/w8d/packs/D4.md:17,57`.

### Collection, terminal acknowledgement and teardown under the same expiry

**Question:** `docs/design/w8d/synth/S3_FOLDIN.md:16`.

**Decision:** Reserve the entire collect/purge/finalize path. Extract complete approved text, purge before final terminal receipt, and retain bounded host-side bytes while awaiting collection. ACK is not permission to extend the capsule lifetime. At expiry fence fresh work; unresolved cleanup or external publication becomes indeterminate and is queried by stable identity.

**Test:** `test_collection_deadline_keeps_stop_and_quarantines_without_release`, `test_repeated_stop_cannot_starve_other_terminal_or_extend_execution`, `test_historical_retained_extract_collection_cannot_revive_worker`, and `test_indeterminate_verifier_is_queried_never_reexecuted` pin hard execution expiry, bounded retained-text reconciliation, historical collection-only authority and no uncertain re-execution. `test_inline_receipt_matches_actual_unchanged_schema` pins the inherited cleanup obligations.

**Cites:** `docs/design/W8_WORKER_BANK.md:185,199-211`; `docs/design/w8d/math/M9.md:37-45`; `docs/design/w8d/synth/S3_FOLDIN.md:16`.

### Lease-bound terminal receipt and ACK identities with real surface

**Question:** `docs/design/w8d/synth/S3_FOLDIN.md:22`.

**Decision:** Receipt identity commits exact terminal bytes and the complete lease sidecar. ACK additionally binds authenticated collector and disposition. Transport positions are excluded. Resolve surface from the actual backend; parity envelopes cannot publish, and an OS receipt cannot masquerade as a web worker.

**Test:** `test_terminal_and_ack_bind_every_lease_field`, `test_aggregator_never_relabels_surface_or_merges_terminal_mutation`, and `test_public_ack_hash_does_not_authenticate_collector_or_mint_credit` mutate the binding/body/collector and reject wrong-surface or contradictory settlement. `test_ready_valid_rejected_indeterminate_are_distinct` pins stable redelivery and dispositions.

**Cites:** `contracts/worker/os.cu.worker-task.v1.schema.json:1739-1878`; `docs/design/W8_WORKER_BANK.md:175-185`; `docs/design/w8d/packs/D6.md:44`.

### Native full-payload fields, sender/score authentication and first excess send

**Question:** `docs/design/w8d/synth/S3_FOLDIN.md:27`.

**Decision:** Map the unchanged W8 acts into native acts and resolve complete `body_ref` bytes. The sidecar carries full-byte length, grant and evidence bindings. Host identity and independent verifier provenance authorize extension and score. Admission rejects the first excess send atomically before append at the same fake-clock tick; watcher polling is additional supervision.

**Test:** `test_full_noncanonical_payload_survives_native_mapping`, `test_unicode_payload_bound_counts_characters_and_retains_bytes`, `test_preview_truncation_and_malformed_references_fail`, `test_sender_is_channel_authenticated_and_fenced`, `test_evidence_score_is_attested_to_complete_bytes_and_lease`, and `test_first_excess_send_throttles_at_same_tick_before_append` pin full bytes, authenticated identities/evidence and immediate fake-clock admission refusal.

**Cites:** `docs/design/w8d/packs/D4.md:23-46,50-57`; `contracts/worker/os.cu.worker-task.v1.schema.json:2995-3147`; `docs/design/w8d/research/R6.md:63-83`.

### Golden canonicalization, exact consumer identity, writer authorization and dispositions

**Question:** `docs/design/w8d/synth/S3_FOLDIN.md:34`.

**Decision:** Freeze versioned canonical bytes and independently generated literal digest vectors, with conservative typed normalization. Require actual consumer executable/version/entry point and writer-surface authority in a trusted capability record. Their deployed values are ABSENT, so live publishing remains unavailable. Keep READY, COLLECTED, VALID, REJECTED and INDETERMINATE distinct; a synthetic fake proves only this interface.

**Test:** `test_canonical_golden_vectors`, `test_repro_golden_vector`, `test_repro_typed_volatility_normalizes`, `test_repro_semantic_mutants_never_merge`, `test_missing_real_consumer_pin_and_writer_authorization_fail_closed`, and `test_ready_valid_rejected_indeterminate_are_distinct` pin literal identities and fake-consumer boundaries. `test_inline_receipt_matches_actual_unchanged_schema` rejects missing/extra receipt fields. The unchanged W8 suite separately pins parity-only outbox refusal; real consumer parity remains ABSENT.

**Cites:** `docs/design/w8d/packs/D6.md:7,32,36-44`; `docs/design/w8d/math/M6.md:49-57`; `docs/design/W8_WORKER_BANK.md:52,175-179,286,325`; `docs/design/w8d/router.json:313`.

### Replay-safe Z3 delivery, bounded digests, finding revocation and memory verdict

**Question:** `docs/design/w8d/synth/S3_FOLDIN.md:41`.

**Decision:** Use a per-origin contiguous cursor, separately authorized bus epoch, complete source reference/length, predecessor/event identities and explicit gap/overflow counters. An authenticated checkpoint authorizes rotation. Append only to Z3 and require current ratification plus canonical repro identity. Revoke using authenticated tombstones; stale verdicts cannot resurrect findings or rewrite Z1/Z2.

**Test:** `test_replay_redelivery_gap_conflict_and_full_body`, `test_bus_epoch_needs_authenticated_checkpoint_and_not_lease_epoch`, `test_bounded_replay_digest_missing_intervals_and_snapshot_restart`, `test_revoke_appends_tombstone_without_rewriting_live_prefix`, and `test_tombstone_before_add_and_newer_verdict_cannot_be_downgraded` pin contiguous replay, authorized checkpoints, bounded records, append-only history and monotone verdict generations. The model preserves the already-issued prefix; actual prompt integration remains a production gate.

**Cites:** `docs/design/w8d/math/M7.md:35-43`; `docs/design/w8d/math/M9.md:37-45`; `docs/design/w8d/packs/D6.md:39-40`; `docs/design/w8d/router.json:111-127,312`.

### Stable request/attempt/receipt identities, verified yield and reserved finalization

**Question:** `docs/design/w8d/synth/S3_FOLDIN.md:46`.

**Decision:** Keep logical requests, paid attempts and terminal receipts distinct. Journal every paid reservation/charge separately even when findings dedup; retransmission cannot charge again. Yield uses verified coverage and canonical finding novelty with all costs included. Budget-stop disallows new paid work and allows only previously reserved cancellation/collection/cleanup.

**Test:** `test_paid_retry_distinct_from_retransmission_and_repro_dedup`, `test_attempt_cost_and_uncertain_provider_conflicts_fail`, `test_request_projection_and_attempt_binding_prevent_cost_aliasing`, `test_coverage_is_separate_from_repro_and_last_proof_revocation`, and `test_budget_stop_only_spends_existing_finalization_reserve` pin stable logical requests, separately paid attempts, full-binding authorization, verified yield and reserved-only finalization.

**Cites:** `docs/design/w8d/synth/S3_FOLDIN.md:33,46`; `docs/design/w8d/research/R5.md:1-11`; `docs/design/w8d/router.json:156-171`; `docs/design/w8d/packs/D6.md:9,44`.
