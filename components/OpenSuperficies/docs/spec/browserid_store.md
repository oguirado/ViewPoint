# Browser identity store (os-f-bid-01)

## Public contract

Package `opensuperficies.local/srf/src/browserid/store` exposes `New(root)`, `Create(Identity)`, `Get(profileID)`, `RecordLogin(profileID)`, `Acquire(profileID, holder)`, `Release(Lease)`, and operator-only `Reclaim(profileID, expectedCounter)`.

Identity JSON includes exactly `profile_id`, `browser` (chromium or firefox), `account_label`, `allowed_domains`, `created_unix`, and nullable `last_login_unix`. IDs are 2–48 ASCII lowercase letters, digits, hyphens; the first is alphanumeric. Account labels are nonblank, trimmed, valid UTF-8 without controls, at most 128 bytes. Domain lists contain 1–16 strictly ascending, unique, lowercase ASCII registrable-domain-shaped names. A conservative in-package table handles common compound public suffixes. Creation with zero `created_unix` uses the store clock.

Storage beneath the caller-provided root is private: `metadata/<id>.json` (0600), `profiles/<id>/` (0700), `locks/<id>.json` (0600), `locks/<id>.guard` (0600), and `audit.jsonl` (0600). Store directories are 0700; metadata and lease state use same-directory temporary files with atomic rename. The profile payload is opaque: this package never opens, reads, or enumerates files beneath profile directories. Profile paths and lease tokens are excluded from audit records. The profile directory itself is only created, mode-set and checked; an existing orphan is refused rather than adopted.

A successful Acquire returns a lease with holder ID, monotonically increasing acquire counter, wall-clock timestamp and unpredictable token. Only the SHA-256 digest of the token is stored in the persisted lock record. Release requires matching profile, holder, counter and token; comparisons use constant-time digest comparison. Leases survive process restarts and never time out automatically. A second holder receives a typed BusyError disclosing only the current holder ID. Reclaim is an explicit operator decision requiring the observed counter; a newer counter cannot be reclaimed by a stale request. Linux flock serializes cross-process mutations to a single identity; the durable JSON lock state is not a filesystem flock.

Every successful logical mutation (create, login, acquire, release, reclaim) appends one JSON line containing `time` (Unix seconds), `action`, `profile_id`, `holder`, and `result`. Neither a browser credential nor a profile path is stored in receipts or audit. No CLI is introduced, so CLI exit-code conventions are not applicable.

## Refusals and tested limits

Validation rejects IDs containing traversal, separators, dots, upper case, or lengths outside 2–48. It rejects unknown browsers, empty or unsafe labels, empty/oversized/unsorted/duplicate domain sets, malformed or non-registrable-shaped domains, invalid timestamps, and unknown fields or malformed/corrupt metadata. Duplicate identity creation, orphaned profile directories, symlinked metadata, concurrent acquisition, wrong holder/token/counter releases, wrong-counter reclaim, and stale leases fail closed. Public-path Go tests named `TestBidStore*` cover positive creation/readback, private file modes, boundary IDs and 16 domains, audit redaction, concurrent acquisition, persistence/reclamation, and negative cases. A fake filesystem port rejects and counts any attempted open below the profile root.

## Constraints and limitations

Domain validation is intentionally conservative: it does not bundle the full, dynamically updated Public Suffix List. Uncommon valid compound suffixes may be rejected; special domains, private suffixes and future registry policy cannot be certified offline. Validate against an authoritative PSL in a future independently reviewed change before expanding support. The local implementation targets Linux local filesystems with working flock, O_NOFOLLOW, atomic rename and append semantics; network filesystems are not certified. Audit appends and metadata renames are separate operations, not a cross-file transaction: on disk/audit I/O failure the caller must quarantine and inspect the store before retrying. No browser process, mount policy, actual login, credential access, X11, network call or CLI is implemented or tested.

The requested acceptance command is an integration TARGET rather than an observed success until the bridge-gate CI confirms it.
