# @module {"id":"va.cu_ocr","layer":4,"owner":"ARCH","deps":["vc.decode","va.cu_marks"],"book":"HB-VC-s2-change"}
"""Opt-in, RAM-only OCR evidence with deterministic text-line fallback.

A bounded readiness probe precedes one full-frame OCR process, never one per
mark. Scores are scaled integer evidence, independent of the original rim score.
"""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import unicodedata

import numpy as np
from eval.v2x.gm_textline import _segment_lines, _hash_line_64, _TextLine
from eval.v2x.st_edgemap import _compute_edge_map
from eval.p1_region_nodes import _region_to_closed_box, _overlap_area
from visioncore.decode import encode_png

_TSV_HEADER = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext'


def sanitize_ocr_text(raw, max_bytes=64):  # @anchor:va.cu_ocr.sanitize_ocr_text
    """Normalize, escape and bound external text to 64 UTF-8 bytes."""
    text = re.sub(r'\s+', ' ', unicodedata.normalize('NFC', str(raw))).strip()
    text = text.replace('\\', '\\\\').replace('|', '\\|')
    text = text.encode('utf-8')[:max_bytes].decode('utf-8', errors='ignore').rstrip()
    # An odd trailing escape count means truncation split an escape pair.
    if (len(text) - len(text.rstrip('\\'))) % 2:
        text = text[:-1]
    return text or None


def conf_to_promille(conf_str):  # @anchor:va.cu_ocr.conf_to_promille
    """Return exact floor(10 * TSV confidence), or None for missing evidence."""
    text = conf_str.strip()
    if not text or text.startswith('-'):
        return None
    match = re.fullmatch(r'([0-9]+)(?:\.([0-9]+))?', text)
    if match is None:
        raise ValueError('invalid conf')
    return min(1000, int(match[1]) * 10 + int((match[2] or '0')[0]))


def parse_conf_promille(conf_str):  # @anchor:va.cu_ocr.parse_conf_promille
    """Parse decimal OCR evidence and preserve the negative sentinel."""
    text = conf_str.strip()
    if text.startswith('-'):
        return None, True
    if not text:
        raise ValueError('invalid TSV conf')
    return conf_to_promille(text), False


def preflight(tessdata_dir=None, lang='eng', binary=None, timeout_s=2):  # @anchor:va.cu_ocr.preflight
    """Admit OCR only with executable, readable language data and listed language."""
    result = dict(ok=False, missing_reason='tessdata_absent', binary=None,
                  tessdata_dir=None, lang=lang)
    try:
        bin_path = binary or shutil.which('tesseract') or None
        if not os.path.isfile(bin_path) or not os.access(bin_path, os.X_OK):
            return result
        result['binary'] = str(Path(bin_path).resolve())
        result['missing_reason'] = 'tessdata_absent'
        td = Path(tessdata_dir if tessdata_dir is not None else
                  (os.environ.get('TESSDATA_PREFIX', '').strip() or
                   '/usr/share/tesseract-ocr/5/tessdata')).resolve()
        result.update(tessdata_dir=str(td), missing_reason='tessdata_absent')
        data = td / (lang + '.traineddata')
        if not (td.is_dir() and os.access(td, os.R_OK | os.X_OK) and
                data.is_file() and os.access(data, os.R_OK) and data.stat().st_size > 0):
            return result
        result['missing_reason'] = 'tessdata_absent'
        proc = subprocess.run([result['binary'], '--tessdata-dir', str(td), '--list-langs'],
                              env=dict(os.environ, TESSDATA_PREFIX=str(td)),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=min(timeout_s, 5))
        if proc.returncode != 0 or lang not in [line.strip() for line in proc.stdout.splitlines()[1:]]:
            result['missing_reason'] = 'tessdata_absent'
            return result
        result.update(ok=True, missing_reason=None)
    except subprocess.SubprocessError:
        result.update(ok=False, missing_reason='tessdata_absent')
    except (OSError, ValueError):
        result['ok'] = False
    return result


