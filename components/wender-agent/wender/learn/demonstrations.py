# @module {"id":"wa.learn.demonstrations","layer":2,"owner":"LEARN","deps":[],"book":"HB-WA-s4-learn"}
"""Episode-private RAM recordings and visual descriptors; no store admission.

Legacy video entry points remain stubs. Recorded events use load_demo/segment_demo.
All PNG parsing is bounded before decompression; no pixels are persisted.
"""
from dataclasses import dataclass, field, fields
from pathlib import Path
import hashlib
import json
import os
import re
import stat
import struct
import time
import zlib
import numpy as np

# @anchor:wa.learn.demonstrations.segment
def segment(video):
    raise NotImplementedError("v0 stub: vision-engine segmentation")


# @anchor:wa.learn.demonstrations.label_intent
def label_intent(segment_frames):
    raise NotImplementedError("v0 stub: multimodal intent labelling")


# @anchor:wa.learn.demonstrations.map_to_actions
def map_to_actions(intent):
    raise NotImplementedError("v0 stub: reasoner -> structured actions")


# @anchor:wa.learn.demonstrations.replay_and_verify
def replay_and_verify(actions, expected):
    raise NotImplementedError("v0 stub: headless replay + verification")


# @anchor:wa.learn.demonstrations.reproduced_share_ppm
def reproduced_share_ppm(segments):
    """Share of segments reproduced WITH a verified result, in parts per million (integer).

    A segment counts only if `verified is True`; reproduced-but-unverified does not count.
    """
    total = len(segments)
    if total == 0:
        return 0
    ok = sum(1 for s in segments if s.get("reproduced") is True and s.get("verified") is True)
    return ok * 1000000 // total


# @anchor:wa.learn.demonstrations.DemoLimits
@dataclass(frozen=True)
class DemoLimits:
    events: int = 500
    event_line: int = 4096
    event_file: int = 512 * 1024
    png_bytes: int = 32 * 1024 * 1024
    axis: int = 4096
    pixels: int = 8_000_000
    retained_bytes: int = 1024 * 1024 * 1024   # full-screen Stitch changes retain ~4.6 MB per step [MEASURED, stitch-s1-demo2]
    memory_bytes: int = 2048 * 1024 * 1024
    candidates: int = 10_000
    comparisons: int = 256_000_000
    acquisitions: int = 40
    operation_ns: int = 900_000_000_000   # pure-Python PNG fallback: 6.7 min for ~180 frames at 1280x900; Pillow: ~4 s [MEASURED]


# @anchor:wa.learn.demonstrations.DemoEvent
@dataclass(frozen=True)
class DemoEvent:
    seq: int
    mono_ns: int
    window_id: str
    width: int
    height: int
    kind: str
    x: object = None
    y: object = None
    button: object = None
    keysym: object = None
    text_len: object = None
    modifiers: tuple = ()
    x2: object = None
    y2: object = None
    direction: object = None
    before_name: object = None
    after_name: object = None

    # @anchor:wa.learn.demonstrations.DemoEvent.__post_init__
    def __post_init__(self):
        object.__setattr__(self, 'modifiers', tuple(self.modifiers))


# @anchor:wa.learn.demonstrations.DemoRecording
@dataclass(frozen=True)
class DemoRecording:
    events: tuple
    root: Path = field(repr=False)
    binding: tuple
    limits: DemoLimits
    files: tuple = field(repr=False)
    root_identity: tuple = field(repr=False)
    final_matches: bool = True

    # @anchor:wa.learn.demonstrations.DemoRecording.__post_init__
    def __post_init__(self):
        object.__setattr__(self, 'files', tuple(tuple(item) for item in self.files))
        for k in ('events', 'binding', 'root_identity'):
            object.__setattr__(self, k, tuple(getattr(self, k)))


# @anchor:wa.learn.demonstrations.TemplateCrop
@dataclass(frozen=True)
class TemplateCrop:
    bbox_xyxy: tuple
    point_offset_xy: tuple
    pixels: np.ndarray = field(repr=False, compare=False)

    # @anchor:wa.learn.demonstrations.TemplateCrop.__post_init__
    def __post_init__(self):
        object.__setattr__(self, 'bbox_xyxy', tuple(self.bbox_xyxy))
        object.__setattr__(self, 'point_offset_xy', tuple(self.point_offset_xy))
        object.__setattr__(self, 'pixels', _owned(self.pixels))


# @anchor:wa.learn.demonstrations.MarkDescriptor
@dataclass(frozen=True)
class MarkDescriptor:
    mark_id: object = None
    box: tuple = ()
    relative_xy: tuple = ()
    missing_reason: object = 'MARKS_UNAVAILABLE'
    kind: object = None

    # @anchor:wa.learn.demonstrations.MarkDescriptor.__post_init__
    def __post_init__(self):
        object.__setattr__(self, 'box', tuple(self.box))
        object.__setattr__(self, 'relative_xy', tuple(tuple(p) for p in self.relative_xy))


