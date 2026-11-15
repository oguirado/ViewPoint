#!/usr/bin/env python3
"""ViewPoint reference overlay compositor (Track P, Claude-written).

Reads a screen recording plus the broker's JSONL logs (RT/cursor.jsonl and
RT/events.jsonl of the shared contract) and writes a new mp4 with:

  * one coloured, labelled cursor per agent at the position and time of each
    "cursor" record (phase intent = hollow ghost, act/result = solid box);
  * a bottom queue strip: ring order, current head (from "turn" records);
  * a verdict badge per "close" record (green / amber / red).

Nothing is drawn on the captured display: this is a pure post-process of the
recording, built as a generated ffmpeg filter script (drawbox / drawtext with
enable='between(t,a,b)'). Standard library + ffmpeg/ffprobe only.
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata

RING_DEFAULT = ["modeler-agent", "props-agent", "look-agent", "camera-agent"]
AGENT_COLOURS = {
    "modeler-agent": "2979FF",  # blue
    "props-agent": "E040FB",    # magenta
    "look-agent": "00E5FF",     # cyan
    "camera-agent": "FFEA00",   # yellow
}
EXTRA_COLOURS = ["B0BEC5", "8D6E63", "9E9D24", "7E57C2"]
# Agent ids come from untrusted logs: the strip never grows past this many
# slots (more would shrink a slot to an unreadable or negative width).
MAX_RING = 8
# Test seams (negative controls flip them; production never does).
HALF_OPEN = True          # enable windows are [a, b), not [a, b]
# Measured (test_harden.StartTime): the ffmpeg CLI rebases input timestamps
# so the filter's t is 0 on the first frame even when the container
# start_time is not 0. Adding start_time would shift the whole overlay; the
# seam exists only so the test can prove that.
ADD_START_TIME = False

GREEN, AMBER, RED = "00C853", "FF9100", "FF1744"
VERDICT_GREEN = {"VERIFIED", "VERIFIED_WITH_SPILL", "STATE_VERIFIED"}
VERDICT_AMBER = {  # refusals / nothing happened: the event did not act
    "STALE_FRAME", "FRAME_EVICTED", "NO_LEASE", "LEASE_EXPIRED",
    "ACTUATOR_BUSY", "FOOTPRINT_MISSING", "TURN_TIMEOUT", "QUEUE_FULL",
    "DUPLICATE_EVENT", "CLAIM_REFUSED", "PASS", "NO_VISIBLE_CHANGE",
}
# Everything else (JOB_ERROR, OUTCOME_UNCERTAIN, INPUT_UNCERTAIN, unknown
# codes) is red: an unknown verdict must never look like a success.

DWELL_S = {"intent": 6.0, "act": 2.0, "result": 1.5}
BADGE_S = 2.5
CONTROL_S = 3.0
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def verdict_colour(verdict):
    if verdict in VERDICT_GREEN:
        return GREEN
    if verdict in VERDICT_AMBER:
        return AMBER
    return RED


_ASCII_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                      "0123456789_ .+#-")
_GLYPHS = None  # sorted [(lo, hi)] code point ranges of the active font


def font_charset(fontfile):
    """Code point ranges the font can draw (fontconfig's fc-query), or None.

    None means unknown: safe_text then keeps ASCII only, exactly as before.
    """
    if not fontfile or not shutil.which("fc-query"):
        return None
    try:
        r = subprocess.run(["fc-query", "-f", "%{charset}", fontfile],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    ranges = []
    for tok in r.stdout.split():
        m = re.fullmatch(r"([0-9a-fA-F]{1,6})(?:-([0-9a-fA-F]{1,6}))?", tok)
        if m:
            lo = int(m.group(1), 16)
            ranges.append((lo, int(m.group(2), 16) if m.group(2) else lo))
    return sorted(ranges) or None


def set_font(fontfile):
    """Selects the font whose coverage decides which non-ASCII letters pass."""
    global _GLYPHS
    _GLYPHS = font_charset(fontfile)
    return _GLYPHS is not None


def _has_glyph(cp):
    if not _GLYPHS:
        return False
    lo, hi = 0, len(_GLYPHS) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        a, b = _GLYPHS[mid]
        if cp < a:
            hi = mid - 1
        elif cp > b:
            lo = mid + 1
        else:
            return True
    return False


def safe_text(s, limit=40):
    """Text that needs no drawtext escaping (logs are untrusted input).

    A whitelist, not an escaper: ASCII [A-Za-z0-9_ .+#-] plus letters and
    digits outside ASCII that the active font can draw (set_font). Quotes,
    backslash, %, :, braces, control and bidi characters, lone surrogates and
    letters without a glyph all become "_". Input is NFC-normalised first so
    "e" + combining accent is one drawable letter.
    """
    try:
        text = unicodedata.normalize("NFC", str(s))
    except (ValueError, TypeError):
        text = str(s)
    out = []
    for ch in text:
        if len(out) >= limit:
            break
        if ch in _ASCII_OK:
            out.append(ch)
        elif ord(ch) > 127 and ch.isalnum() and _has_glyph(ord(ch)):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


def short_name(agent_id):
    a = str(agent_id)
    return safe_text(a[:-6] if a.endswith("-agent") else a, 16)


def read_jsonl(path):
    """Returns (records, n_bad_lines). Bad lines are counted, never guessed."""
    recs, bad = [], 0
    if not path or not os.path.exists(path):
        return recs, bad
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(obj, dict):
                recs.append(obj)
            else:
                bad += 1
    return recs, bad


def load_frame_ts(frames_dir):
    """frame number -> ts_ms from RT/frames/frame_<N>.json (contract)."""
    out = {}
    if not frames_dir or not os.path.isdir(frames_dir):
        return out
    for name in os.listdir(frames_dir):
        m = re.fullmatch(r"frame_(\d+)\.json", name)
        if not m:
            continue
        try:
            with open(os.path.join(frames_dir, name), encoding="utf-8") as fh:
                rec = json.load(fh)
            out[int(m.group(1))] = int(rec["ts_ms"])
        except (ValueError, KeyError, TypeError, OSError):
            continue
    return out


def _num(v):
    """A finite JSON number. json.loads yields NaN/Infinity and unbounded
    ints; both would crash int() or poison every comparison downstream."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return abs(v) < 2 ** 53
    return isinstance(v, float) and math.isfinite(v)


def _sid(v):
    """A usable id: a non-empty string (lists/dicts are not even hashable)."""
    return isinstance(v, str) and bool(v)


def record_ts_ms(rec, frame_ts):
    """Wall-clock ms of a record: its own ts_ms, else its frame's ts_ms.

    The contract schema only guarantees ts_ms on "enqueue"; for the other
    records we accept an optional ts_ms and fall back to the frame record
    they reference. None = untimed (the caller skips and reports it).
    """
    if _num(rec.get("ts_ms")):
        return float(rec["ts_ms"])
    for key in ("frame", "verify_frame_ref", "head_frame",
                "dispatch_capture_seq"):
        v = rec.get(key)
        if _num(v) and int(v) in frame_ts:
            return float(frame_ts[int(v)])
    return None


def agent_of_event(event_id, enq_agent, known):
    if not _sid(event_id):
        return None
    if event_id in enq_agent:
        return enq_agent[event_id]
    eid = event_id
    for a in sorted(known, key=len, reverse=True):
        if eid.startswith(a + "-"):
            return a
    return None


def build_plan(cursor_recs, event_recs, start_ms, duration, width, height,
               frame_ts=None, ring=None, clip_to_window=True):
    """Pure function: logs -> list of timed draw items + stats.

    clip_to_window=True is THE check that a record whose time is outside
    [0, duration) draws nothing. Tests flip it to prove they would notice.
    """
    frame_ts = frame_ts or {}
    stats = {"cursor_drawn": 0, "turn_drawn": 0, "badge_drawn": 0,
             "control_drawn": 0, "skipped_untimed": 0,
             "skipped_out_of_window": 0, "skipped_invalid": 0}

    def rel(rec):
        ts = record_ts_ms(rec, frame_ts)
        if ts is None:
            stats["skipped_untimed"] += 1
            return None
        t = (ts - start_ms) / 1000.0
        if clip_to_window and not (0.0 <= t < duration):
            stats["skipped_out_of_window"] += 1
            return None
        return t

    # cursor records may live in cursor.jsonl and also in events.jsonl
    seen, cursors = set(), []
    for rec in list(cursor_recs) + [r for r in event_recs
                                    if r.get("rec") == "cursor"]:
        if rec.get("rec", "cursor") != "cursor":
            continue
        key = json.dumps(rec, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        cursors.append(rec)

    ring_in, ring = list(ring or RING_DEFAULT), []
    for a in ring_in:  # operator input: drop blanks and repeats, keep order
        if _sid(a) and a not in ring:
            ring.append(a)
    dropped = set(ring[MAX_RING:])
    ring = ring[:MAX_RING]
    enq_agent = {}
    for rec in event_recs:
        if (rec.get("rec") == "enqueue" and _sid(rec.get("event_id"))
                and _sid(rec.get("agent_id"))):
            enq_agent[rec["event_id"]] = rec["agent_id"]
    for rec in cursors + event_recs:
        a = rec.get("agent_id")
        if _sid(a) and a not in ring:
            if len(ring) < MAX_RING:
                ring.append(a)
            else:
                dropped.add(a)
    stats["agents_dropped"] = len(dropped)
    colours = {}
    extra = 0
    for a in ring:
        if a in AGENT_COLOURS:
            colours[a] = AGENT_COLOURS[a]
        else:
            colours[a] = EXTRA_COLOURS[extra % len(EXTRA_COLOURS)]
            extra += 1

    s = height / 1080.0
    px = lambda v, lo=1: max(lo, int(round(v * s)))
    geo = {
        "s": s, "W": width, "H": height,
        "cur": px(24, 8) // 2 * 2, "cur_t": px(3, 2),
        "font": px(20, 8), "font_small": px(16, 7),
        "strip_h": px(72, 24),
    }
    geo["strip_y"] = height - geo["strip_h"]
    left = px(110, 30)
    slot_w = (width - left - px(8)) // max(1, len(ring))
    geo["slots"] = {}
    for i, a in enumerate(ring):
        x = left + i * slot_w
        geo["slots"][a] = {
            "x": x, "y": geo["strip_y"] + px(6), "w": slot_w - px(8),
            "h": geo["strip_h"] - px(12),
            "badge": [x + (slot_w - px(8)) // 2, geo["strip_y"] + px(38),
                      (slot_w - px(8)) // 2 - px(6), px(24, 9)],
        }

    items = []

    # --- cursors -----------------------------------------------------
    timed = []
    for rec in cursors:
        a, phase = rec.get("agent_id"), rec.get("phase")
        if (not _sid(a) or a not in colours or not isinstance(phase, str)
                or phase not in DWELL_S
                or not _num(rec.get("x")) or not _num(rec.get("y"))):
            stats["skipped_invalid"] += 1
            continue
        t = rel(rec)
        if t is None:
            continue
        timed.append((t, a, phase, int(rec["x"]), int(rec["y"]),
                      rec.get("event_id")))
    timed.sort(key=lambda r: r[0])
    for i, (t, a, phase, x, y, eid) in enumerate(timed):
        end = t + DWELL_S[phase]
        for t2, a2 in ((r[0], r[1]) for r in timed[i + 1:]):
            # >=: the sort is stable, so a later record of the same agent
            # with an equal ts replaces this one (end == t -> not drawn)
            if a2 == a and t2 >= t:
                end = min(end, t2)  # the agent's next record replaces it
                break
        end = min(end, duration) if clip_to_window else end
        if end <= t:
            continue
        half = geo["cur"] // 2
        x = min(max(x, half), width - half - 1)
        y = min(max(y, half), height - half - 1)
        items.append({"type": "cursor", "agent": a, "phase": phase,
                      "ghost": phase == "intent", "x": x, "y": y,
                      "a": t, "b": end, "colour": colours[a],
                      "event_id": eid})
        stats["cursor_drawn"] += 1

    # --- turns (queue head) -----------------------------------------
    turns = []
    for rec in event_recs:
        if rec.get("rec") != "turn":
            continue
        if not _sid(rec.get("agent_id")) or rec["agent_id"] not in colours:
            stats["skipped_invalid"] += 1
            continue
        t = rel(rec)
        if t is not None:
            turns.append((t, rec["agent_id"], rec.get("turn")))
    turns.sort(key=lambda r: r[0])
    for i, (t, a, n) in enumerate(turns):
        end = turns[i + 1][0] if i + 1 < len(turns) else max(duration, t)
        end = min(end, duration) if clip_to_window else end
        if end <= t:
            continue
        items.append({"type": "head", "agent": a, "turn": n, "a": t,
                      "b": end, "colour": colours[a]})
        stats["turn_drawn"] += 1

    # --- verdict badges ---------------------------------------------
    closes = []
    for rec in event_recs:
        if rec.get("rec") != "close":
            continue
        a = rec.get("agent_id") or agent_of_event(
            rec.get("event_id"), enq_agent, colours)
        verdict = rec.get("verdict")
        if not _sid(a) or a not in colours or not isinstance(verdict, str):
            stats["skipped_invalid"] += 1
            continue
        t = rel(rec)
        if t is not None:
            closes.append((t, a, verdict, rec.get("event_id")))
    closes.sort(key=lambda r: r[0])
    for i, (t, a, verdict, eid) in enumerate(closes):
        end = t + BADGE_S
        for t2, a2, _, _ in closes[i + 1:]:
            if a2 == a and t2 > t:
                end = min(end, t2)
                break
        end = min(end, duration) if clip_to_window else end
        if end <= t:
            continue
        items.append({"type": "badge", "agent": a, "verdict": verdict,
                      "colour": verdict_colour(verdict), "a": t, "b": end,
                      "event_id": eid})
        stats["badge_drawn"] += 1

    # --- control records --------------------------------------------
    for rec in event_recs:
        if rec.get("rec") != "control":
            continue
        if not isinstance(rec.get("op"), str):
            stats["skipped_invalid"] += 1
            continue
        t = rel(rec)
        if t is None:
            continue
        items.append({"type": "control", "op": safe_text(rec.get("op"), 12),
                      "a": t, "b": min(t + CONTROL_S, duration)})
        stats["control_drawn"] += 1

    return {"ring": ring, "colours": colours, "geo": geo, "items": items,
            "stats": stats, "duration": duration}


def _en(a, b, t0=0.0):
    """enable window [a, b) in record time; t0 = stream start_time.

    Half-open on purpose: between() is closed on both ends, so a record that
    lands exactly on a frame time drew the agent's old AND new cursor on that
    frame. Both bounds print with the same %.3f, so windows that share a
    bound neither overlap nor leave a gap.
    """
    if not HALF_OPEN:
        return "enable='between(t,%.3f,%.3f)'" % (a + t0, b + t0)
    return "enable='gte(t,%.3f)*lt(t,%.3f)'" % (a + t0, b + t0)


def build_filter(plan, fontfile):
    """Plan -> ffmpeg filtergraph text (one filter per line, comma-chained)."""
    g = plan["geo"]
    W, H = g["W"], g["H"]
    f = []
    font = "fontfile=%s" % fontfile
    px = lambda v, lo=1: max(lo, int(round(v * g["s"])))
    t0 = float(plan.get("t0") or 0.0)

    # static queue strip
    f.append("drawbox=x=0:y=%d:w=%d:h=%d:color=black@0.78:t=fill"
             % (g["strip_y"], W, g["strip_h"]))
    f.append("drawtext=%s:text='QUEUE':x=%d:y=%d:fontsize=%d:fontcolor=white"
             % (font, px(10), g["strip_y"] + px(10), g["font"]))
    for i, a in enumerate(plan["ring"]):
        sl = g["slots"][a]
        f.append("drawbox=x=%d:y=%d:w=%d:h=%d:color=0x%s:t=%d"
                 % (sl["x"], sl["y"], sl["w"], sl["h"], plan["colours"][a],
                    px(3, 2)))
        f.append("drawtext=%s:text='%d %s':x=%d:y=%d:fontsize=%d:"
                 "fontcolor=white"
                 % (font, i + 1, short_name(a), sl["x"] + px(10),
                    sl["y"] + px(6), g["font"]))

    for it in plan["items"]:
        en = _en(it["a"], it["b"], t0)
        if it["type"] == "head":
            sl = g["slots"][it["agent"]]
            f.append("drawbox=x=%d:y=%d:w=%d:h=%d:color=0x%s:t=fill:%s"
                     % (sl["x"], sl["y"], sl["w"], sl["h"], it["colour"], en))
            idx = plan["ring"].index(it["agent"]) + 1
            f.append("drawtext=%s:text='%d %s  HEAD':x=%d:y=%d:fontsize=%d:"
                     "fontcolor=black:%s"
                     % (font, idx, short_name(it["agent"]), sl["x"] + px(10),
                        sl["y"] + px(6), g["font"], en))
            if it.get("turn") is not None:
                f.append("drawtext=%s:text='turn %s':x=%d:y=%d:fontsize=%d:"
                         "fontcolor=white:%s"
                         % (font, safe_text(it["turn"], 8), px(10),
                            g["strip_y"] + px(40), g["font_small"], en))
    # badges after heads so a head fill never hides a badge
    for it in plan["items"]:
        en = _en(it["a"], it["b"], t0)
        if it["type"] == "badge":
            bx, by, bw, bh = g["slots"][it["agent"]]["badge"]
            f.append("drawbox=x=%d:y=%d:w=%d:h=%d:color=0x%s:t=fill:%s"
                     % (bx, by, bw, bh, it["colour"], en))
            f.append("drawbox=x=%d:y=%d:w=%d:h=%d:color=black:t=1:%s"
                     % (bx, by, bw, bh, en))
            f.append("drawtext=%s:text='%s':x=%d:y=%d:fontsize=%d:"
                     "fontcolor=black:%s"
                     % (font, safe_text(it["verdict"], 22), bx + px(14, 5),
                        by + px(4), g["font_small"], en))
        elif it["type"] == "control":
            f.append("drawtext=%s:text='CONTROL %s':x=%d:y=%d:fontsize=%d:"
                     "fontcolor=white:box=1:boxcolor=0x%s:boxborderw=%d:%s"
                     % (font, it["op"], px(10), g["strip_y"] - px(40),
                        g["font"], RED, px(6), en))
    # cursors last: always on top
    for it in plan["items"]:
        if it["type"] != "cursor":
            continue
        en = _en(it["a"], it["b"], t0)
        c, half = g["cur"], g["cur"] // 2
        x0, y0 = it["x"] - half, it["y"] - half
        if it["ghost"]:
            f.append("drawbox=x=%d:y=%d:w=%d:h=%d:color=0x%s:t=%d:%s"
                     % (x0, y0, c, c, it["colour"], g["cur_t"], en))
            box = "boxcolor=0x%s@0.45" % it["colour"]
        else:
            f.append("drawbox=x=%d:y=%d:w=%d:h=%d:color=0x%s:t=fill:%s"
                     % (x0, y0, c, c, it["colour"], en))
            f.append("drawbox=x=%d:y=%d:w=%d:h=%d:color=black:t=1:%s"
                     % (x0 - 1, y0 - 1, c + 2, c + 2, en))
            box = "boxcolor=0x%s" % it["colour"]
        label = short_name(it["agent"]) + (" plan" if it["ghost"] else "")
        lx = it["x"] + half + px(8)
        if lx + px(130) > W:  # keep the label on screen
            lx = max(0, it["x"] - half - px(130))
        ly = min(max(it["y"] - half, 0), H - px(30))
        f.append("drawtext=%s:text='%s':x=%d:y=%d:fontsize=%d:"
                 "fontcolor=black:box=1:%s:boxborderw=%d:%s"
                 % (font, safe_text(label), lx, ly,
                    g["font_small"], box, px(4), en))
    return ",\n".join(f) + "\n"


def _fnum(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def probe_info(video):
    """width, height, duration and start (pts of the first frame, seconds).

    ffprobe prints "N/A" or omits duration for a recording that never got
    its trailer (killed recorder): that is a clean error here, not a
    traceback. start is reported in the summary only (see ADD_START_TIME).
    """
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries",
             "stream=width,height,duration,start_time:"
             "format=duration,start_time", "-of", "json", video],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as ex:
        raise SystemExit("ffprobe failed on %s: %r" % (video, ex))
    if r.returncode != 0:
        raise SystemExit("ffprobe failed on %s rc=%d: %s"
                         % (video, r.returncode, r.stderr.strip()[-500:]))
    try:
        j = json.loads(r.stdout)
        st, fmt = j["streams"][0], j.get("format") or {}
        width, height = int(st["width"]), int(st["height"])
    except (ValueError, KeyError, IndexError, TypeError):
        raise SystemExit("ffprobe found no video stream in %s" % video)
    start = _fnum(st.get("start_time"))
    if start is None:
        start = _fnum(fmt.get("start_time")) or 0.0
    duration = _fnum(st.get("duration"))
    if duration is None or duration <= 0:
        duration = _fnum(fmt.get("duration"))
    if duration is None or duration <= 0 or width <= 0 or height <= 0:
        raise SystemExit(
            "ffprobe reports no usable duration/size for %s (unfinished "
            "recording? stop the recorder with kill -INT and retry)" % video)
    return {"width": width, "height": height, "duration": duration,
            "start": start}


def probe(video):
    i = probe_info(video)
    return i["width"], i["height"], i["duration"]


def _same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


def compose(video, out, cursor_path, events_path, start_ms, frames_dir=None,
            ring=None, crf=18, preset="veryfast", clip_to_window=True,
            keep_filter=None):
    if not _num(start_ms) or start_ms <= 0:
        raise SystemExit("start_ms must be a positive epoch in ms, got %r"
                         % (start_ms,))
    if _same_file(video, out):
        raise SystemExit("--out and --video are the same file: refusing to "
                         "overwrite the recording")
    info = probe_info(video)
    width, height, duration = info["width"], info["height"], info["duration"]
    cursor_recs, bad_c = read_jsonl(cursor_path)
    event_recs, bad_e = read_jsonl(events_path)
    plan = build_plan(cursor_recs, event_recs, start_ms, duration, width,
                      height, load_frame_ts(frames_dir), ring, clip_to_window)
    plan["stats"]["bad_json_lines"] = bad_c + bad_e
    plan["t0"] = info["start"] if ADD_START_TIME else 0.0
    font = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
    if font is None:
        raise SystemExit("no font found: " + ", ".join(FONT_CANDIDATES))
    set_font(font)
    script = build_filter(plan, font)
    if keep_filter:
        fpath = keep_filter
        with open(fpath, "w", encoding="utf-8") as fh:
            fh.write(script)
    else:
        fd, fpath = tempfile.mkstemp(suffix=".ffscript")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(script)
    # Encode next to the target and rename on success: a failed or killed
    # encode never replaces (or truncates) a previous good output.
    root, ext = os.path.splitext(out)
    part = "%s.part%d%s" % (root, os.getpid(), ext or ".mp4")
    try:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-y", "-i", video, "-filter_script:v", fpath,
               "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
               "-pix_fmt", "yuv420p", "-c:a", "copy",
               part if os.path.isabs(part) else os.path.join(".", part)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("ffmpeg failed rc=%d: %s"
                             % (r.returncode, r.stderr[-2000:]))
        os.replace(part, out)
    finally:
        if not keep_filter:
            os.unlink(fpath)
        if os.path.exists(part):
            os.unlink(part)
    summary = dict(plan["stats"])
    summary.update({"out": out, "width": width, "height": height,
                    "duration_s": round(duration, 3), "ring": plan["ring"],
                    "start_time_s": round(info["start"], 3),
                    "filters": script.count("\n")})
    return plan, summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--video", required=True, help="recording mp4")
    ap.add_argument("--out", required=True, help="composited mp4")
    ap.add_argument("--rt", default=os.environ.get(
        "VP_SWARM_RT", "/dev/shm/vqa-cu/ref"), help="runtime root RT")
    ap.add_argument("--cursor", help="default RT/cursor.jsonl")
    ap.add_argument("--events", help="default RT/events.jsonl")
    ap.add_argument("--frames", help="default RT/frames")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--start-ms", type=float,
                   help="epoch ms of the first recorded frame")
    g.add_argument("--start-file",
                   help="the .start_ms sidecar written by record.sh")
    ap.add_argument("--offset-ms", type=float, default=0.0,
                    help="added to the start epoch: use a positive value "
                         "when the overlay shows late (x11grab start-up lag)")
    ap.add_argument("--ring", help="comma list; default contract order")
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--preset", default="veryfast")
    ap.add_argument("--keep-filter", help="write the filter script here")
    a = ap.parse_args(argv)
    if a.start_file:
        try:
            with open(a.start_file, encoding="utf-8") as fh:
                start_ms = float(fh.read().strip())
        except (OSError, ValueError) as ex:
            raise SystemExit("bad --start-file %s: %s" % (a.start_file, ex))
    else:
        start_ms = a.start_ms
    if not math.isfinite(a.offset_ms):
        raise SystemExit("bad --offset-ms (start offset must be finite)")
    start_ms += a.offset_ms
    _, summary = compose(
        a.video, a.out, a.cursor or os.path.join(a.rt, "cursor.jsonl"),
        a.events or os.path.join(a.rt, "events.jsonl"), start_ms,
        a.frames or os.path.join(a.rt, "frames"),
        a.ring.split(",") if a.ring else None, a.crf, a.preset,
        keep_filter=a.keep_filter)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