def parse_tsv(tsv_text, max_rows=4096):  # @anchor:va.cu_ocr.parse_tsv
    """Parse bounded TSV word evidence; geometry uses R10 closed coordinates."""
    rows = iter(tsv_text.splitlines())
    if next(rows, None) != _TSV_HEADER:
        raise ValueError('invalid tesseract TSV header')
    words = []
    row_idx = 0
    for row in rows:
        if not row:
            continue
        if row_idx >= min(max_rows, 4096):
            break
        row_idx += 1
        cols = row.split('\t', 11)
        if len(cols) != 12:
            raise ValueError('ragged TSV row')
        level, page, block, par, line, num, x, y, w, h = map(int, cols[:10])
        if level != 5 or x < 0 or y < 0 or w <= 0 or h <= 0:
            continue
        conf, sentinel = parse_conf_promille(cols[10])
        if sentinel:
            continue
        text = sanitize_ocr_text(cols[11])
        if text is None:
            continue
        box = _region_to_closed_box((x, y, w, h))
        words.append(dict(text=text, raw_text=cols[11].strip(), box=box, box_closed=list(box),
                          bbox_px=[x, y, w, h], conf_promille=conf, raw_conf=cols[10],
                          line_key=[page, block, par, line], word_num=num, row_idx=row_idx))
    return words


def _planes(rgb):
    a = rgb.astype(np.int16)
    co = a[..., 0] - a[..., 2]
    temp = a[..., 2] + (co >> 1)
    luma = temp + ((a[..., 1] - temp) >> 1)
    return luma, _compute_edge_map(luma)