# @anchor:wa.learn.demonstrations.TargetDescriptor
@dataclass(frozen=True)
class TargetDescriptor:
    recorded_point: tuple
    small: TemplateCrop
    context: TemplateCrop
    mark: MarkDescriptor = field(default_factory=MarkDescriptor)

    # @anchor:wa.learn.demonstrations.TargetDescriptor.__post_init__
    def __post_init__(self):
        object.__setattr__(self, 'recorded_point', tuple(self.recorded_point))


# @anchor:wa.learn.demonstrations.ExpectedChange
@dataclass(frozen=True)
class ExpectedChange:
    bbox: tuple
    mask: np.ndarray = field(repr=False, compare=False)
    after_crop: np.ndarray = field(repr=False, compare=False)
    missing_reason: object = None
    # The demo's BEFORE pixels in bbox: lets replay tell content that differs from the demo (another
    # generated screen, other code) from the UI reaction it must verify [MEASURED, Stitch S2 run R11-R12].
    before_crop: object = field(default=None, repr=False, compare=False)

    # @anchor:wa.learn.demonstrations.ExpectedChange.__post_init__
    def __post_init__(self):
        object.__setattr__(self, 'bbox', tuple(self.bbox))
        object.__setattr__(self, 'mask', _owned(self.mask))
        object.__setattr__(self, 'after_crop', _owned(self.after_crop))
        if self.before_crop is not None:
            object.__setattr__(self, 'before_crop', _owned(self.before_crop))


# @anchor:wa.learn.demonstrations.DemoStep
@dataclass(frozen=True)
class DemoStep:
    step_id: int
    event: DemoEvent
    target: object
    expected_change: ExpectedChange
    replayable: bool
    refusal_code: object = None


def _owned(a):
    a = np.asarray(a)
    return np.frombuffer(a.tobytes(), dtype=a.dtype).reshape(a.shape)


def _require(ok, code):
    if not ok:
        raise ValueError(code)


def _integer(v, low=0, high=2**63 - 1):
    return type(v) is int and low <= v <= high


def _limits(limits):
    _require(type(limits) is DemoLimits, 'INVALID_LIMITS')
    for f in fields(limits):
        _require(_integer(getattr(limits, f.name), 1, getattr(DemoLimits(), f.name)), 'INVALID_LIMITS')


_KEYS = frozenset(('Return', 'Tab', 'BackSpace', 'Delete', 'Left', 'Right', 'Up', 'Down',
                   'Home', 'End', 'Page_Up', 'Page_Down', 'space', 'Escape', 'Esc', 'escape', 'esc', 'ESC', 'ESCAPE'))
_MODS = frozenset(('ctrl', 'alt', 'shift', 'super'))
_NOISE_KEYS = frozenset(('Num_Lock', 'Caps_Lock', 'Scroll_Lock', 'Shift_L', 'Shift_R', 'Control_L',
                         'Control_R', 'Alt_L', 'Alt_R', 'Super_L', 'Super_R', 'Meta_L', 'Meta_R'))
_ROW_KEYS = frozenset(('seq', 'mono_ns', 'window', 'kind', 'x', 'y', 'button', 'x2', 'y2',
                       'dir', 'keysym', 'text_len', 'modifiers'))


def _frame_name(name, seq, suffix):
    _require(type(name) is str and re.fullmatch(r'[0-9]+_' + suffix + r'\.png', name)
             and int(name.split('_')[0]) == seq, 'FILE_CUSTODY')
    return name


def _adapt_recorded_event(row, limits, dimensions=None):
    # The shipped recorder (OpenSuperficies 181c576) writes SPARSE rows: absent fields mean null.
    if type(row) is dict and 'before' in row:
        _require(set(row) <= _ROW_KEYS | {'before', 'after', 'reason'}, 'CLOSED_SCHEMA')
        row = {**{k: None for k in _ROW_KEYS | {'before', 'after', 'reason'}}, **row}
    _require(type(row) is dict and (row.keys() == _ROW_KEYS or
             row.keys() == _ROW_KEYS | {'before', 'after', 'reason'}), 'CLOSED_SCHEMA')
    shipped = 'reason' in row
    _require(not shipped or row['reason'] is None, 'CLOSED_SCHEMA')
    w = row['window']
    if type(w) is str:
        _require(dimensions is not None, 'FRAME_DIMENSIONS')
        w = {'id': w, 'w': dimensions[1], 'h': dimensions[0]}
    _require(type(w) is dict and w.keys() == {'id', 'w', 'h'}, 'CLOSED_SCHEMA')
    _require(type(w['id']) is str and 0 < len(w['id']) <= 128 and w['id'].isascii(), 'WINDOW_BINDING')
    _require(_integer(w['w'], 1, limits.axis) and _integer(w['h'], 1, limits.axis)
             and w['w'] * w['h'] <= limits.pixels, 'IMAGE_BUDGET')
    e = DemoEvent(row['seq'], row['mono_ns'], w['id'], w['w'], w['h'], row['kind'],
                  row['x'], row['y'], row['button'], row['keysym'], row['text_len'],
                  row['modifiers'], row['x2'], row['y2'], row['dir'],
                  _frame_name(row['before'], row['seq'], 'before') if shipped else None,
                  (_frame_name(row['after'], row['seq'], 'after') if row['after'] is not None else '') if shipped else None)
    _validate_event(e, limits)
    return e


