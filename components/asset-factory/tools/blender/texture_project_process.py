"""Compatibility import for historical texture-worker recipes.

The projection caller now starts its worker directly as a systemd service.
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from providers.trellis2.build.process_guard import main, supervise

if __name__ == '__main__':
    raise SystemExit(main())
