# ref_overlay: recording + cursor overlay compositor (unit F2, Track P)

Reference implementation written by Claude (claude-fable-5-2), declared
non-Gemini product work. Standard library + `ffmpeg`/`ffprobe` only.

The overlay is a post-process of the recording. Nothing is drawn on the
captured X display, so it adds no changed regions there.

## Files

| File | Role |
|---|---|
| `record.sh` | x11grab recorder, prints the ffmpeg PID |
| `compose.py` | recording + `cursor.jsonl` + `events.jsonl` -> overlaid mp4 |
| `test_compose.py` | unittest suite (pixel sampling via PPM, stdlib parser) |

## Record

```bash
PID=$(./record.sh :99 take1 /path/outdir)   # -> outdir/take1_<HHMM>.mp4
kill -INT "$PID"                            # STOP: SIGINT only, by PID
```

- 1920x1080, 30 fps, libx264 ultrafast, real pointer not drawn
  (`-draw_mouse 0`; the overlay is the cursor).
- Sidecars: `take1_<HHMM>.start_ms` (epoch ms taken just before ffmpeg
  starts) and `take1_<HHMM>.log`.
- `kill -INT` lets ffmpeg finalise the mp4. `kill -9` leaves it unplayable.
- Display `:1` is refused (rc 3). An existing output is never overwritten (rc 4).
- ffmpeg runs in a `systemd-run --user --scope` with MemoryMax=8G when
  available (`VP_RECORD_NO_SCOPE=1` disables that).

## Compose

```bash
python3 compose.py --video outdir/take1_1133.mp4 --out outdir/take1_overlay.mp4 \
    --rt /dev/shm/vqa-cu/ref --start-file outdir/take1_1133.start_ms
```

`--rt` defaults to `$VP_SWARM_RT` or `/dev/shm/vqa-cu/ref` and supplies
`cursor.jsonl`, `events.jsonl` and `frames/`; `--cursor`, `--events`,
`--frames` override each one. `--start-ms <epoch ms>` replaces `--start-file`.
`--keep-filter <path>` saves the generated ffmpeg filter script. The command
prints one JSON summary line (drawn and skipped counts).

What is drawn:

| Record | Overlay |
|---|---|
| `cursor`, phase `intent` | hollow box (ghost) in the agent colour + label `<agent> plan`, until the agent's next cursor record, 6 s at most |
| `cursor`, phase `act` / `result` | solid box + label, 2 s / 1.5 s or until the agent's next record |
| `turn` | that agent's slot in the bottom queue strip is filled and tagged HEAD until the next `turn`; `turn N` at the left |
| `close` | badge with the verdict code in the agent's slot for 2.5 s |
| `control` | red `CONTROL <op>` tag above the strip for 3 s |

Queue strip: slots in ring order `1 modeler, 2 props, 3 look, 4 camera`
(override with `--ring a,b,c`); agents seen in the logs but not in the ring
are appended with a fallback colour.

Colours: modeler blue `2979FF`, props magenta `E040FB`, look cyan `00E5FF`,
camera yellow `FFEA00`.

Verdict badge colours:

| Colour | Verdicts |
|---|---|
| green | `VERIFIED`, `VERIFIED_WITH_SPILL`, `STATE_VERIFIED` |
| amber | `STALE_FRAME`, `FRAME_EVICTED`, `NO_LEASE`, `LEASE_EXPIRED`, `ACTUATOR_BUSY`, `FOOTPRINT_MISSING`, `TURN_TIMEOUT`, `QUEUE_FULL`, `DUPLICATE_EVENT`, `CLAIM_REFUSED`, `PASS`, `NO_VISIBLE_CHANGE` |
| red | `JOB_ERROR`, `OUTCOME_UNCERTAIN`, `INPUT_UNCERTAIN`, and any unknown code |

### Timing rule (read this before integrating)

The contract schema (02 section 7) only puts `ts_ms` on `enqueue`. `cursor`,
`turn` and `close` carry frame numbers, not times. compose.py therefore
resolves a record's time as:

1. the record's own `ts_ms` (epoch ms) if present, else
2. the `ts_ms` of the frame record it references in `RT/frames/frame_<N>.json`
   (`frame`, `verify_frame_ref`, `head_frame`, `dispatch_capture_seq`, in that
   order), else
3. the record is skipped and counted as `skipped_untimed`.

A record whose time falls outside `[0, duration)` of the video draws nothing
and is counted as `skipped_out_of_window`. Malformed JSON lines are counted as
`bad_json_lines` and skipped. All text taken from the logs is reduced to
`[A-Za-z0-9_ .+#-]` before it reaches the filter script.

## Tests

