# srf-identity-surface: CLI contract (v1)

Binary: `build/bin/srf-identity-surface` (source `cmd/srf-identity-surface/main.go`, library `src/browserid/surface`).
The Go source is authoritative; this page is checked against it at the commit that adds it.

## Invocation and environment

```
srf-identity-surface <verb> [flags]
```

- One verb per process. Unknown flags, flags not allowed for the verb, positional arguments, or `--timeout` outside `(0, 3m]` give exit 2.
- Store and registry come from flags or env: `--store-root` / `SRF_IDENTITY_STORE_ROOT`, `--registry` / `SRF_PROXY_REGISTRY`. Verbs that touch a capsule need both (exit 3 otherwise). `--proxy` / `SRF_EGRESS_PROXY` defaults to the fixed internal proxy.
- `--state-dir` overrides the private control-state root (default under `/dev/shm`, owned by the calling campaign).
- Default deadline `--timeout 30s`; `start --kind vm` defaults to 60 s unless `--timeout` is given.

## Output

- Success: exactly one JSON object (one line) on stdout, exit 0. Exceptions: `capabilities` prints text, and `view` prints one JSON line and then blocks.
- Failure: one JSON line on stdout, `{"error":{"code":N,"detail":"..."}}`, plus a `"result"` key holding the zero-valued result when the verb has a typed result. stderr carries no runner output, typed text or secrets.

| Exit | Meaning |
|---|---|
| 0 | ok |
| 2 | usage: bad verb, flag, argument or id |
| 3 | setup: missing store/registry/proxy, host admission (kvm, memory) |
| 4 | unavailable: no capsule state, capsule or tool not reachable |
| 5 | malformed: an in-capsule helper returned invalid data |
| 6 | refused: policy, state conflict, cancelled readiness |

Exit 1 is never used.

## Verbs

### capabilities
No flags, no side effects. Two text lines, exit 0:
```
srf-identity-surface capabilities v1
--paste --type-stdin --scroll --drag downloads
```
Tokens come from a closed set; a token is added only together with the feature it names.

### start
Flags: `--identity ID` (required), `--windows 1..16` (default 1), `--capsule NAME` (optional; `srf-id-*`, `srf-pg-*` or `srf-vm-*` by kind), `--kind browser|program|vm` (default browser), `--program ID` (required for program and vm).
```json
{"kind":"program","container":"srf-id-x","run_id":"srf-id-x","windows":[{"id":"w1","x":0,"y":0,"w":1280,"h":900,"title":"","url":""}]}
```
`kind` is omitted for browser capsules. Window ids are `w1`..`w16`, stable for the capsule's life. Readiness is checked before return: the in-capsule ready line plus `xpra layer up`.

### stop
`--container C`. Result: `{"stopped":"C","removed_verified":true}`.

### frame
`--container C --window wN`. Writes one PNG to RAM (`/dev/shm` only, never disk) and returns:
```json
{"path":"/dev/shm/.../frames/w1/f000001.png","seq":1,"sha256_rgb24":"…","width":1280,"height":900,"capture_mono_ns":0,"generation":"C","window":{"id":"w1","x":0,"y":0,"w":1280,"h":900,"title":"…","url":""}}
```
`seq` increases per window. `sha256_rgb24` hashes the raw RGB24 pixels, not the PNG bytes.

### ax
`--container C --window wN [--text-container ID]`. Browser capsules only (filtered CDP shim); a program capsule gives exit 4.
```json
{"window":{…},"frame_seq":1,"scroll":{"x":0,"y":0},"elements":[{"id":"…","role":"button","name":"…","box":[x,y,w,h],"region":"main","actionable":true,"focused":false,"states":[],"container":null,"value_len":0}],"truncated":false,"skipped_frames":0}
```
`name` is cut to 120 characters. Field values are never returned, only `value_len`. `--text-container` returns the visible text of one container.

### input
`--container C --window wN` plus exactly one operation:

| Operation | Flags |
|---|---|
| key | `--key KEYSYM` (X keysym; chords such as `ctrl+t`) |
| text | `--type TEXT` or `--type-stdin` (UTF-8, at most 16 KiB, one trailing newline dropped), optionally `--paste` to enter it through the capsule clipboard and ctrl+v |
| click | `--click X,Y [--button N]` (window-local coordinates) |
| scroll | `--scroll X,Y --dir up\|down\|left\|right [--count 1..20] [--modifier ctrl\|shift]` |
| drag | `--drag X1,Y1:X2,Y2 [--button N]` |

Result:
```json
{"surface_id":"w1","kind":"click","input_mono_ns":0,"focus_verified":true,"result":"ok"}
```
Typed text is never logged or echoed.

### view / login
`view --container C [--port P] [--login [--record DEMO_ID]]`. Serves the capsule's HTML5 viewer on `127.0.0.1:P` (a free port when P is 0). It prints one line and then blocks until SIGINT/SIGTERM (at most 24 h):
```json
{"url":"http://127.0.0.1:P/","native":"…","socket":"…"}
```
- Without `--login`, the view is read-only.
- `--login` (or the verb `login`) enables operator input. On exit the capsule is restored to read-only, which takes about 10 s, announced on stderr.
- `--record` writes the operator's gestures as `demos/<id>/events.jsonl` in RAM, at most 500 events. Printable keys are stored only as `text_len`.

### view-stop
`--container C`. Stops the operator view started for C. Result: `{"stopped_view":"C","pid":N,"seconds":S}`.

### paste-text
Operator to capsule clipboard: `--container C` plus `--file F` (`-` reads stdin) or `--from-clipboard` (host clipboard via xclip). 1..16384 bytes of UTF-8. It requires the operator's login view (`view --login`) to be open on C, otherwise exit 4. Result: `{"pasted_runes":N,"container":"C"}`. The text is placed on the capsule clipboard; it is not typed.

### downloads
`--container C`:
- no other flag: lists finished downloads, `[{"name":"…","bytes":N,"mtime":T}]`;
- `--export NAME --to ABS_DIR`: copies one file out (at most 15 MiB, never overwrites), `{"exported":"/abs/path","bytes":N}`;
- `--delete NAME`: removes one file in the capsule, `{"deleted":"NAME"}`.

### net
- `net up [--proxy-bin B --policy P --audit-dir D]` starts the egress proxy network.
- `net down` stops it.

Result: `{"network":"up"|"down"}`.

## Stability

- The contract is v1. Removing a verb or a JSON field, or changing an exit code, requires a new `capabilities` header version.
- Clients should probe `capabilities` and pin the header line.
