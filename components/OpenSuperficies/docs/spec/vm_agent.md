# VM guest agent

The agent implements the frozen W3 protocol in `src/vmsurface/proto/api.go`. The
source contract is campaign design §9–§10 [CITED]. It never invokes a shell and
runs commands as its current uid. Guest init supplies device paths and starts it
as the worker uid [CITED].

```
srf-guest-agent --port DEVICE [--view-port DEVICE]
```

The optional view device serves concurrent xpra connections to the guest-local
`/run/srfview/xpra.sock`. Device discovery and ownership belong to guest init.
Agent and view transports are independent. Reopening a live view stream id
closes the old connection, waits for its close frame and cleanup, then dials
the replacement. The old worker cannot close or delete the replacement.

## Wire

Every frame has `VMA1`, a byte type, a big-endian uint32 stream id, a big-endian
uint32 payload length, then exactly that payload. Maximum payload is one MiB
[CITED]. Types and JSON shapes are defined by the frozen Go API. OPEN carries
`argv`, `env`, `stdin_cap`, `timeout_ms`; EXIT carries `rc`. STDOUT and STDERR
carry raw bytes. The client retains only its bounded stderr tail.

Bad magic, unknown types, truncated frames and excessive lengths terminate the
transport after an EXIT with malformed code, when the header identifies a stream
and the peer remains writable. No resynchronization is attempted. Malformed OPEN
JSON and invalid per-stream messages terminate that stream. An unreadable or
unwritable transport cancels all its streams. Slow writes close the transport
after a bounded deadline.

## Command policy

Bare command aliases and their fixed absolute guest paths are accepted; arbitrary
paths are refused. An inherited PATH cannot choose executable files. The argv is
passed directly to `exec.CommandContext`, with no shell. Environment consists of
fixed PATH/HOME plus filtered DISPLAY, NO_AT_BRIDGE and SRF_LOGIN; other caller
variables are discarded. DISPLAY must be `:100`; boolean flags must be `0` or `1`
[CITED].

Bounds follow the fix-room timeout table and design §10.1 [CITED]. Each shim
reserves one second beyond the policy, strictly inside the listed surface bound.

| Entry | Policy ms [CITED] | Shim deadline ms [CITED] | Surface bound ms [CITED] | Maximum stdin bytes [CITED] |
|---|---:|---:|---:|---:|
| xdotool operations | 3000 | 4000 | 5000 | 0 |
| xdotool type via stdin | 28000 | 29000 | 30000 | 8000 |
| srf-fbread pair | 3000 | 4000 | 5000 | 0 |
| pastePython | 10000 | 11000 | 12000 | 16384 |
| axPython | 3000 | 4000 | 5000 (room table) | 32769 |
| downloadsPython | 18000 | 19000 | 20000 | 0 |
| recordPython | until cancellation | until cancellation | unbounded | stream (-1) |
| xpra-start | 110000 | 111000 | 120000 | 0 |
| xpra-stop | 13000 | 14000 | 15000 | 0 |

The actual AX direct builder has no `timeout` argv; `Tool.AX` encloses it in a
context deadline of 10000 ms [CITED: ax.go]. Tests verify both the room-table
inequality and this actual enclosing context, without changing surface code.
Typed text passes unchanged from `io.go` through stdin. The validator in
`browserid/inject` limits text to 2000 UTF-8 runes [CITED], requiring at most
8000 bytes [CITED: Unicode maximum encoding width]. The policy test pins that
validator bound so a future increase requires review of this byte cap.

A zero timeout selects the policy timeout; a positive value may only shorten it.
A zero stdin cap selects the policy cap; a positive cap may only reduce it.
`proto.StdinLimit` and `Allowed` use the same exact command match; the agent
never grants stdin by scanning argv operands for command words or helper hashes.
Record requires stdin_cap=-1 and timeout_ms=0 [CITED]. Record STDIN_EOF cancels the
process. A noninteractive recording shim must therefore avoid translating its
initial local stdin EOF into record cancellation; it sends CANCEL on shutdown.

The xdotool parser uses a finite subcommand table and validates every operand,
including the actual chained click, drag and scroll forms emitted by the
surface. Scroll permits balanced modifier keydown/keyup; click permits the
surface's clearmodifiers and repeat/delay forms. Command words are refused as key
operands because xdotool recognizes them as commands in that variadic position.
`srf-fbread` accepts only `pair :100 0`. The identity entrypoint accepts only
xpra-start or xpra-stop [CITED].

Python requires the isolated interpreter argv, with unbuffered mode only for the
record helper. Source hashes must belong both to the caller's helper hash set
and the generated policy table. The downloads helper additionally accepts exactly
the surface's folder and optional filename/action operands. Folder is
`/profile/exchange` or `/profile/Downloads`; action is delete or the surface's
fixed export bound. Filenames cannot escape that directory. The helper retains
its existing regular-file, O_NOFOLLOW and size checks.

## Generation and verification

`go generate ./src/vmsurface/helpers` parses the surface Go files using go/ast;
it extracts the literal constants pastePython, axPython, downloadsPython and
recordPython without importing the surface. `helpers_gen.go` records their exact
source SHA-256 and policy metadata. `TestSurfaceHashesCurrent` independently
reparses the source and fails on drift. `TestFullHelperPolicy` pins the complete
source hash, timeout, stdin cap and stream flag table; protocol tests inspect
actual surface builders and require shim deadlines strictly below their bounds. `helpers.Hashes()` returns a fresh hash
set; `helpers.Entries` exposes the metadata to consumers.

The guest uses bounded per-stream stdin queues, a bounded stream population and
separate stdout/stderr frames. CANCEL, timeout and peer loss kill the process
group. Pipe copying has a bounded shutdown delay. Normal EOF closes finite stdin;
record EOF cancels the stream. Tests re-execute the Go test binary, use net.Pipe
and fake Unix xpra sockets, and do not start a guest or graphical program.

Wire exit codes are success 0, refusal 2, unavailable executable/device 3,
malformed/oversize 5 and cancellation/timeout/dead peer 6 [CITED]. Child exit codes
in the permitted range are preserved; unsupported child codes, including 1 and
signal deaths, map to 6 [CITED: room exit-code constraint]. No argv, input text or
environment contents are included in diagnostics.

This implementation mandate is scoped to the supplied W3 design, frozen API and
surface helper/argv corpus.
