# P3 — X11 MPX (`xinput create-master`) + Blender 4.0.2 spike report

- Track / unit: **Track P, unit P3** (`claude-fable-5-2`, declared non-Gemini product spike, report only).
- Start: `2026-11-14T11:53:32+01:00` [MEASURED `date -Is`, rc 0]
- End (after `vision stop`): `2026-11-14T11:55:45+01:00` [MEASURED `date -Is`, rc 0]
- `vision` job: **`vpmpx`**, display **`:2`**, scope `vis-vpmpx-3979035.scope` [MEASURED start JSON: `"code":"OK","display":":2"`, rc 0]
- Stop receipt: **`~/.local/state/vis/receipts/vpmpx-4ba1fd64dee84869ab8ee429847cbebe.jsonl`** (latest alias `~/.local/state/vis/receipts/vpmpx.jsonl`) [MEASURED `python3 -I ~/Documentos/vis-skill/vision stop --job vpmpx` → `"code":"OK"`, rc 0]
- Displays `:0`, `:1`, job `vpswarm` and `/dev/shm/vqa-cu/hybrid/` were not touched. No `sudo`, no installs, no `pkill`.

**VERDICT: MPX_ROADMAP_ONLY**

The X server gives two fully independent master pointers, but Blender 4.0.2 merges them into one cursor and one window/modal state. A second pointer moved a transform that the first pointer started, then confirmed it with its own click. The MVP architecture (off-display cursor overlay + serialized turn queue) is confirmed.

## 1. Method and its limits

All X commands ran as `systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 env DISPLAY=:2 <cmd>`.

| Item | What was done |
| :--- | :--- |
| Job start | `cd /tmp && python3 -I ~/Documentos/vis-skill/vision start --job vpmpx --size 1280x720 --mem 6G --app "blender --factory-startup" --wait-title Blender` (rc 0) |
| Blender-side observation | The `vision`-launched Blender (PID 3979046, window 4194306) has no introspection channel. A **second Blender 4.0.2 instance** was started on the same display `:2` with a logging script: `blender --factory-startup --python /tmp/vpmpx-p3/logger.py` (PID 3981867, window 6291458, 1280x701 at 0,38). All Blender-side measurements below come from this instance, one window. |
| Logger | Pass-through modal operator writing every event (`type`, `value`, `mouse_x`, `mouse_y`) plus a 50 ms timer writing the Cube location on change, to `/tmp/vpmpx-p3/blender_events.log`. |
| Pointer driver | `python3 -I /tmp/vpmpx-p3/mpx.py` (ctypes over `libXi.so.6` / `libXtst.so.6` / `libX11.so.6`): `XIWarpPointer`, `XIQueryPointer`, and `XISetClientPointer(dev)` followed by `XTestFake{Motion,Button,Key}Event`, so each fake event is routed through the chosen master's XTEST slave. |
| Scratch files | Created outside the worktree in `/tmp/vpmpx-p3/` (`mpx.py`, `logger.py`, logs). Nothing but this report was written in the worktree. |

Scratch artefact hashes [MEASURED `sha256sum`, rc 0]:

| File | sha256 |
| :--- | :--- |
| `/tmp/vpmpx-p3/blender_events.log` (32 lines) | `01c0a556d7b24c39157334c0ef742bc7fa82a99df77daf91c807835e5e887078` |
| `/tmp/vpmpx-p3/xi2_T1.log` | `ab7d53d93dfce25db32d73f286f4c3b96c0cba132ef59bc4261a9bad8e16a95d` |
| `/tmp/vpmpx-p3/mpx.py` | `3e59b6ff3c8e8f01a2e149c6ec16388352c0504c1aaf7e31b403f767128336b7` |
| `/tmp/vpmpx-p3/logger.py` | `a84a6ac41cd817dfbce5e28788b3ad9d8c8d0927115d1b914de1b3034b1e4b19` |

Not tested (AUSENTE — no inventar): the unmodified `vision`-launched instance's internal state; Blender source-level XI2 handling; `xinput set-cp` per-window client pointers; two separate Blender windows/processes with one master each; real (non-XTEST) hardware devices; a real GPU display server instead of Xvfb.

## 2. Tool probes

