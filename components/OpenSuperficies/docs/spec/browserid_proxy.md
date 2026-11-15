# Identity browser egress proxy

`src/browserid/proxy` implements HTTP absolute-URI forwarding and CONNECT;
`cmd/srf-egress-proxy` supplies a bounded HTTP server, signal shutdown, file
registry, and an explicitly configured file-policy adapter. No container,
network, browser, X server, credentials, or profile is opened by tests.

## Policy and registry contracts

```go
type Policy interface {
    Decide(capsule, host string) Verdict // approved | pending | denied
    Account(capsule, host string, bytes int64) error
}
type PortPolicy interface {
    AllowPort(capsule, host string, port int) bool
}
```

`Config` requires Policy, Registry, Audit, Clock, Resolve and Dial; omitted dependencies
are errors. `Resolve`, `Dial` and `Clock` are injectable. `Account` receives actual
bidirectional wire-byte counts, never payloads. Its failure terminates the
transfer. Nonstandard CONNECT ports require explicit `PortPolicy` approval,
including approved hosts; hostname approval alone does not permit them. Plain
HTTP ports other than 80 [TARGET] use the same `AllowPort` exception and default
to refusal (`http_port_denied`). The file field remains `connect_ports` for
compatibility and supplies exceptions for both protocols.

`src/browserid/egress` integration is AUSENTE — no inventar in the inspected
working tree. Its owner can implement this interface without changing the
proxy. The CLI's temporary `FilePolicy` is an explicit operator policy input,
not a service-catalog replacement. Denied overrides approved; other names are
pending. File-policy accounting is handled by the proxy's runtime quotas.
Do not expose this policy file for agent writes. Example:

```json
{
  "schema": "browserid.proxy-policy/v1",
  "approved": {"identity-demo": ["example.com"]},
  "denied": ["blocked.example"],
  "connect_ports": {"identity-demo": {"example.com": [8443]}},
  "private_targets": {}
}
```

The registry is a JSON object, exactly the CLI bus contract:

```json
{"identity-demo":"172.30.80.10"}
```

The CLI must atomically replace the registry after assigning/reserving the
capsule IP, and remove an entry after verified removal. Duplicate IPs, unknown
source IPs, malformed files and unavailable policy fail closed. Neither HTTP
headers nor forwarded-for values establish capsule identity. Source-IP identity
assumes operator-owned Docker networks and no malicious peer with raw-packet
capabilities; capsules must drop all capabilities. Metadata files reload per
request. Mount the containing directory, so atomic renames remain visible.

## Enforcement and audit

Pending hosts are allowed and audited. Limits are twenty new hosts per capsule
per rolling hour and fifty MiB bidirectionally per host per rolling hour [CITED:
operator ruling in lane task]. Only the offending host/request is refused;
other hosts and capsules continue. Existing admitted hosts remain usable when
the new-host allowance is exhausted. Names are exact, lowercased, trailing-dot
stripped, with Unicode converted to Punycode A-labels. Unicode acceptance is a
strict IDNA2008 subset (selected Latin, Greek, Cyrillic, Japanese, CJK and Hangul
letters): combining, RTL, compatibility-width and unsupported
labels fail closed; complete UTS46/NFC mapping is AUSENTE — no inventar.
Canonical A-labels are validated by decode/re-encode. IP literals need explicit
approved status. Userinfo is refused. IPv6 literals are normalized by net.ParseIP.

Before either protocol dials, the proxy resolves the normalized hostname once
with its injected resolver, under a bounded context. All returned addresses are
vetted; any forbidden address rejects the entire answer set with HTTP 403
[TARGET], audit verdict `refused-private`, and fixed reason
`private_destination`. Loopback, link-local, RFC1918, CGNAT 100.64/10,
IPv6 unique-local fc00::/7, unspecified and multicast addresses are forbidden
[TARGET: destination policy]. This includes IPv4-mapped IPv6, the reserved
srf-idnet 172.30.80.0/24 and Docker's RFC1918 bridge ranges [TARGET]. Empty,
invalid or failed DNS answers fail closed, without exposing resolver errors.
Quota admission precedes hostname resolution, so quota-refused names cannot
trigger DNS lookups. Pending names that pass admission consume a new-host slot
even if DNS later fails or resolves to a forbidden destination.
The dialer receives only the selected vetted IP and port; HTTP authority stays
the normalized hostname. There is no second name resolution, fallback dial or
redirect following.