def fallback_mark_lines(rgb, mark, luma=None, edge=None):  # @anchor:va.cu_ocr.fallback_mark_lines
    """Reuse crop-relative gm_textline evidence and integer rim classification."""
    if luma is None or edge is None:
        luma, edge = _planes(rgb)
    x, y, w, h = (mark[k] for k in ('x', 'y', 'w', 'h'))
    crop = luma[y:y+h, x:x+w]
    ce = edge[y:y+h, x:x+w]
    raw = _segment_lines(crop, edge_thresh=5)
    if not raw and w >= 4 and h >= 6:
        counts = np.sum(np.abs(crop[:, 1:].astype(np.int16) -
                              crop[:, :-1].astype(np.int16)) > 5, axis=1)
        maximum = int(np.max(counts)) if len(counts) else 0
        if maximum >= max(3, (w - 1) // 4):
            start = None
            for row in range(h + 1):
                active = row < h and counts[row] >= max(3, maximum // 3)
                if active and start is None:
                    start = row
                elif not active and start is not None:
                    raw.append(_TextLine(start, row - start, _hash_line_64(counts[start:row])))
                    start = None
    raw = raw[:8]
    rim = (np.count_nonzero(np.any(ce[:2, :], axis=0)) +
           np.count_nonzero(np.any(ce[max(0, h-3):, :], axis=0)) +
           np.count_nonzero(np.any(ce[:, :2], axis=1)) +
           np.count_nonzero(np.any(ce[:, max(0, w-3):], axis=1)))
    rim_promille = min(1000, 1000 * int(rim) // (2 * (w + h)))
    n = len(raw)
    if n >= 1 and rim_promille < 500:
        kind = 'textline'
    elif rim_promille >= 500 and w >= 4*h:
        kind = 'pill'
    elif w <= 48 and h <= 48 and rim_promille >= 500 and n == 0:
        kind = 'icon_btn'
    else:
        kind = 'canvas_ctrl'
    return dict(visual_kind=kind, n_lines=n, line_hash=int(raw[0].line_hash) if raw else 0,
                lines=[dict(y0=int(t.start_row), h=int(t.height), line_hash=int(t.line_hash))
                       for t in raw[:8]])


def words_for_marks(words, marks, rgb, nameless_ids=None):  # @anchor:va.cu_ocr.words_for_marks
    """Attach OCR tokens to their winning mark in TSV reading order."""
    luma, edge = _planes(rgb)
    output = []
    for mark in marks:
        record = dict(mark, **fallback_mark_lines(rgb, mark, luma, edge),
                      ocr_text=None, ocr_score_promille=None, missing_reason='ocr_empty')
        if nameless_ids is not None and mark['id'] not in nameless_ids:
            record['missing_reason'] = 'named_by_ax'
        else:
            x, y, w, h = (mark[k] for k in ('x', 'y', 'w', 'h'))
            matches = []
            for word in words:
                x0, y0, x1, y1 = word['box']
                area = _overlap_area(x, y, x+w-1, y+h-1, x0, y0, x1, y1)
                eligible = [m for m in marks if nameless_ids is None or m['id'] in nameless_ids]
                def rank(m):
                    mx, my, mw, mh = (m[k] for k in ('x', 'y', 'w', 'h'))
                    overlap = _overlap_area(mx, my, mx+mw-1, my+mh-1, x0, y0, x1, y1)
                    return -overlap, mw*mh, m['id']
                if area > 0 and min(eligible, key=rank)['id'] == mark['id']:
                    matches.append((word, area))
            matches.sort(key=lambda pair: (pair[0]['line_key'], pair[0]['word_num'], pair[0]['box'][1], pair[0]['box'][0]))
            if matches:
                # Sanitize raw tokens once, at the joined wire boundary.
                text = sanitize_ocr_text(' '.join(word['raw_text'] for word, _ in matches))
                if text is not None:
                    score = sum(word['conf_promille'] for word, _ in matches) // len(matches)
                    record.update(ocr_text=text, ocr_score_promille=score, missing_reason=None)
        output.append(record)
    return output


def run_full_frame_tsv(rgb, *, tessdata_dir=None, lang='eng', binary=None, timeout_s=5, runner=None):  # @anchor:va.cu_ocr.run_full_frame_tsv
    """Read bounded full-frame OCR TSV over RAM pipes."""
    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError('OCR requires clean uint8[H,W,3] RGB pixels')
    pf = preflight(tessdata_dir=tessdata_dir, lang=lang, binary=binary, timeout_s=min(timeout_s, 5))
    words, reason = [], pf['missing_reason']
    if pf['ok']:
        try:
            cmd = [pf['binary'], 'stdin', 'stdout', '--tessdata-dir', pf['tessdata_dir'],
                   '-l', lang, '--psm', '6', '-c', 'tessedit_create_tsv=1']
            proc = (runner or subprocess.run)(cmd, input=encode_png(rgb),
                    env=dict(os.environ, TESSDATA_PREFIX=pf['tessdata_dir']),
                    capture_output=True, timeout=min(timeout_s, 5))
            stderr = proc.stderr.decode('utf-8', errors='replace') if isinstance(proc.stderr, bytes) else proc.stderr
            if proc.returncode != 0:
                reason = ('tessdata_absent' if any(s in (stderr or '') for s in
                          ('Error opening data file', 'Could not initialize tesseract')) else 'ocr_unavailable')
            else:
                tsv = proc.stdout.decode('utf-8') if isinstance(proc.stdout, bytes) else proc.stdout
                words = parse_tsv(tsv)
        except (OSError, ValueError, subprocess.SubprocessError):
            reason = 'ocr_unavailable'
    return dict(ocr_ok=reason is None, missing_reason=reason, words=words)


def annotate_marks(rgb, marks, *, named_mark_ids=(), tessdata_dir=None, lang='eng', binary=None, timeout_s=5, clean_sha256=None, nameless_ids=None, runner=None):  # @anchor:va.cu_ocr.annotate_marks
    """Attach OCR evidence only to nameless marks on verified clean RGB pixels."""
    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError('OCR requires clean uint8[H,W,3] RGB pixels')
    if clean_sha256 is not None and hashlib.sha256(rgb.tobytes()).hexdigest() != clean_sha256:
        raise ValueError('OCR must run on clean pre-annotate RGB buffer, never annotated marks_png')
    eligible = {m['id'] for m in marks if m['id'] not in set(named_mark_ids or ())}
    if nameless_ids is not None:
        eligible &= set(nameless_ids)
    evidence = (run_full_frame_tsv(rgb, tessdata_dir=tessdata_dir, lang=lang,
                binary=binary, timeout_s=timeout_s, runner=runner) if eligible else
                dict(ocr_ok=True, missing_reason=None, words=[]))
    output = words_for_marks(evidence['words'], marks, rgb, nameless_ids=eligible)
    if evidence['missing_reason'] is not None:
        for record in output:
            if record['missing_reason'] != 'named_by_ax':
                record.update(ocr_text=None, ocr_score_promille=None,
                              missing_reason=evidence['missing_reason'])
    return output