| Probe | Result |
| :--- | :--- |
| `command -v xinput` | `/usr/bin/xinput`, rc 0 [MEASURED] |
| `xinput --version` (on `:2`) | `xinput version 1.6.4`, `XI version on server: 2.4`, rc 0 [MEASURED] |
| `blender --version` | `Blender 4.0.2`, rc 0 [MEASURED] |
| `command -v xdpyinfo` | `/usr/bin/xdpyinfo`, rc 0 [MEASURED] |
| `xdotool --version` | `xdotool version 3.20160805.1`, rc 0 [MEASURED] |
| `xdpyinfo` on `:2` | lists `XInputExtension` and `XTEST`; `dimensions: 1280x720 pixels`, rc 0 [MEASURED] |
| `python3 -I -c "import Xlib"` | `ModuleNotFoundError`, rc 1 → python-xlib AUSENTE (ctypes used instead) [MEASURED] |

## 3. X-server level: MPX works

`xinput list` before (rc 0) [MEASURED]: one master pair — `Virtual core pointer id=2`, `Virtual core keyboard id=3`, slaves 4, 5, 6, 7.

`xinput create-master SecondAgent` → rc 0 [MEASURED]. `xinput list` after (rc 0) [MEASURED]:

```
⎡ Virtual core pointer                    	id=2	[master pointer  (3)]
⎜   ↳ Virtual core XTEST pointer              	id=4	[slave  pointer  (2)]
⎜   ↳ Xvfb mouse                              	id=6	[slave  pointer  (2)]
⎣ Virtual core keyboard                   	id=3	[master keyboard (2)]
    ↳ Virtual core XTEST keyboard             	id=5	[slave  keyboard (3)]
    ↳ Xvfb keyboard                           	id=7	[slave  keyboard (3)]
⎡ SecondAgent pointer                     	id=8	[master pointer  (9)]
⎜   ↳ SecondAgent XTEST pointer               	id=10	[slave  pointer  (8)]
⎣ SecondAgent keyboard                    	id=9	[master keyboard (8)]
    ↳ SecondAgent XTEST keyboard              	id=11	[slave  keyboard (9)]
```

Independent positions [MEASURED `python3 -I mpx.py query 2 query 8 warp 2 300 300 warp 8 900 500 sleep 0.2 query 2 query 8`, rc 0]:

| Moment | Master 2 `root_xy` | Master 8 `root_xy` |
| :--- | :--- | :--- |
| Before warp | (640,360) | (640,360) |
| After warp | (300,300) | (900,500) |

After the interleaved test T1 the server still held two distinct positions: master 2 at (260,300), master 8 at (960,500) [MEASURED `mpx.py query 2 query 8`, rc 0]. `xinput test-xi2 --root` during T1 logged Motion events with distinct device ids 4 and 10, i.e. the two XTEST slaves [MEASURED, `timeout 6` → rc 124 by design].

`xinput remove-master "SecondAgent pointer"` → rc 0; `xinput list | grep -c SecondAgent` → `0` [MEASURED].

## 4. Blender 4.0.2 level: the two pointers collapse into one

Blender `mouse_y` = 719 − root y in this window [MEASURED: root (300,300) logged as `xy=(300,419)`].

### T1 — interleaved motion, no buttons

Driver: four alternating steps, master 2 along x = 200→260 at root y 300, master 8 along x = 900→960 at root y 500, 0.15 s apart (rc 0). Blender log [MEASURED]:

```
1794653706.972 EV type=MOUSEMOVE value=NOTHING xy=(200,419)
1794653707.121 EV type=MOUSEMOVE value=NOTHING xy=(900,219)
1794653707.275 EV type=MOUSEMOVE value=NOTHING xy=(220,419)
1794653707.423 EV type=MOUSEMOVE value=NOTHING xy=(920,219)
1794653707.571 EV type=MOUSEMOVE value=NOTHING xy=(240,419)
1794653707.725 EV type=MOUSEMOVE value=NOTHING xy=(940,219)
1794653707.874 EV type=MOUSEMOVE value=NOTHING xy=(260,419)
1794653708.028 EV type=MOUSEMOVE value=NOTHING xy=(960,219)
```

Blender receives one `MOUSEMOVE` stream whose single cursor position jumps about 700 px between the two masters. Nothing in the event identifies which device produced it.