def _validate_event(e, limits):
    _require(type(e) is DemoEvent and _integer(e.seq) and _integer(e.mono_ns), 'EVENT_INTEGER')
    _require(type(e.window_id) is str and 0 < len(e.window_id) <= 128 and e.window_id.isascii(), 'WINDOW_BINDING')
    _require(_integer(e.width, 1, limits.axis) and _integer(e.height, 1, limits.axis)
             and e.width * e.height <= limits.pixels, 'IMAGE_BUDGET')
    for name, suffix in ((e.before_name, 'before'), (e.after_name, 'after')):
        if name not in (None, ''):
            _frame_name(name, e.seq, suffix)
    _require(e.kind in ('click', 'dblclick', 'drag', 'scroll', 'key', 'text'), 'UNSUPPORTED_ACTION')
    _require(type(e.modifiers) is tuple and len(e.modifiers) <= 4
             and all(type(m) is str and m in _MODS for m in e.modifiers)
             and len(set(e.modifiers)) == len(e.modifiers), 'KEY_VOCABULARY')
    if e.kind in ('click', 'dblclick', 'drag', 'scroll'):
        _require(_integer(e.x, 0, e.width - 1) and _integer(e.y, 0, e.height - 1), 'POINT_INTEGER')
    else:
        _require(e.x is None and e.y is None and e.button is None, 'CLOSED_SCHEMA')
    if e.kind in ('click', 'dblclick', 'drag'):
        _require(_integer(e.button, 1, 3), 'CLOSED_SCHEMA')
    else:
        _require(e.button is None, 'CLOSED_SCHEMA')
    if e.kind == 'drag':
        _require(_integer(e.x2, 0, e.width - 1) and _integer(e.y2, 0, e.height - 1), 'POINT_INTEGER')
    else:
        _require(e.x2 is None and e.y2 is None, 'CLOSED_SCHEMA')
    _require(e.direction in ('up', 'down', 'left', 'right') if e.kind == 'scroll'
             else e.direction is None, 'CLOSED_SCHEMA')
    if e.kind == 'key':
        _require(type(e.keysym) is str and (e.keysym in _KEYS or
                 (len(e.keysym) == 1 and e.keysym.isascii() and e.keysym.isalnum()
                  and any(m in ('ctrl', 'alt') for m in e.modifiers))), 'KEY_VOCABULARY')
    else:
        _require(e.keysym is None, 'CLOSED_SCHEMA')
    _require(_integer(e.text_len, 1, 4096) if e.kind == 'text' else e.text_len is None, 'TEXT_LENGTH')


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        _require(k not in out, 'CLOSED_SCHEMA')
        out[k] = v
    return out


def _json(data):
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=_pairs,
                          parse_constant=lambda _: _require(False, 'CLOSED_SCHEMA'))
    except (UnicodeError, TypeError, json.JSONDecodeError):
        raise ValueError('CLOSED_SCHEMA') from None


def _directory(path):
    """Open each directory component without following symlinks, including parents."""
    fd = None
    try:
        p = Path(path).absolute()
        _require(p == p.resolve(), 'FILE_CUSTODY')
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in p.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        result, fd = fd, None
        return result
    except OSError:
        raise ValueError('FILE_CUSTODY') from None
    finally:
        if fd is not None:
            os.close(fd)


def _ram_fd(fd):
    """Match the opened FD's kernel mount ID and device, never a path prefix."""
    try:
        info = Path(f'/proc/self/fdinfo/{fd}').read_text()
        ids = [line.split()[1] for line in info.splitlines() if line.startswith('mnt_id:')]
        _require(len(ids) == 1, 'RAM_REQUIRED')
        device = os.fstat(fd).st_dev
        mounts = Path('/proc/self/mountinfo').read_text().splitlines()
        matches = [line.split() for line in mounts if line.split()[0] == ids[0]]
        _require(len(matches) == 1, 'RAM_REQUIRED')
        row = matches[0]
        major, minor = map(int, row[2].split(':'))
        _require((major, minor) == (os.major(device), os.minor(device))
                 and row[row.index('-') + 1] in ('tmpfs', 'ramfs'), 'RAM_REQUIRED')
    except (OSError, IndexError, TypeError):
        raise ValueError('RAM_REQUIRED') from None


def _root(directory):
    p = Path(directory).absolute()
    _require(p == p.resolve() and p.is_relative_to('/dev/shm/vqa-ramws') and p.is_dir(), 'FILE_CUSTODY')
    fd = _directory(p)
    try:
        _ram_fd(fd)
        return p, fd
    except Exception:
        os.close(fd)
        raise


