# Exact framebuffer RAM capture

The engine shares one session between capture and direct verification. A successful
`pair :100 <xid>` child supplies the framebuffer and GetImage under one server
grab; its successful exit attests deferred ungrab. Release does not read the
moving scene again. Reads execute once, with the caller deadline capped at two
seconds. The configured container resolver must use canonical srfd state.

Production stores require the live, locked shim run directory accepted by
`capture.DefaultGuard().CheckRunDir`. No fallback directory exists. The cockpit
owns the bounded shim process and closes the engine before stopping that child.
The production store samples global swap usage for every provenance request.
Unknown swap evidence is an error, never evidence of no swap.

Recording history uses exclusive `f<seq>.xwd` files. The bounded verification
workspace is `head.xwd`, replaced in place; historical frame files are never
removed at budget saturation. This is the JRC-04 headroom implementation: the
loop leaves two frame sizes of headroom and suspends history storage when full;
verification reuses its already-accounted head allocation, including repeated
checks of a moving scene. Seq advances for each changed verification head or
window identity. Probe reopens the production head file. Only TTL, explicit Reap
and engine shutdown remove the session workspace. Stop seals history and halts
the sampling ticker; TTL still owns the retained workspace.

## DEVIATIONS

| Deliberate Go hardening relative to xfb.py / ramws.py | Result |
|---|---|
| byte_order outside 0 or 1 | E_XWD_FORMAT |
| header_size or ncolors above 65536 | E_XWD_GEOMETRY |
| bytes_per_line above 65536 | E_XWD_GEOMETRY |
| XID outside uint32, noncanonical spelling or zero window-name XID | E_XWD_GEOMETRY |
| Any rise above initial host swap usage | Provenance Swap=true, latched |
| Host exec consistency | One exec; fbread owns consistent-read retries |
| Moving verification head | Fixed managed RAM head allocation; history retained |

The DEVIATIONS table in xwd_test.go asserts Go-only parser codes. The peer vector
file is copied byte-for-byte and pinned to SHA-256
`a737817b280623ee5ba53ba950155ca92ca51abb558cbd638af75e685e81f7c3`.
Its good cases check both complete XWD bytes and canonical RGB24 hashes; its bad
cases retain peer error-code ordering. Peer notice is recorded in bus_RB1.md.

All tests use synthetic bytes, fake execs, injected quota counters and memory
stores. They launch no docker, X server, browser or application and write no
frame bytes to disk. Production tmpfs writes and real server-grab behaviour
remain conductor live probes.

QueryPointer metadata from the successful pair's stderr JSON is validated and
appended to `pointer.jsonl` with the current sequence. It is never drawn into the
frame. Metadata stops appending if its reservation reaches a quota; this keeps
already-retained frames available for verification and sealing.
