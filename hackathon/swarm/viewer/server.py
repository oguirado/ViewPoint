#!/usr/bin/env python3
"""Read-only stdlib HTTP server for ViewPoint Swarm static replays (H5_r1).

Validates `replays/` (`ReplayStore(..., require_pin=True)`) and
`static/data/*.swarm.json` (`validate_swarm_bundle(..., require_pin=True)`)
against external `REPLAY_PINS` env var (strict precedence) and/or `--pins-file`
(`PINS.manifest` stored outside `replays/` and `static/`) fail-closed at startup
before serving any traffic. Permits `GET` and `HEAD` only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlsplit

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parents[1] if len(_THIS_DIR.parents) >= 2 else _THIS_DIR
if (_REPO_ROOT / "gateway" / "replay_store.py").is_file() and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from swarm.viewer.export_replay import (
        REPLAY_ID_RE,
        ExportError,
        Replay,
        ReplayError,
        ReplayNotFound,
        ReplayStore,
        extract_embedded_swarm_sha256,
        load_pins_manifest,
        parse_pins,
        validate_swarm_bundle,
    )
except ModuleNotFoundError:  # pragma: no cover - direct `python server.py` inside `/app`
    from export_replay import (  # type: ignore[import-not-found,no-redef]
        REPLAY_ID_RE,
        ExportError,
        Replay,
        ReplayError,
        ReplayNotFound,
        ReplayStore,
        extract_embedded_swarm_sha256,
        load_pins_manifest,
        parse_pins,
        validate_swarm_bundle,
    )

CSP_POLICY = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "media-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)

STATIC_MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".txt": "text/plain; charset=utf-8",
    ".ico": "image/x-icon",
}

PREFERRED_REPLAY_ORDER: tuple[str, ...] = (
    "take2-w2-4agents",
    "take3-w2-guards",
    "take1-w1-2agents",
)

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def safe_json_bytes(payload: Any) -> bytes:
    """Encode JSON escaping HTML-active characters (`<`, `>`, `&`) to `\\uXXXX`."""
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    encoded = (
        encoded.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    return (encoded + "\n").encode("utf-8")


def sanitize_gateway_url(raw: str | None) -> str | None:
    """Accept only safe `https://...` or `http://localhost[:port]...` / `http://127.0.0.1[:port]...` URLs (preserving `#fragment`)."""
    if raw is None:
        return None
    text = raw.strip()
    if not text or len(text) > 512:
        return None
    if any(ch in text for ch in ("\x00", "\r", "\n", "\t", " ")) or any(
        ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text
    ):
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.username or parts.password:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not host:
        return None
    try:
        _ = parts.port
    except ValueError:
        return None
    if scheme == "https":
        return text
    if scheme == "http" and host in {"localhost", "127.0.0.1"}:
        return text
    return None


def _has_symlink_to_root(target: Path, root: Path) -> bool:
    probe: Path | None = target
    root_resolved = root.resolve()
    while probe is not None:
        try:
            if probe.is_symlink():
                return True
        except OSError:
            return True
        if probe == root or probe == root_resolved:
            break
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    return False


class ViewerState:
    """Validated immutable replay and swarm timeline state loaded fail-closed at startup."""

    def __init__(
        self,
        viewer_dir: Path,
        *,
        pins_file: Path | None = None,
        replay_pins_env: str | None = None,
        gateway_url_env: str | None = None,
    ) -> None:
        self.viewer_dir = Path(viewer_dir)
        if self.viewer_dir.is_symlink() or not self.viewer_dir.is_dir():
            raise ExportError(f"viewer_dir must be a real directory: {self.viewer_dir}")

        self.replay_dir = self.viewer_dir / "replays"
        self.static_dir = self.viewer_dir / "static"
        self.data_dir = self.static_dir / "data"

        for sub, name in (
            (self.replay_dir, "replays"),
            (self.static_dir, "static"),
            (self.data_dir, "static/data"),
        ):
            if _has_symlink_to_root(sub, self.viewer_dir) or not sub.is_dir():
                raise ExportError(f"{name} directory missing or symlink: {sub}")

        self.static_root = self.static_dir.resolve()
        self.replay_root = self.replay_dir.resolve()
        index_html = self.static_dir / "index.html"
        if _has_symlink_to_root(index_html, self.static_dir) or not index_html.is_file():
            raise ExportError(f"static/index.html missing or symlink: {index_html}")

        raw_env_pins = replay_pins_env if replay_pins_env is not None else os.environ.get("REPLAY_PINS")
        raw_env_swarm_pins = os.environ.get("SWARM_PINS")

        if pins_file is not None:
            cand_manifest = Path(pins_file)
            self._reject_mutable_pins_location(cand_manifest)
        else:
            cand_manifest = self.viewer_dir / "PINS.manifest"
            if cand_manifest.is_symlink():
                raise ExportError(f"pins manifest must not be a symlink: {cand_manifest}")

        # R4 + H5_r2: When REPLAY_PINS env var is set, it takes STRICT precedence over any local manifest;
        # local PINS.manifest is NEVER trusted for swarm_pins, and .swarm.json integrity is cryptographically
        # bound to the REPLAY_PINS-verified replay provenance.note `swarm_sha256=<64-hex>` token.
        if raw_env_pins is not None:
            self.pins = parse_pins(raw_env_pins)
            if not self.pins:
                raise ReplayError("REPLAY_PINS must declare at least one pinned replay")
            self.swarm_pins: dict[str, str] | None = (
                parse_pins(raw_env_swarm_pins) if raw_env_swarm_pins is not None else None
            )
        else:
            self._reject_mutable_pins_location(cand_manifest)
            manifest_replay_pins, manifest_swarm_pins = load_pins_manifest(cand_manifest)
            if set(manifest_swarm_pins) != set(manifest_replay_pins):
                raise ExportError(
                    f"PINS.manifest must provide matching replay: and swarm: pins ({cand_manifest})"
                )
            self.pins = manifest_replay_pins
            self.swarm_pins = (
                parse_pins(raw_env_swarm_pins)
                if raw_env_swarm_pins is not None
                else manifest_swarm_pins
            )

        self.store = ReplayStore(
            self.replay_dir,
            self.static_dir,
            pins=self.pins,
            require_pin=True,
        )

        loaded_ids = {r.replay_id for r in self.store.all()}
        if self.swarm_pins is not None and set(self.swarm_pins) != loaded_ids:
            raise ExportError(
                f"swarm PINS mismatch against replays: swarm={sorted(self.swarm_pins)} vs replays={sorted(loaded_ids)}"
            )

        disk_swarm_files = {p.name for p in self.data_dir.glob("*.swarm.json")}
        expected_swarm_files = {f"{rid}.swarm.json" for rid in loaded_ids}
        if disk_swarm_files != expected_swarm_files:
            raise ExportError(
                f"unpinned or missing .swarm.json files in {self.data_dir}: {sorted(disk_swarm_files)}"
            )

        self._swarm_by_id: dict[str, dict[str, Any]] = {}
        self._swarm_sha_by_id: dict[str, str] = {}

        for replay in self.store.all():
            if replay.data.get("mode") != "REPLAY":
                raise ExportError(f"{replay.replay_id} mode must be REPLAY")
            labels = replay.data.get("labels")
            if not isinstance(labels, dict) or labels.get("mode") != "REPLAY" or labels.get("live") is not False:
                raise ExportError(f"{replay.replay_id} labels must declare REPLAY and live=false")
            if replay.attestation != "pinned":
                raise ExportError(f"{replay.replay_id} must have pinned attestation")

            embedded_swarm_sha = extract_embedded_swarm_sha256(
                replay.data,
                f"{replay.replay_id}.json",
            )
            swarm_path = self.data_dir / f"{replay.replay_id}.swarm.json"
            if _has_symlink_to_root(swarm_path, self.data_dir) or not swarm_path.is_file():
                raise ExportError(f"swarm bundle missing or symlink: {swarm_path}")
            swarm_sha = hashlib.sha256(swarm_path.read_bytes()).hexdigest()
            if swarm_sha != embedded_swarm_sha:
                raise ExportError(
                    f"{swarm_path.name} sha256 ({swarm_sha}) does not match "
                    f"{replay.replay_id}.json embedded swarm_sha256 ({embedded_swarm_sha})"
                )

            swarm_doc = validate_swarm_bundle(
                swarm_path,
                static_dir=self.static_dir,
                replay_dir=self.replay_dir,
                pins=self.pins,
                swarm_pins=self.swarm_pins,
                require_pin=True,
            )
            self._swarm_by_id[replay.replay_id] = swarm_doc
            self._swarm_sha_by_id[replay.replay_id] = swarm_sha

        self._ordered_replays: list[Replay] = sorted(
            self.store.all(),
            key=lambda r: (
                PREFERRED_REPLAY_ORDER.index(r.replay_id)
                if r.replay_id in PREFERRED_REPLAY_ORDER
                else len(PREFERRED_REPLAY_ORDER),
                r.replay_id,
            ),
        )
        env_default = (os.environ.get("DEFAULT_REPLAY_ID") or "").strip()
        loaded_map = {r.replay_id: r for r in self._ordered_replays}
        if env_default and env_default in loaded_map:
            self.default_replay_id = env_default
        else:
            self.default_replay_id = self._ordered_replays[0].replay_id

        raw_gw = gateway_url_env if gateway_url_env is not None else os.environ.get("GATEWAY_URL")
        self.gateway_url: str | None = sanitize_gateway_url(raw_gw)

    def _reject_mutable_pins_location(self, pins_path: Path) -> None:
        if pins_path.is_symlink():
            raise ExportError(f"pins_file must not be a symlink: {pins_path}")
        resolved = pins_path.resolve()
        if resolved.is_relative_to(self.replay_root) or resolved.is_relative_to(self.static_root):
            raise ExportError(
                f"pins_file must reside outside mutable replays/ and static/ directories: {pins_path}"
            )

    def is_video_mounted(self, swarm_doc: dict[str, Any]) -> bool:
        video_url = swarm_doc.get("video_url")
        if not isinstance(video_url, str) or not video_url.startswith("/static/videos/"):
            return False
        rel = video_url.removeprefix("/static/")
        if ".." in rel:
            return False
        cand = self.static_dir / rel
        if _has_symlink_to_root(cand, self.static_dir):
            return False
        try:
            resolved = cand.resolve()
        except OSError:
            return False
        return resolved.is_relative_to(self.static_root) and resolved.is_file()

    def list_replays_payload(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for replay in self._ordered_replays:
            summary = ReplayStore.public_summary(replay)
            swarm_doc = self._swarm_by_id[replay.replay_id]
            summary["live"] = False
            summary["swarm_sha256"] = self._swarm_sha_by_id[replay.replay_id]
            summary["source_events_sha256"] = swarm_doc["source_events_sha256"]
            summary["turns"] = len(swarm_doc["turns"])
            summary["cursors"] = len(swarm_doc["cursors"])
            summary["video_url"] = swarm_doc["video_url"]
            summary["video_available"] = self.is_video_mounted(swarm_doc)
            items.append(summary)
        return {
            "mode": "REPLAY",
            "live": False,
            "default_replay_id": self.default_replay_id,
            "gateway_url": self.gateway_url,
            "replays": items,
        }

    def get_replay_payload(self, replay_id: str) -> dict[str, Any]:
        replay: Replay = self.store.get(replay_id)
        swarm_doc = self._swarm_by_id[replay.replay_id]
        return {
            "mode": "REPLAY",
            "live": False,
            "replay_id": replay.replay_id,
            "sha256": replay.sha256,
            "attestation": replay.attestation,
            "swarm_sha256": self._swarm_sha_by_id[replay.replay_id],
            "source_events_sha256": swarm_doc["source_events_sha256"],
            "video_available": self.is_video_mounted(swarm_doc),
            "replay": replay.data,
            "swarm": swarm_doc,
        }


class ViewerHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        viewer_state: ViewerState,
    ) -> None:
        self.viewer_state = viewer_state
        super().__init__(server_address, ViewerRequestHandler)


class ViewerRequestHandler(BaseHTTPRequestHandler):
    server_version = "ViewPointReplayViewer/1.1"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if os.environ.get("VIEWER_QUIET") == "1":
            return
        super().log_message(format, *args)

    def _send_security_headers(self, *, cache_control: str = "no-store") -> None:
        self.send_header("Content-Security-Policy", CSP_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cache-Control", cache_control)

    def _respond_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        head_only: bool = False,
        extra_headers: dict[str, str] | None = None,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self._send_security_headers(cache_control=cache_control)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _respond_json(
        self,
        status: int,
        payload: Any,
        *,
        head_only: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = safe_json_bytes(payload)
        self._respond_bytes(
            status,
            body,
            "application/json; charset=utf-8",
            head_only=head_only,
            extra_headers=extra_headers,
            cache_control="no-store",
        )

    def _reject_method(self) -> None:
        self._respond_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {
                "error": "method_not_allowed",
                "message": "Read-only REPLAY viewer allows GET and HEAD only.",
                "mode": "REPLAY",
                "live": False,
            },
            head_only=False,
            extra_headers={"Allow": "GET, HEAD"},
        )

    def handle_one_request(self) -> None:
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            if self.command not in ("GET", "HEAD"):
                self._reject_method()
                self.wfile.flush()
                return
            if self.command == "HEAD":
                self._dispatch(head_only=True)
            else:
                self._dispatch(head_only=False)
            self.wfile.flush()
        except TimeoutError:
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_GET(self) -> None:  # pragma: no cover - routed via _dispatch
        self._dispatch(head_only=False)

    def do_HEAD(self) -> None:  # pragma: no cover - routed via _dispatch
        self._dispatch(head_only=True)

    def do_POST(self) -> None:
        self._reject_method()

    def do_PUT(self) -> None:
        self._reject_method()

    def do_DELETE(self) -> None:
        self._reject_method()

    def do_PATCH(self) -> None:
        self._reject_method()

    def do_OPTIONS(self) -> None:
        self._reject_method()

    def _dispatch(self, *, head_only: bool) -> None:
        state: ViewerState = self.server.viewer_state  # type: ignore[attr-defined]
        raw_url_path = urlsplit(self.path).path

        if "\x00" in raw_url_path or "\\" in raw_url_path:
            self._respond_json(
                HTTPStatus.FORBIDDEN,
                {"error": "forbidden_path", "mode": "REPLAY", "live": False},
                head_only=head_only,
            )
            return

        unquoted = unquote(raw_url_path)
        if "\x00" in unquoted or "\\" in unquoted:
            self._respond_json(
                HTTPStatus.FORBIDDEN,
                {"error": "forbidden_path", "mode": "REPLAY", "live": False},
                head_only=head_only,
            )
            return

        raw_segments = [seg for seg in raw_url_path.split("/") if seg != ""]
        unquoted_segments = [seg for seg in unquoted.split("/") if seg != ""]
        if any(seg in (".", "..") or seg.startswith(".") for seg in raw_segments + unquoted_segments):
            self._respond_json(
                HTTPStatus.FORBIDDEN,
                {"error": "path_traversal_refused", "mode": "REPLAY", "live": False},
                head_only=head_only,
            )
            return

        if unquoted in ("/", "/index.html"):
            self._serve_static_relative("index.html", head_only=head_only)
            return

        if unquoted == "/healthz":
            self._respond_json(
                HTTPStatus.OK,
                {"ok": True},
                head_only=head_only,
            )
            return

        if unquoted == "/api/replays":
            self._respond_json(
                HTTPStatus.OK,
                state.list_replays_payload(),
                head_only=head_only,
            )
            return

        if unquoted.startswith("/api/replays/"):
            rest = unquoted.removeprefix("/api/replays/").strip("/")
            if "/" in rest or not REPLAY_ID_RE.fullmatch(rest):
                self._respond_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "replay_not_found", "mode": "REPLAY", "live": False},
                    head_only=head_only,
                )
                return
            try:
                payload = state.get_replay_payload(rest)
            except ReplayNotFound:
                self._respond_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "replay_not_found", "mode": "REPLAY", "live": False},
                    head_only=head_only,
                )
                return
            self._respond_json(HTTPStatus.OK, payload, head_only=head_only)
            return

        if unquoted.startswith("/static/"):
            rel_path = unquoted.removeprefix("/static/")
            if not rel_path:
                self._respond_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "not_found", "mode": "REPLAY", "live": False},
                    head_only=head_only,
                )
                return
            self._serve_static_relative(rel_path, head_only=head_only)
            return

        self._respond_json(
            HTTPStatus.NOT_FOUND,
            {"error": "not_found", "mode": "REPLAY", "live": False},
            head_only=head_only,
        )

    def _serve_static_relative(self, rel_path: str, *, head_only: bool) -> None:
        state: ViewerState = self.server.viewer_state  # type: ignore[attr-defined]
        parts = [p for p in rel_path.split("/") if p]
        if not parts or any(p in (".", "..") or p.startswith(".") for p in parts):
            self._respond_json(
                HTTPStatus.FORBIDDEN,
                {"error": "path_traversal_refused", "mode": "REPLAY", "live": False},
                head_only=head_only,
            )
            return

        candidate = state.static_dir.joinpath(*parts)
        if _has_symlink_to_root(candidate, state.static_dir):
            self._respond_json(
                HTTPStatus.FORBIDDEN,
                {"error": "symlink_refused", "mode": "REPLAY", "live": False},
                head_only=head_only,
            )
            return

        try:
            resolved = candidate.resolve()
        except OSError:
            self._respond_json(
                HTTPStatus.NOT_FOUND,
                {"error": "not_found", "mode": "REPLAY", "live": False},
                head_only=head_only,
            )
            return

        if not resolved.is_relative_to(state.static_root):
            self._respond_json(
                HTTPStatus.FORBIDDEN,
                {"error": "path_traversal_refused", "mode": "REPLAY", "live": False},
                head_only=head_only,
            )
            return

        if not resolved.is_file():
            self._respond_json(
                HTTPStatus.NOT_FOUND,
                {"error": "not_found", "mode": "REPLAY", "live": False},
                head_only=head_only,
            )
            return

        ext = resolved.suffix.lower()
        content_type = STATIC_MIME_TYPES.get(ext)
        if content_type is None:
            self._respond_json(
                HTTPStatus.FORBIDDEN,
                {"error": "unsupported_asset_type", "mode": "REPLAY", "live": False},
                head_only=head_only,
            )
            return

        data = resolved.read_bytes()
        total_len = len(data)
        range_hdr = self.headers.get("Range")

        if ext == ".mp4" and range_hdr:
            m = RANGE_RE.fullmatch(range_hdr.strip())
            if m:
                start_s, end_s = m.group(1), m.group(2)
                if start_s == "" and end_s != "":
                    suffix_len = int(end_s)
                    start = max(0, total_len - suffix_len)
                    end = total_len - 1
                else:
                    start = int(start_s) if start_s else 0
                    end = int(end_s) if end_s else (total_len - 1)
                if 0 <= start <= end < total_len:
                    chunk = data[start : end + 1]
                    self._respond_bytes(
                        HTTPStatus.PARTIAL_CONTENT,
                        chunk,
                        content_type,
                        head_only=head_only,
                        extra_headers={
                            "Accept-Ranges": "bytes",
                            "Content-Range": f"bytes {start}-{end}/{total_len}",
                        },
                        cache_control="public, max-age=300",
                    )
                    return

        extra = {"Accept-Ranges": "bytes"} if ext == ".mp4" else None
        cache_ctl = "public, max-age=300" if ext in {".png", ".svg", ".jpg", ".jpeg", ".webp", ".mp4"} else "no-store"
        self._respond_bytes(
            HTTPStatus.OK,
            data,
            content_type,
            head_only=head_only,
            extra_headers=extra,
            cache_control=cache_ctl,
        )


def create_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    viewer_dir: Path | None = None,
    *,
    pins_file: Path | None = None,
    replay_pins_env: str | None = None,
    gateway_url_env: str | None = None,
) -> ViewerHTTPServer:
    """Validate bundles fail-closed and return a bound `ViewerHTTPServer`."""
    resolved_dir = Path(viewer_dir) if viewer_dir is not None else _THIS_DIR
    state = ViewerState(
        resolved_dir,
        pins_file=pins_file,
        replay_pins_env=replay_pins_env,
        gateway_url_env=gateway_url_env,
    )
    return ViewerHTTPServer((host, int(port)), state)


def build_arg_parser() -> argparse.ArgumentParser:
    default_host = os.environ.get("HOST", "127.0.0.1")
    default_port = int(os.environ.get("PORT", "8080"))
    parser = argparse.ArgumentParser(
        description="Run the read-only ViewPoint Swarm static replay viewer (H5_r1)."
    )
    parser.add_argument(
        "--host",
        type=str,
        default=default_host,
        help="Bind host (default: HOST env or 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=default_port,
        help="Bind port (default: PORT env or 8080)",
    )
    parser.add_argument(
        "--viewer-dir",
        type=Path,
        default=_THIS_DIR,
        help="Viewer root directory containing replays/, static/, and PINS.manifest",
    )
    parser.add_argument(
        "--pins-file",
        type=Path,
        default=None,
        help="External pins manifest outside replays/ and static/ (overridden when REPLAY_PINS env var is set)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        httpd = create_server(
            host=args.host,
            port=args.port,
            viewer_dir=args.viewer_dir,
            pins_file=args.pins_file,
        )
    except (ExportError, ReplayError, OSError) as exc:
        print(f"ViewerStartupError: {exc}", file=sys.stderr)
        return 2

    bound_host, bound_port = httpd.server_address[:2]
    print(
        f"[viewer] mode=REPLAY live=false serving http://{bound_host}:{bound_port} "
        f"replays={len(httpd.viewer_state.store.all())} default={httpd.viewer_state.default_replay_id}",
        file=sys.stderr,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