A private destination requires BOTH exact-host `Approved` status AND an
operator-owned `PrivateTargetPolicy.AllowPrivate(capsule, host)` exception.
`FilePolicy` implements it using optional `private_targets`, a capsule-to-host
list with exact normalized names. Omitted/empty/malformed permissions default
to refusal. No suffix or wildcard matching. For example, the operator can add
`"private_targets":{"identity-demo":["internal.example"]}` only alongside
an explicit approved entry for that same host. Never grant agents write access
to this file. Approved private literals also require this separate exception.


Atomic shared reservations prevent simultaneous transfers bypassing byte
limits. Byte exhaustion and new-host admission share a mutex; an already
exhausted host never consumes a new-host slot. Unused reservations are refunded. Byte events are coalesced within a
second and expire from the latest reservation in that bucket [TARGET: at most
one second conservative retention]. In-flight reads can temporarily reserve
remaining budget; competing requests may fail early. HTTP metering includes
request/response lines, headers and bodies; CONNECT metering counts tunnel
bytes in both directions. Headers and bodies are never recorded. No TLS
interception is performed.

Pre-exhausted hosts receive HTTP 403 with `pending_host_limit` or
`pending_byte_limit`; denied hosts receive `policy_denied`. Crossing a byte
limit during an established stream closes that stream (a CONNECT success
cannot subsequently be replaced by an HTTP error). Unknown capsules and invalid
targets are refused. Audit failure before dialing refuses the request; audit
failure at completion closes all proxy connections to stop unaudited operation.

The audit is append-only JSONL, with an opening record and completion record
for a permitted attempt; only the completion carries transferred bytes.
Verdicts include `approved`, `pending`, `denied` and `refused-private`.
Fields: `capsule`, normalized `host`, `port`, `verdict`, `bytes`, optional fixed
`reason`. It never includes URL paths, query strings, request/response headers,
cookies, bodies or raw error text. A refused valid target has one record.
Malformed targets/unknown clients are refused without logging untrusted target
text. Audit files require private permissions (0600 [TARGET]); symlinks and
nonregular files are refused. Use RAM or an operator-provided state directory.
This audit contains metadata only, never frames.

The runtime bounds active requests, HTTP header sizes, dial and request
lifetimes. The global concurrent request cap is 256 [TARGET]. CONNECT also has
a per-capsule cap of 32 [TARGET] (`Config.MaxCapsuleTunnels`, zero selects the
default; negative is invalid), including tunnels still dialing or hijacking.
Saturation returns `capsule_busy`; slots are released on every exit. CONNECT
has an activity timeout of one minute [TARGET] (`Config.TunnelIdleTimeout`,
zero selects the default; negative is invalid) and a lifetime cap of one hour
[TARGET]. Successful traffic in either direction refreshes both socket
deadlines, without extending the lifetime cap. Both legs close on idle expiry,
quota/error/shutdown, and the copy goroutine is joined. A socket returned by
Hijack is closed even if Hijack reports an error or shutdown rejects tracking. SIGTERM closes hijacked
tunnels before HTTP shutdown. Exit codes: 0 success, 2 usage, 3 invalid/missing
configuration, 4 listener unavailable, 5 runtime/shutdown failure [TARGET].

Counters are memory-only. Restart resets them; this is not a durable quota
service. Policy updates apply to subsequent requests, not already-open tunnels;
restart the proxy to revoke active tunnels. The service-catalog owner must
supply any stronger live-revocation/durable-accounting contract.

## Operator-only network plan — not executed