def _read(fd, name, bound, memory_bytes=None):
    _require(type(name) is str and name == Path(name).name and name not in ('', '.', '..'), 'FILE_CUSTODY')
    try:
        f = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            a = os.fstat(f)
            _require(stat.S_ISREG(a.st_mode) and 0 < a.st_size <= bound, 'FILE_BUDGET')
            if memory_bytes is not None:
                _require(a.st_size * 4 <= memory_bytes, 'MEMORY_BUDGET')
            chunks = bytearray()
            while len(chunks) <= a.st_size:
                part = os.read(f, min(65536, a.st_size + 1 - len(chunks)))
                if not part:
                    break
                chunks.extend(part)
            b = os.fstat(f)
            _require((a.st_dev, a.st_ino, a.st_size, a.st_mtime_ns, a.st_ctime_ns) ==
                     (b.st_dev, b.st_ino, b.st_size, b.st_mtime_ns, b.st_ctime_ns)
                     and len(chunks) == a.st_size, 'FILE_CHANGED')
            return bytes(chunks)
        finally:
            os.close(f)
    except OSError:
        raise ValueError('FILE_CUSTODY') from None


def _png(data, limits, *, deadline_ns=None):
    deadline_ns = deadline_ns if deadline_ns is not None else time.monotonic_ns() + limits.operation_ns
    _require(len(data) <= limits.png_bytes and data[:8] == b'\x89PNG\r\n\x1a\n', 'PNG_FORMAT')
    pos, compressed, ended, idat_closed = 8, bytearray(), False, False
    width = height = channels = None
    while pos < len(data):
        _require(time.monotonic_ns() < deadline_ns, 'DEADLINE')
        _require(pos + 12 <= len(data), 'PNG_FORMAT')
        n = struct.unpack_from('>I', data, pos)[0]
        _require(n <= limits.png_bytes and pos + n + 12 <= len(data), 'PNG_FORMAT')
        kind, payload = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        crc = struct.unpack_from('>I', data, pos + 8 + n)[0]
        _require(zlib.crc32(kind + payload) & 0xffffffff == crc, 'PNG_CRC')
        if width is None:
            _require(kind == b'IHDR' and n == 13, 'PNG_FORMAT')
            width, height, depth, color, comp, filt, inter = struct.unpack('>IIBBBBB', payload)
            _require(0 < width <= limits.axis and 0 < height <= limits.axis
                     and width * height <= limits.pixels, 'IMAGE_BUDGET')
            _require(depth == 8 and color in (2, 6) and (comp, filt, inter) == (0, 0, 0), 'PNG_UNSUPPORTED')
            channels = 3 if color == 2 else 4
            _require(width * height * 64 + len(data) * 4 <= limits.memory_bytes, 'MEMORY_BUDGET')
        elif kind == b'IDAT':
            _require(not idat_closed, 'PNG_FORMAT')
            compressed.extend(payload)
        elif kind == b'IEND':
            _require(n == 0 and compressed and pos + 12 == len(data), 'PNG_FORMAT')
            ended = True
            break
        else:
            _require(kind != b'IHDR' and kind[0] & 32 != 0, 'PNG_UNSUPPORTED')
            if compressed:
                idat_closed = True
        pos += n + 12
    _require(ended, 'PNG_FORMAT')
    stride = width * channels
    size = height * (stride + 1)
    try:
        d = zlib.decompressobj()
        raw = d.decompress(compressed, size + 1)
        _require(len(raw) == size and d.eof and not d.unused_data and not d.unconsumed_tail, 'PNG_DECOMPRESSION')
    except zlib.error:
        raise ValueError('PNG_DECOMPRESSION') from None
    _require(int(np.frombuffer(raw, dtype=np.uint8)[::stride + 1].max()) <= 4, 'PNG_FILTER')
    fast = _fast_unfilter(data, width, height, channels)
    if fast is not None:
        return fast
    out = np.empty((height, stride), dtype=np.uint8)
    for y in range(height):
        _require(time.monotonic_ns() < deadline_ns, 'DEADLINE')
        f = raw[y * (stride + 1)]
        _require(f <= 4, 'PNG_FILTER')
        row = np.frombuffer(raw, dtype=np.uint8, count=stride, offset=y * (stride + 1) + 1)
        if f == 0:
            out[y] = row
            continue
        for x in range(stride):
            a = int(out[y, x - channels]) if x >= channels else 0
            b = int(out[y - 1, x]) if y else 0
            c = int(out[y - 1, x - channels]) if y and x >= channels else 0
            if f == 0:
                predictor = 0
            elif f == 1:
                predictor = a
            elif f == 2:
                predictor = b
            elif f == 3:
                predictor = (a + b) // 2
            else:
                p = a + b - c
                distances = (abs(p - a), abs(p - b), abs(p - c))
                predictor = (a, b, c)[distances.index(min(distances))]
            out[y, x] = (int(row[x]) + predictor) & 255
    return out.reshape(height, width, channels)[:, :, :3].copy()


def _fast_unfilter(data, width, height, channels):
    # Already-validated PNG (chunks, CRC, bounded inflate, filter bytes): unfilter natively when Pillow is
    # installed. The pure-Python fallback below took 6.7 min for ~180 Paeth frames at 1280x900 [MEASURED].
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            if im.size != (width, height) or im.mode != ('RGB' if channels == 3 else 'RGBA'):
                return None
            return np.asarray(im, dtype=np.uint8)[:, :, :3].copy()
    except Exception:
        return None


