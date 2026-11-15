# Offline KVM capture qualification

This package is a pure Go parser/evaluator. It does not read /dev/kvm, inspect host state, open processes, invoke a hypervisor, boot a guest, or collect captures. All observations and the current time are supplied by the caller.

## API and policy

Call Assess(Policy, Captures, time.Time), then Record.JSON(). The trusted caller supplies the approved /dev/kvm path; expected node major/minor and OwnerUID (an explicit pointer, allowing root ownership with value 0); the dedicated user, group, non-root UID/GID, mapped PID, SHA-256 digests for the VMM, kernel, rootfs and qualified probe; a scope, acquisition ID and provenance; a validity interval and maximum evidence age. Policy UID and GID must be nonzero. Each caller controls its own acquisition provenance; the package does not authenticate attestations.

Captures has four passive documents, Node, Account, Process and Behavior. Each is UTF-8 with a single KEY=VALUE field per line, no duplicates or unknown keys, no empty values, a 4,096-byte TARGET limit per document, and a 256-byte TARGET limit per line. All values are ASCII alphanumerics or underscore, dot, slash, colon or hyphen. All digests are 64 lowercase hexadecimal bytes, times are canonical decimal Unix seconds, and mode is four octal characters with a leading zero.

Every document requires: kind, scope, acquisition, provenance, captured_at. All four must agree with the policy and with one another, including the capture time.

- Node (kind=node): device, type=char, owner_uid, owner_gid, mode, major, minor, device_count.
- Account (kind=account): user, uid, group, gid, dedicated=true.
- Process (kind=process): pid, user, uid, group, gid, mapped=true, privileged=false, vmm_sha256, kernel_sha256, rootfs_sha256.
- Behavior (kind=behavior): pid, uid, gid, device, vmm_sha256, kernel_sha256, rootfs_sha256, probe_sha256, probe_qualified=true, privileged=false, emulation=false, device_count, open, ioctl, cold_boot.

The behavioral result fields accept exactly ok, fail or unavailable. Only explicit ok for all three can qualify. The node must have the expected major/minor, one device, no world permissions or special bits, and owner/group read/write permissions. The process must be the dedicated mapped non-root identity and have pinned matching digests. The behavior must come from the same scope, PID and identity, with exactly one approved device and no software fallback.

## Results

A fully matched lifecycle produces decision=QUALIFIED, inventory=PASS, behavior=PASS and reason=CAPTURED_SCOPE_ONLY. A missing policy or captured field returns decision=HOLD with an AUSENTE reason; a mismatch, stale capture or failed behavior returns HOLD. Malformed, duplicate or contradictory input produces an error and a zero Record so no qualified record survives. Records are deterministic integer-safe JSON/text, with a source_sha256 over length-framed policy and four exact captured strings. The hash is for traceability, not a digital signature.

The caller-provided current time makes tests deterministic, and no clock, filesystem, network, process or device access occurs in the library. These are offline fixtures, not measured KVM availability. A trusted external collector must independently validate acquisition provenance and that a qualified probe genuinely performed open, ioctl and cold boot. No CLI, script or live probe is added.