These shell commands are a concrete deployment plan [TARGET], not evidence of
a running deployment. The addresses/subnet are reserved examples [TARGET];
the operator must verify no collision before running them. Use only the
dedicated rootless osworker daemon described in W8. No host port is published.
The CLI requires `--listen` with a literal IP and port. Hostnames, omitted
hosts, and omitted addresses are refused. Wildcards `0.0.0.0` and `::` require
`--allow-any-listen`; the deployment below uses only `172.30.80.2:3128`
[TARGET]. There is no all-interfaces default.
The proxy joins the internal identity network and the external default bridge;
identity capsules join only the internal network. The proxy listens only on its
internal interface. Docker's embedded DNS can resolve names; application
internet traffic must traverse the forward proxy. Keep Chromium QUIC disabled.

Build only the Go executable for the operator runtime (no image build):

```bash
CGO_ENABLED=0 taskset -c 10-14 /usr/local/go/bin/go build -o build/room_PROXY/srf-egress-proxy ./cmd/srf-egress-proxy
```

The operator pre-creates private RAM metadata/audit directories, assigns them to
the container uid, writes the reviewed policy JSON and initial registry, then
runs the following against the dedicated daemon from the repository directory.
The dedicated daemon socket URI is AUSENTE — no inventar; the operator must set
`DOCKER_HOST` to the configured rootless daemon endpoint. Every command pins
that endpoint explicitly. The desktop image tag is inherited from the supplied Holdings model [CITED];
its immutable digest is AUSENTE — no inventar. Pin that digest before production.

```bash
: "${DOCKER_HOST:?dedicated rootless daemon endpoint required}"
docker --host "$DOCKER_HOST" network create --internal --subnet 172.30.80.0/24 srf-idnet
docker --host "$DOCKER_HOST" create --name srf-egress-proxy --network bridge --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges --read-only --memory 256m --pids-limit 256 --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m --mount type=bind,src="$PWD/build/room_PROXY/srf-egress-proxy",dst=/srf-egress-proxy,readonly --mount type=bind,src=/dev/shm/vqa-ramws/identity-proxy/config,dst=/config,readonly --mount type=bind,src=/dev/shm/vqa-ramws/identity-proxy/audit,dst=/audit --entrypoint /srf-egress-proxy opensuperficies-desktop-arm64:1.1.0 --listen 172.30.80.2:3128 --registry /config/registry.json --policy /config/policy.json --audit /audit/egress.jsonl
docker --host "$DOCKER_HOST" network connect --ip 172.30.80.2 srf-idnet srf-egress-proxy
docker --host "$DOCKER_HOST" start srf-egress-proxy
```

Identity launch's required network arguments [TARGET] are
`--network srf-idnet --ip 172.30.80.10` and
`--env SRF_EGRESS_PROXY=http://172.30.80.2:3128`, plus IMAGE/CLI isolation and
private-profile mounts. A future approved seat implementation must generate
them; no agent should bypass the protected seat allowlist. That allowlist's
current `network none` restriction blocks live identity launch (CLI bus).
Do not attach an identity capsule to `bridge` or publish a proxy listener.
Operator stop: `docker --host "$DOCKER_HOST" stop --time 10 srf-egress-proxy`; remove only after stop.
The existing dedicated-daemon kill switch remains authoritative.

## Offline verification

Tests use fake policies/resolvers/clocks and net.Pipe upstreams, never a TCP listener or
network connection. They cover verdicts, Unicode normalization, IP/userinfo
refusals, port approval, rolling quotas, host isolation, concurrent accounting,
CONNECT byte equality and shutdown, audit privacy/failure, registry/policy
validation, DNS rebinding and IP pinning, private exceptions, HTTP port policy,
per-capsule fairness, idle closure/deadline refresh, hijack failure cleanup,
byte-exhausted admission, and explicit listener validation. Run:

```bash
timeout 90 taskset -c 10-14 /usr/local/go/bin/go test -race -count=1 ./src/browserid/proxy ./cmd/srf-egress-proxy
```

Failing-first evidence is `build/room_PROXY/failing-first.log`: the security regressions failed
on the pre-change proxy and CLI (including absent resolver/cap contracts).
The existing shutdown-track path already closed rejected sockets; the added
shutdown-race regression preserves that behavior. Repository gates run on a
scratch copy under this lane's surface, to contain generated files.

This implementation and its claims are scoped to the supplied corpus and the
inspected working-tree files.