def _frame(fd, name, limits, binding, *, deadline_ns=None):
    data = _read(fd, name, limits.png_bytes, limits.memory_bytes)
    pixels = _png(data, limits, deadline_ns=deadline_ns)
    _require(pixels.shape[:2] == (binding[2], binding[1]), 'FRAME_DIMENSIONS')
    return pixels, hashlib.sha256(data).digest()


# @anchor:wa.learn.demonstrations.load_demo
def load_demo(directory, *, limits, final_check='strict'):
    _limits(limits)
    _require(final_check in ('strict', 'informative'), 'INVALID_LIMITS')
    start = time.monotonic_ns()
    p, fd = _root(directory)
    try:
        data = _read(fd, 'events.jsonl', limits.event_file, limits.memory_bytes)
        count = data.count(b'\n') + (0 if data.endswith(b'\n') else 1)
        _require(0 < count <= limits.events + 1, 'EVENT_BUDGET')
        _require(len(data) * 4 + count * 4096 <= limits.memory_bytes, 'MEMORY_BUDGET')
        rows = data.split(b'\n')
        if rows[-1] == b'':
            rows.pop()
        _require(0 < len(rows) <= limits.events + 1, 'EVENT_BUDGET')
        _require(all(0 < len(raw) <= limits.event_line for raw in rows), 'EVENT_BUDGET')
        rows = tuple(_json(raw) for raw in rows)
        _require(all(type(row) is dict for row in rows), 'CLOSED_SCHEMA')
        _require(sum(row.get('kind') != 'stop' for row in rows) <= limits.events, 'EVENT_BUDGET')
        events, files = [], []
        binding = previous = last = None
        last_after_available = False
        stopped = False
        full = _ROW_KEYS | {'before', 'after', 'reason'}
        for row in rows:
            # Shipped recorder rows are SPARSE (absent = null); the stop row carries window ''.
            if type(row) is dict and set(row) <= full and ('before' in row or row.get('kind') == 'stop'):
                row = {**{k: None for k in full}, **row}
                if row['kind'] == 'stop' and row['window'] == '':
                    row['window'] = None
            _require(type(row) is dict and (row.keys() == _ROW_KEYS or
                     row.keys() == _ROW_KEYS | {'before', 'after', 'reason'}), 'CLOSED_SCHEMA')
            _require(not stopped, 'RECORDING_ORDER')
            _require(_integer(row['seq']) and _integer(row['mono_ns']), 'EVENT_INTEGER')
            _require(previous is None or (row['seq'] > previous[0] and row['mono_ns'] >= previous[1]), 'RECORDING_ORDER')
            previous = (row['seq'], row['mono_ns'])
            if row['kind'] == 'key' and row.get('keysym') in _NOISE_KEYS:
                continue   # lock/modifier-only keys carry no intent (operator Num_Lock toggles) [MEASURED]
            if row['kind'] == 'stop':
                _require('reason' in row and row['reason'] in ('operator', 'limit', 'recorder_exited'), 'CLOSED_SCHEMA')
                _require(all(row[k] is None for k in ('x', 'y', 'button', 'x2', 'y2', 'dir', 'keysym', 'text_len', 'before', 'after'))
                         and row['modifiers'] == [], 'CLOSED_SCHEMA')
                _require(binding is not None and row['window'] in (None, binding[0],
                         {'id': binding[0], 'w': binding[1], 'h': binding[2]}), 'WINDOW_BINDING')
                stopped = True
                continue
            dimensions = None
            if type(row['window']) is str:
                name = _frame_name(row.get('before', f"{row['seq']}_before.png"), row['seq'], 'before')
                dimensions = _png(_read(fd, name, limits.png_bytes, limits.memory_bytes), limits,
                                  deadline_ns=start + limits.operation_ns).shape[:2]
            try:
                e = _adapt_recorded_event(row, limits, dimensions)
            except (TypeError, OverflowError):
                raise ValueError('CLOSED_SCHEMA') from None
            current = (e.window_id, e.width, e.height)
            binding = binding or current
            _require(current == binding, 'WINDOW_BINDING')
            events.append(e)
            last_after_available = False
            for suffix, supplied in (('before', e.before_name), ('after', e.after_name)):
                _require(time.monotonic_ns() - start < limits.operation_ns, 'DEADLINE')
                name = supplied if supplied is not None else f'{e.seq}_{suffix}.png'
                if suffix == 'after' and e.before_name is not None:
                    if not name:
                        continue
                    try:
                        os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                last, digest = _frame(fd, name, limits, binding, deadline_ns=start + limits.operation_ns)
                files.append((name, digest))
                last_after_available = suffix == 'after'
        _require(bool(events), 'EVENT_BUDGET')
        final_name = 'final.png'
        try:
            os.stat(final_name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            _require(re.fullmatch(r'w[0-9]+', binding[0]) is not None, 'FILE_CUSTODY')
            final_name = f'final_{binding[0]}.png'
        final, digest = _frame(fd, final_name, limits, binding, deadline_ns=start + limits.operation_ns)
        # Live demos: the operator may act after the last recorded event (dialogs, downloads), so 'informative'
        # keeps the recording and only reports the mismatch [MEASURED, stitch-s1-demo2].
        matches = not last_after_available or bool(np.array_equal(last, final))
        _require(matches or final_check == 'informative', 'FINAL_MISMATCH')
        files.append((final_name, digest))
        st = os.fstat(fd)
        return DemoRecording(tuple(events), p, binding, limits, tuple(files), (st.st_dev, st.st_ino), matches)
    finally:
        os.close(fd)


def _crop(rgb, x, y, w, h):
    x0, y0 = max(0, x - w // 2), max(0, y - h // 2)
    x1, y1 = min(rgb.shape[1], x + w // 2), min(rgb.shape[0], y + h // 2)
    return TemplateCrop((x0, y0, x1, y1), (x - x0, y - y0), rgb[y0:y1, x0:x1])


def _mark(description, x, y):
    matches = []
    for m in description.get('marks', ()):
        if (type(m) is dict and all(_integer(m.get(k), 0 if k in ('x', 'y') else 1) for k in ('x', 'y', 'w', 'h'))
                and m['x'] <= x < m['x'] + m['w'] and m['y'] <= y < m['y'] + m['h']):
            matches.append(m)
    if len(matches) != 1:
        return MarkDescriptor(missing_reason='MARK_AMBIGUOUS' if matches else 'MARKS_UNAVAILABLE')
    m = matches[0]
    # IDs are structural metadata only; raw text never enters descriptors.
    mid = m.get('id') if _integer(m.get('id')) else None
    return MarkDescriptor(mid, (m['x'], m['y'], m['w'], m['h']),
                          ((x - m['x'], m['w']), (y - m['y'], m['h'])), None,
                          m.get('kind') if type(m.get('kind')) is str and m['kind'] in _MARK_KINDS else None)


# @anchor:wa.learn.demonstrations.segment_demo
def segment_demo(recording, *, describer=None):
    _require(type(recording) is DemoRecording, 'INVALID_RECORDING')
    limits = recording.limits
    _limits(limits)
    start = time.monotonic_ns()
    p, fd = _root(recording.root)
    try:
        st = os.fstat(fd)
        _require((st.st_dev, st.st_ino) == recording.root_identity, 'FILE_CHANGED')
        _require(0 < len(recording.events) <= limits.events, 'EVENT_BUDGET')
        hashes = dict(recording.files)
        steps, retained = [], 0
        previous = None
        for e in recording.events:
            _validate_event(e, limits)
            _require((e.window_id, e.width, e.height) == recording.binding, 'WINDOW_BINDING')
            _require(previous is None or (e.seq > previous.seq and e.mono_ns >= previous.mono_ns), 'RECORDING_ORDER')
            previous = e
            _require(e.width * e.height * 64 + retained + limits.png_bytes * 4 <= limits.memory_bytes, 'MEMORY_BUDGET')
            before_name = e.before_name if e.before_name is not None else f'{e.seq}_before.png'
            after_name = e.after_name if e.after_name is not None else f'{e.seq}_after.png'
            before, bh = _frame(fd, before_name, limits, recording.binding, deadline_ns=start + limits.operation_ns)
            _require(hashes.get(before_name) == bh, 'FILE_CHANGED')
            after = None
            if after_name in hashes:
                after, ah = _frame(fd, after_name, limits, recording.binding, deadline_ns=start + limits.operation_ns)
                _require(hashes[after_name] == ah, 'FILE_CHANGED')
            else:
                _require(e.before_name is not None, 'FILE_CHANGED')
            # Reserve worst-case descriptor copies before allocating a mask/crop.
            reserve = e.width * e.height * 8 + 2 * (64 * 40 + 160 * 96) * 3
            _require(retained + reserve <= limits.retained_bytes, 'RETAINED_BUDGET')
            target = None
            if e.kind in ('click', 'dblclick', 'drag', 'scroll'):
                mark = MarkDescriptor()
                if describer is not None:
                    try:
                        desc = describer.describe({'path': str(p / before_name)}, prev=None)
                        mark = _mark(desc, e.x, e.y)
                    except Exception:
                        mark = MarkDescriptor(missing_reason='DESCRIBE_UNAVAILABLE')
                target = TargetDescriptor((e.x, e.y), _crop(before, e.x, e.y, 64, 40),
                                          _crop(before, e.x, e.y, 160, 96), mark)
            changed = (np.any(np.abs(after.astype(np.int16) - before.astype(np.int16)) > 12, axis=2)
                       if after is not None else np.zeros(before.shape[:2], dtype=bool))
            ys, xs = np.nonzero(changed)
            if len(xs):
                box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
                x0, y0, x1, y1 = box
                expected = ExpectedChange(box, changed[y0:y1, x0:x1], after[y0:y1, x0:x1], None, before[y0:y1, x0:x1])
            else:
                expected = ExpectedChange((), np.empty((0, 0), bool), np.empty((0, 0, 3), np.uint8), 'CHANGE_UNOBSERVABLE')
            retained += expected.mask.nbytes + expected.after_crop.nbytes + (
                expected.before_crop.nbytes if expected.before_crop is not None else 0)
            if target:
                retained += target.small.pixels.nbytes + target.context.pixels.nbytes
            # Scroll/drag effects often land in the NEXT before frame (capture lag) [MEASURED, stitch-s1-demo2]: replay verifies motion instead.
            refusal = (None if e.kind in ('scroll', 'drag') else expected.missing_reason
                       or ('UNSUPPORTED_ACTION' if e.kind not in ('click', 'key', 'text') else None))
            steps.append(DemoStep(e.seq, e, target, expected, refusal is None, refusal))
            _require(time.monotonic_ns() - start < limits.operation_ns, 'DEADLINE')
        final_name = recording.files[-1][0]
        _require(final_name in ('final.png', f'final_{recording.binding[0]}.png'), 'FILE_CUSTODY')
        final, fh = _frame(fd, final_name, limits, recording.binding, deadline_ns=start + limits.operation_ns)
        _require(hashes.get(final_name) == fh and (after is None or not recording.final_matches or np.array_equal(final, after)), 'FINAL_MISMATCH')
        return tuple(steps)
    finally:
        os.close(fd)


_MARK_KINDS = frozenset(('button', 'tab', 'field', 'row', 'icon', 'text', 'unknown'))
_OMISSIONS = frozenset(('MARKS_UNAVAILABLE', 'MARK_AMBIGUOUS', 'DESCRIBE_UNAVAILABLE',
                        'OCR_UNAVAILABLE', 'MARK_KIND_UNAVAILABLE', 'DRAG_ENDPOINT_OMITTED',
                        'SCROLL_DIRECTION_OMITTED', 'CHANGE_UNOBSERVABLE'))


# @anchor:wa.learn.demonstrations.validate_text_slot
def validate_text_slot(value, *, expected_length, max_bytes):
    """Review Unicode without normalizing or exposing caller-owned content."""
    _require(type(value) is str, 'TEXT_SLOT')
    _require(expected_length is None or (_integer(expected_length) and len(value) == expected_length), 'TEXT_SLOT')
    _require(_integer(max_bytes, 1, 4096), 'TEXT_BUDGET')
    _require(all((ord(c) >= 32 or c in '\n\t') and not 127 <= ord(c) <= 159
                 and not 0xD800 <= ord(c) <= 0xDFFF and not 0x202A <= ord(c) <= 0x202E
                 and not 0x2066 <= ord(c) <= 0x2069 and ord(c) != 0x061C
                 and ord(c) not in (0x200E, 0x200F) for c in value), 'TEXT_ENCODING')
    # Bound before allocating an encoded copy, then apply the actual UTF-8 byte cap.
    _require(len(value) <= max_bytes, 'TEXT_BUDGET')
    payload = value.encode('utf-8', errors='strict')
    _require(len(payload) <= max_bytes, 'TEXT_BUDGET')
    return payload


# @anchor:wa.learn.demonstrations.DemoProjection
@dataclass(frozen=True)
class DemoProjection:
    payload: bytes
    text_slots: tuple
    at_recorded: tuple
    omissions: tuple

    # @anchor:wa.learn.demonstrations.DemoProjection.document
    @property
    def document(self):
        """Return a fresh dictionary; callers cannot mutate the normative payload."""
        return json.loads(self.payload)


def _export_id(value):
    _require(type(value) is str and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', value) is not None, 'EXPORT_ID')


def _export_box(value, length):
    _require(type(value) is tuple and len(value) == length
             and all(_integer(v, -2147483648, 2147483647) for v in value), 'EXPORT_INTEGER')
    return list(value)


def _symbolic_value(value):
    if value is None or type(value) in (str, int):
        return True
    if type(value) is list:
        return all(_symbolic_value(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _symbolic_value(item) for key, item in value.items())
    return False


def _export_payload(document):
    # Called only on the fresh allowlist projection, never on detector dictionaries.
    _require(_symbolic_value(document), 'EXPORT_INTEGER')
    payload = (json.dumps(document, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    _require(len(payload) <= 65_536, 'EXPORT_BUDGET')
    return payload


# @anchor:wa.learn.demonstrations.project_demo_steps
def project_demo_steps(steps, *, demo_id, site_id, at_recorded=frozenset()):
    """Closed D2 symbolic evidence; no pixels, caller text or relocation authority."""
    _export_id(demo_id)
    _export_id(site_id)
    _require(type(steps) in (tuple, list) and 0 < len(steps) <= DemoLimits().events, 'EVENT_BUDGET')
    _require(type(at_recorded) in (set, frozenset, tuple) and all(_integer(k, 1) for k in at_recorded), 'SELECTION')
    # Reserve per-step dictionaries, local metadata and serialization buffers before allocation.
    _require(len(steps) * 8192 + 65_536 * 4 <= DemoLimits().memory_bytes, 'MEMORY_BUDGET')
    rows, slots, omissions = [], [], []
    previous = None
    for step in steps:
        _require(type(step) is DemoStep and type(step.event) is DemoEvent, 'INVALID_STEP')
        e = step.event
        _validate_event(e, DemoLimits())
        _require(_integer(step.step_id, 1) and step.step_id == e.seq, 'EXPORT_INTEGER')
        _require(previous is None or (e.seq > previous.seq and e.mono_ns >= previous.mono_ns), 'RECORDING_ORDER')
        gap = 0 if previous is None else (e.mono_ns - previous.mono_ns) // 1_000_000
        if rows:
            rows[-1]['gap_after_ms'] = gap
        kind = {'dblclick': 'double_click', 'text': 'paste'}.get(e.kind, e.kind)
        if e.kind == 'click' and e.button == 3:
            kind = 'right_click'
        point = None if e.x is None else _export_box((e.x, e.y), 2)
        change = step.expected_change
        _require(type(change) is ExpectedChange, 'INVALID_DESCRIPTOR')
        box = None
        if change.missing_reason is None:
            box = _export_box(change.bbox, 4)
            _require(0 <= box[0] < box[2] <= e.width and 0 <= box[1] < box[3] <= e.height, 'CROP_GEOMETRY')
        else:
            _require(change.missing_reason == 'CHANGE_UNOBSERVABLE', 'INVALID_DESCRIPTOR')
            omissions.append((e.seq, 'CHANGE_UNOBSERVABLE'))
        mark = None
        if step.target is not None:
            _require(type(step.target) is TargetDescriptor and step.target.recorded_point == (e.x, e.y), 'INVALID_DESCRIPTOR')
            m = step.target.mark
            _require(type(m) is MarkDescriptor, 'INVALID_DESCRIPTOR')
            if m.missing_reason is None and m.kind in _MARK_KINDS and _integer(m.mark_id):
                mb = _export_box(m.box, 4)
                _require(mb[2] > 0 and mb[3] > 0 and 0 <= mb[0] <= e.x < mb[0] + mb[2] <= e.width
                         and 0 <= mb[1] <= e.y < mb[1] + mb[3] <= e.height, 'INVALID_DESCRIPTOR')
                _require(m.relative_xy == ((e.x - mb[0], mb[2]), (e.y - mb[1], mb[3]))
                         and all(type(n) is int and type(den) is int and 0 <= n < den for n, den in m.relative_xy), 'EXPORT_INTEGER')
                # D2 boxes are [x0, y0, x1, y1]; vision-engine marks are [x, y, w, h] (memoryweb_gate GEOMETRY) [MEASURED, demo2].
                mark = dict(mark_id=str(m.mark_id), kind=m.kind, box_px=[mb[0], mb[1], mb[0] + mb[2], mb[1] + mb[3]],
                            relative_xy_bps=[n * 10000 // den for n, den in m.relative_xy],
                            ocr_text=None, ocr_score_promille=None, missing_reason='OCR_UNAVAILABLE')
                omissions.append((e.seq, 'OCR_UNAVAILABLE'))
            else:
                reason = m.missing_reason or ('MARK_KIND_UNAVAILABLE' if m.kind not in _MARK_KINDS else 'MARKS_UNAVAILABLE')
                _require(type(reason) is str and reason in _OMISSIONS, 'INVALID_DESCRIPTOR')
                omissions.append((e.seq, reason))
        else:
            omissions.append((e.seq, 'MARKS_UNAVAILABLE'))
        if e.kind == 'text' or (e.kind == 'key' and e.keysym in ('v', 'V') and e.modifiers == ('ctrl',)):
            slots.append((e.seq, f'slot-{e.seq}'))
        if e.kind in ('scroll', 'drag'):
            omissions.append((e.seq, 'SCROLL_DIRECTION_OMITTED' if e.kind == 'scroll' else 'DRAG_ENDPOINT_OMITTED'))
        rows.append(dict(seq=e.seq, kind=kind, button=e.button, recorded_point_px=point,
                         modifiers=list(e.modifiers), key_name=e.keysym, text_len=e.text_len,
                         gap_before_ms=gap, gap_after_ms=0, expected_change_box_px=box, mark=mark))
        previous = e
    _require(all(any(s.step_id == k and s.event.kind == 'click' for s in steps) for k in at_recorded), 'SELECTION')
    # D2 demo_steps.seq is contiguous 1..n (memoryweb_gate STEP_SEQUENCE); recorder seqs (gaps from filtered noise keys)
    # stay in the projection's text_slots/at_recorded/omissions only [MEASURED, demo2].
    for n, row in enumerate(rows, 1):
        row['seq'] = n
    document = dict(schema='mw.demo_steps.v1', demo_id=demo_id, site_id=site_id, steps=rows)
    payload = _export_payload(document)
    return DemoProjection(payload, tuple(slots), tuple(sorted(at_recorded)), tuple(omissions))