### T2 — modal operator started by master 2, disturbed by master 8

Driver sequence (rc 0) and Cube location from the logger [MEASURED]:

| Step (driver mark time) | Action | Blender log |
| :--- | :--- | :--- |
| 1794653722.7 | master 2 → root (400,350); `g` key via master 2's keyboard | `EV type=G value=PRESS xy=(400,369)`; Cube at `0,0,0` |
| 1794653723.477 | master 2 → (430,350) | `STATE cube=0.3014,0.6704,-0.0097` at .521 |
| 1794653723.877 | **master 8 only** → (1000,560) | `STATE cube=8.0958,12.4119,-4.8006` at .917 |
| 1794653724.278 | **master 8 only** → (950,450) | `STATE cube=6.5105,11.8166,-2.3718` at 724.315 |
| 1794653724.679 | **master 8** LMB down/up | modal ends: logger sees `LEFTMOUSE RELEASE xy=(950,269)` at .739 |
| 1794653725.231 | master 2 → (500,400) | `MOUSEMOVE xy=(500,319)`, no further `STATE` line: Cube stays at `6.5105,11.8166,-2.3718` |

The grab owned by master 2 followed master 8's cursor (30 px of master 2 travel gave 0.30 units on X; master 8's motion took it to 8.10) and master 8's click committed it. This is a direct modal/active-cursor conflict.

### T3 — overlapping button holds

Driver (rc 0): master 2 LMB down at (300,300); master 8 moves to (900,500), LMB down, LMB up; master 2 moves to (350,350), LMB up. While both were held, `XIQueryPointer` reported button 1 down on both masters independently (`buttons_first4=2, 0, 0, 0` for dev 2 and dev 8) [MEASURED]. Blender log [MEASURED]:

```
1794653735.399 EV type=MOUSEMOVE value=NOTHING xy=(300,419)
1794653735.700 EV type=LEFTMOUSE value=PRESS xy=(300,419)
1794653735.997 EV type=MOUSEMOVE value=NOTHING xy=(900,219)
1794653735.997 EV type=LEFTMOUSE value=CLICK_DRAG xy=(900,219)
1794653736.797 EV type=MOUSEMOVE value=NOTHING xy=(350,369)
1794653737.100 EV type=LEFTMOUSE value=RELEASE xy=(350,369)
```

Blender saw one press and one release. Master 8's motion was read as master 2's drag (`CLICK_DRAG` at master 8's position), and master 8's own press/release produced no separate Blender events.

## 5. Answer to the experimental question

| Question | Answer | Evidence |
| :--- | :--- | :--- |
| Does `xinput create-master` work on the private Xvfb? | Yes | §3: rc 0, masters 2 and 8 hold independent positions and button state |
| Does Blender 4.0.2 treat the two masters as independent pointers in one window? | No | T1: single merged cursor stream |
| Are modal operators isolated per pointer? | No | T2: master 8 moved and confirmed master 2's grab |
| Are button states isolated per pointer? | No | T3: one press/release pair, cross-pointer `CLICK_DRAG` |

Architectural consequence:

- **MVP**: keep one real X pointer per Blender window; draw per-agent cursors in an off-display overlay; serialize agent input through the turn queue so only one agent's gesture (press → modal → confirm) is live at a time.
- **Roadmap**: MPX is usable only as an X-level transport. Using it for true simultaneity would need Blender-side per-device event routing (AUSENTE in 4.0.2 as measured) or one Blender process/window per master, which was not tested here.

## 6. Cleanup

| Action | Result |
| :--- | :--- |
| `kill 3981867` (logger Blender, recorded PID) | rc 0; `kill -0 3981867` → rc 1 (gone) [MEASURED] |
| `xinput remove-master "SecondAgent pointer"` | rc 0 [MEASURED] |
| `python3 -I ~/Documentos/vis-skill/vision stop --job vpmpx` | rc 0, `"code":"OK"`, receipt `~/.local/state/vis/receipts/vpmpx-4ba1fd64dee84869ab8ee429847cbebe.jsonl` [MEASURED] |
| `pgrep -a blender` / `pgrep -a Xvfb` after stop | no `vpmpx` process left; the remaining Blender (PID 3735585) and Xvfb processes belong to other jobs and were not touched [MEASURED] |
