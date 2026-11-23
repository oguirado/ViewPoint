"""Opt-in pinned engine startup: PYTHONPATH=<repo>/config python3 ... ."""
import hashlib
import json
import os
from pathlib import Path
import sys

_root = Path(__file__).resolve().parent.parent
try:
    _cfg = json.loads((_root / 'config/vision.json').read_text())
    _wheel = Path(_cfg['engine_path'])
    if not _wheel.is_absolute():
        _wheel = _root / _wheel
    if hashlib.sha256(_wheel.read_bytes()).hexdigest() != _cfg['pin']['wheel_sha256']:
        raise ValueError('engine wheel hash mismatch')
except (OSError, ValueError, KeyError) as _error:
    sys.stderr.write('E_PIN: %s\n' % _error)
    os._exit(2)
sys.path.insert(0, str(_wheel))
