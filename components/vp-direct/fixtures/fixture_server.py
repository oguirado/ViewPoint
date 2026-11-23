#!/usr/bin/env python3
"""Tiny stdlib fixture server for the vp-direct fixture site.

Serves static HTML files from ``site/`` on 127.0.0.1 only. No network
access beyond loopback. Usage:

    python3 -I fixture_server.py [--host 127.0.0.1] [--port 0] [--root DIR]

Exit code 0 means the server served until stopped. The caller stops it
by its own recorded PID; the server installs no signal handlers beyond
Python defaults and never manages other processes.
"""

import argparse
import http.server
import functools
import socket
import sys
from pathlib import Path


def free_loopback_port(host="127.0.0.1"):
    """Return one currently free TCP port on the given loopback host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def make_handler(root: Path):
    """Build a request handler that serves only files under root."""
    safe_root = root.resolve()

    class FixtureHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(safe_root), **kwargs)

        def log_message(self, fmt, *args):  # keep output tiny
            sys.stderr.write("fixture-server: " + fmt % args + "\n")

        def end_headers(self):
            self.send_header("X-Fixture-Server", "vp-direct")
            super().end_headers()

    return FixtureHandler


def main(argv=None):
    parser = argparse.ArgumentParser(description="vp-direct fixture server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--root", default=None,
                        help="site directory (default: site/ beside this file)")
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1":
        parser.error("fixture server binds 127.0.0.1 only")
    here = Path(__file__).resolve().parent
    root = Path(args.root) if args.root else here / "site"
    handler = make_handler(root)
    with http.server.ThreadingHTTPServer((args.host, args.port), handler) as httpd:
        host, port = httpd.server_address[:2]
        sys.stderr.write(f"fixture-server listening on {host}:{port}\n")
        sys.stderr.flush()
        httpd.serve_forever(poll_interval=0.1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
