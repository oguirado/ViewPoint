#!/usr/bin/env python3
"""Keep the existing private driver entry point for the channel harness."""

from pathlib import Path
from runpy import run_path


if __name__ == "__main__":
    run_path(str(Path(__file__).resolve().parent / "tests" / "LIVE_channel.py"),
             run_name="__main__")
