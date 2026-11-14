# Cockpit native REC stream

REC uses `src/recstream` through `cockpit/recstream.go`. The existing
`RamCapture` interface and native save flow remain the boundary. The old
`src/ramcap` engine remains available through `wireRamcapEngine` for D11's raw,
bit-exact verification; native REC does not pass compressed video off as D11.

## Source and resampling

The adapter resolves the capsule using the canonical cockpit state, then starts
`docker exec -i <resolved-container> /usr/local/bin/srf-fbread stream :100 <xid>`
without a shell. An unresolved capsule starts neither child process.

The read-only `ramcap.StreamDecoder` verifies W8UH/W8UF/W8UE records, binding,
sequence, geometry, pair equality, pixel digest and credits. Host control sends
HELLO, initial credit and cumulative ACKs. Shutdown serializes an explicit W8UC
STOP after prior ACKs, suppresses later ACKs, closes stdin and waits for source
exit before disconnecting; cancellation bounds a failed reader. Captured pixels remain exact RGB24
at 1280 × 900 [CITED: operator ruling 16]; the scaled WALL view is never a source.

Native REC has no worker-seat attestation. Its session binding marks unavailable
seat/image/reader/allowlist metadata `AUSENTE`; these are not claimed digests or
an agent grant. Native epoch is a fixed sentinel, not a capsule lease epoch.
Authority comes from the existing recording lease/operator guard and canonical
container resolver, not from those metadata fields.

A single pending-frame mailbox resamples onto a 30 fps [TARGET] host clock.
A tick consumes the newest pending frame, or repeats the last frame. Replacing
an unconsumed frame increments `Dropped`; emitting the last frame again
increments `Duplicated`. `Frames` counts complete RGB24 writes to encoder stdin.
The initial valid frame is emitted immediately. There are no invented black
frames and no arrival-triggered bursts. A slow encoder applies backpressure;
clock ticks may be missed under host overload, which must be measured live.

## Encoder and storage

Host ffmpeg receives anonymous-pipe rawvideo with `-pix_fmt rgb24 -s 1280x900
-r 30 -i pipe:0`, then `libx264`, `ultrafast`, `zerolatency`, `yuv420p`,
`-movflags +frag_keyframe+empty_moov`, GOP 30 [TARGET], and fragmented MP4 on
`pipe:1`. Its encoded output alone goes into the private RAM file.

The engine refuses relative paths, paths outside `/dev/shm/vqa-ramws/`, symlink
aliases/escapes, non-tmpfs/ramfs mounts and non-private recording roots. Production
retains the existing live ramws shim ownership guard. Each recording directory
is mode 0700 [TARGET]; its video and metadata files are mode 0600 [TARGET]. Raw
XWD/RGB frames never become files. There is no temp video on a disk filesystem.

Defaults and upper limits are 10 minutes [TARGET] and 1 GiB [TARGET] of MP4.
Configuration can lower either limit; the supplied RAM TTL also limits duration.
The MP4 sink rejects any top-level box exceeding the remaining byte budget and
rolls back to the last complete video fragment. A trailing unpaired `moof` is
removed, without buffering an entire box in memory. A recording with no complete
video fragment fails closed. The file never exceeds its byte cap.

The four existing save artifacts [CITED: recording_policy.go] are created in RAM
at startup: `video-000.mp4`, `eventos.jsonl`, `FLUJO.md`, `manifiesto.json`.
The small sidecars are finalized with actual counters and the stop reason.
Stopping lets an in-flight raw frame finish before closing encoder stdin,
then waits for flushing/finalization. A bounded grace period forces the pipe
closed only if an encoder stalls; child cleanup is also bounded. Duration and byte caps stop capture automatically; the adapter
projects completion to sealed RAM, or an error on failure. The short Start call
context does not cancel a successful long recording. App shutdown/parent
cancellation stops and reaps it. `SizeBytes` returns MP4 bytes; Reap removes the
RAM recording and permits the next recording.

## Native save authority

The adapter installs the engine exporter in the recording policy when REC starts.
It adds no App method, Wails binding, HTTP route, CDP path or lease action.
The existing native menu/dialog still requires sealed RAM, unexpired TTL and an
operator-controlled unheld slot. Only that host flow invokes Export, copies the
MP4 and sidecars into a fresh directory under the operator's choice, verifies
them, then reaps RAM. Export refuses an active/failed session or relative target.
No agent-facing method chooses a disk destination.

## Verification scope

Offline tests use the pinned W8U golden vectors and expanded in-memory frames,
fake docker/ffmpeg children, a virtual cadence clock, malformed wire, refusal
paths, hard caps, native save and cleanup. Unit counter/clock results carry [VM];
they are not live playback or sustained throughput evidence. An opt-in `RECSTREAM_CODEC_TEST=1` check uses real ffmpeg with generated
pixels, then decodes both full and capped MP4s entirely in RAM. Live capsule
playback, live fps and multi-capsule load measurements are AUSENTE here and remain
conductor checks. No cockpit app, display server, browser or docker was
started for these tests.