```bash
cd product/ref_overlay
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
    python3 -m unittest -v test_compose
```

17 tests, about 8 s on the build host. They need ffmpeg with libx264 and
DejaVu Sans.

Verified by the suite (synthetic 20 s 1920x1080 `testsrc`, 4 agents):

- each agent's colour is at its cursor position during its act, and the
  recording shows through before the intent and after the act dwell;
- an intent ghost has the agent colour on its border and the recording in its
  centre;
- the head highlight sits in the right slot for each turn and in no other;
- badges are green / amber / red for `VERIFIED` / `CLAIM_REFUSED` /
  `JOB_ERROR` and disappear after 2.5 s;
- outside the strip and the live cursor boxes, a 16 px grid of samples at four
  times matches the source within the re-encode tolerance (max channel
  difference 40, mean below 6);
- records at -1 s and +25 s draw nothing;
- negative control `NegativeControl.test_window_check_removed_goes_red`: with
  the window check disabled (`clip_to_window=False`) the same pixel sample
  turns agent-coloured, so the test above is not vacuous;
- plan logic without ffmpeg: verdict classes, intent replaced by act, frame
  timestamp fallback, invalid records, unknown agents, duplicate cursor
  records, hostile text in logs.

Verified by hand on the build host (not in the suite):

- mutation: replacing the window check in a copy of compose.py with `False`
  makes 4 of 17 tests fail;
- `record.sh` on a private `Xvfb :93`: printed PID was ffmpeg, `kill -INT`
  stopped it within 3 s, result 1920x1080 30 fps 4.97 s, playable, and
  compose.py accepted it with `--start-file`; `record.sh :1 ...` returned rc 3.

Not verified:

- no run against a real broker's `cursor.jsonl` / `events.jsonl`, so the
  timing rule above is untested against real records;
- sync accuracy of `.start_ms`: it is taken before ffmpeg opens the display,
  so overlays can lead the picture by the x11grab start-up time (not measured);
- long recordings: one filter per overlay item, cost for thousands of records
  is not measured;
- `record.sh` has no automated test, and was not run on a display with Blender;
- audio streams are copied, untested (x11grab recordings have none).

## Hardening (w186 cell, lane sw-overlay-projection)

Tests: `python3 -m unittest -v test_harden` (19 tests, each group with a
negative control). What changed in `compose.py`:

- Hostile logs: NaN/Infinity/huge numbers and list/dict values in `x`, `y`,
  `ts_ms`, `frame`, `agent_id`, `phase`, `event_id`, `op` are skipped and
  counted (`skipped_invalid`), never a traceback.
- At most `MAX_RING` (8) strip slots; further agent ids from the log are
  counted in `agents_dropped` and draw nothing.
- Labels: `safe_text` is still a whitelist (no escaping), now NFC-normalised
  and extended to non-ASCII letters/digits that the font can draw
  (`fc-query` charset). No glyph, no `fc-query`, quotes, `%`, `\`, bidi and
  control characters, lone surrogates: `_`.
- Enable windows are half-open `gte(t,a)*lt(t,b)`: `between` drew an agent's
  old and new cursor together on a frame that lands exactly on the boundary.
- `--out` equal to `--video` (also through a symlink) is refused; the encode
  goes to `<out>.part<pid>` and is renamed on success, so a failed encode
  keeps the previous output.
- Clean errors (no traceback) for a non-video input, a recording without
  duration and a bad `--start-file`/`--start-ms`.
- `--offset-ms N`: added to the start epoch, to calibrate the x11grab
  start-up lag noted above.
- Container `start_time` is reported (`start_time_s`) but not added: the
  ffmpeg CLI rebases the filter time to 0 (pinned by `StartTime` tests).

## fix-io round (xr-io cross-review)

Tests: `python3 -m unittest -v test_fixio` (5 tests; 3 of them failed
before the fixes). Full suite: 41 tests.

| Finding | State | What changed | Test |
|---|---|---|---|
| O1 MAJOR: `:01` / `:001.0` passed the display :1 guard | fixed | record.sh parses the display number (`^:N(.S)?$`, base 10) and refuses value 1 with rc 3; anything not `:N` or `:N.S` is rc 2 (this also rejects `:1x`, `host:1`) | `DisplayOneGuard` (a stub ffmpeg is first on PATH, no display is ever grabbed) |
| O3 MINOR: two cursors of one agent at an equal ts both drawn | fixed | the later record in file order replaces the earlier one | `EqualTsCursors` |
| O2 MINOR: filter cost grows with records x frames | open | not changed; measured by the reviewer at 4000 records -> 113.8 s / 1.73 GB on a 10 s 1080p30 clip. Keep cursor logs under about 1000 records per take, or split the take | none |
