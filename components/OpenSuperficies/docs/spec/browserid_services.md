# Browser service assignments

`src/browserid/services` is a stdlib-only Go package. It stores service-name
assignments and access requests, without domains, browser launches, network
policy, credentials, leases, recordings, or a disk-save API.

## Catalog and authority

The egress owner supplies `Catalog` with `HasService(name string) bool` and
`Services() []string`. The catalog must support concurrent reads and present a
consistent snapshot. A service removed from the catalog is immediately hidden
from queries, and cannot be assigned, requested, or approved. Stored historical
assignments remain available for operator removal. This package never approves
a service in the catalog.

`Open(dir, catalog)` returns an AI-capable registry without an operator token.
`OpenOperator(dir, catalog, credential)` first calls the injected
`OperatorCredential.AuthorizeServices() error`; refusal returns no registry or
token. Only the trusted embedding operator host supplies that credential.
There is deliberately no built-in allow credential, string secret, environment
credential, HTTP endpoint, Wails binding, or CLI operator-write command.

`OperatorToken` is an interface containing an unexported method. Its concrete
implementation and construction are private. Tokens are tied to the exact
registry instance. Nil, typed-nil, and tokens from other instances are refused.
`Assign`, `Unassign`, `Approve`, and `Deny` require this token. The host must not
expose `OpenOperator`, its credential implementation, or its returned token to
an AI binding. The capability separates trusted Go callers; it is not an OS
security boundary against arbitrary code that can edit the same private files.

## Persistence

The caller supplies a trusted private directory; its mode is `0700` [CITED:
lane contract]. State is `assignments.json`, schema
`browserid.assignments/v1`, with `revision`, `assignments`, `requests`, and
`journal`. Files and `.lock` use mode `0600` [CITED: lane contract]. Compact
`encoding/json` serialization plus a newline is canonical: object keys sort,
assignment arrays sort and contain no duplicates. Unknown fields, noncanonical
bytes, invalid identifiers, invalid revisions, inconsistent request IDs and
corrupt state fail closed. Final-component file symlinks and nonregular files
are refused. Ancestor directories must remain under the caller's control.

An exclusive flock plus an instance mutex covers load, mutation, and persistence,
so independent handles and processes do not overwrite each other's updates.
Writes use an adjacent temporary file, file sync, atomic rename, and directory
sync. State is capped at `16 MiB` [TARGET]; a mutation exceeding that limit is
refused before persistence, preserving the last valid state. Every successful mutation increments the global revision; repeated
operator assign/unassign calls are audited mutations even if membership already
matches. A deduplicated pending request does not increment the revision.

Every operator mutation adds exactly one [CITED: lane contract] journal entry
with UTC time, actor `operator`, action, capsule, service, and resulting revision.
The authoritative journal resides inside the same atomic state transaction;
`journal.jsonl` is its recoverable projection. Refreshing this projection replaces
its file atomically while preserving the preceding lines and adding the new
entry. After a crash, a missing projection or an exact earlier complete prefix
is rebuilt from state. Arbitrary projection edits fail closed. AI requests do
not create operator journal entries. An I/O error after a successful state
rename can mean the mutation committed; callers must query before retrying.

## Queries and requests

Capsules match `capsule-[0-9]{1,3}` or `srf-ng-` followed by an ASCII alphanumeric
character and ASCII alphanumerics, `_`, `.`, or `-`. The container suffix is
bounded to `120` characters [TARGET]. Service names match
`[a-z][a-z0-9_-]{1,31}`; this implements the requested lowercase service labels
of `2–32` characters [CITED: lane contract]. AI IDs are ASCII alphanumeric
initially, then alphanumeric, `_`, `.`, or `-`, bounded to `128` characters
[TARGET]. Reasons accept up to `280` printable Unicode characters [CITED: lane
contract]; invalid UTF-8, newlines, tabs, and other nonprintable characters fail.
An empty reason is allowed because the contract specifies only an upper bound.

`WhereCanOpen(service)` returns `service`, sorted `available_in`, `everywhere`,
and `approved_in_catalog`. Unknown but syntactically valid services return an
unapproved empty result. `everywhere` means assigned to every capsule known to
this registry, with a nonempty known-capsule set. It does not claim to discover
all live containers. Unassigning the last service retains that known capsule
with an empty service list. `Status(capsule)` returns sorted currently approved
services and the current revision; unknown valid capsules return an empty list.
Queries never grant access or create assignments.

`RequestAccess(ai_id, capsule, service, reason)` creates a pending request.
Its full SHA-256 ID binds the JSON tuple of its fields and creation revision.
Identical pending tuples deduplicate. Each AI may have at most `20` pending
requests [CITED: lane contract]. `ListPending` sorts by request ID. `Approve`
atomically assigns the service and changes the request to `approved`; `Deny`
changes it to `denied` without changing assignment membership. Resolved requests
cannot be decided again. Approval rechecks catalog membership. A resolved tuple
can be requested again and receives a new revision-bound ID.

## CLI

```
srf-services -dir PRIVATE_DIR -catalog APPROVED_SERVICE_NAMES_JSON where chatgpt
srf-services -dir PRIVATE_DIR -catalog APPROVED_SERVICE_NAMES_JSON status capsule-1
srf-services -dir PRIVATE_DIR -catalog APPROVED_SERVICE_NAMES_JSON request ai-a capsule-1 chatgpt 'research'
srf-services -dir PRIVATE_DIR -catalog APPROVED_SERVICE_NAMES_JSON pending
```

The operator-maintained catalog adapter accepts a JSON array of unique service
names, bounded to `1 MiB` [TARGET]. It contains no domain bundles. Both paths are explicit; missing or invalid
catalog input fails closed. The CLI imports only `Open`, never `OpenOperator`.
Its commands expose reads and pending-request submission; no command grants or
removes access. Results and errors are a single JSON line.

| Exit | Meaning |
|---|---|
| `0` [TARGET] | Success |
| `2` [TARGET] | Usage or invalid fields |
| `3` [TARGET] | Operator capability refused |
| `4` [TARGET] | Service absent from approved catalog |
| `5` [TARGET] | Storage, corrupt data, catalog input, or output failure |
| `6` [TARGET] | Pending limit or resolved/missing request |

The CLI never emits exit `1` [CITED: lane contract]. Operator refusal and request
decision errors are mapped defensively but no CLI operator verbs exist.
The mandate is limited to the supplied service-assignment corpus and interfaces.
