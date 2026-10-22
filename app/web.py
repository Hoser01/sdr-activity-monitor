#!/usr/bin/env python3
"""Read-only HTTP bridge for the SDR terminal dashboard."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.dashboard import render
from app.diagnostics import build_diagnostics
from app.api import dashboard_data

LOG = logging.getLogger("sdr_dashboard_web")
INDEX_PATH = Path(__file__).parent.parent / "web" / "index.html"


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "SDRDashboard/1.0"

    def _send(self, status: int, content_type: str, body: bytes,
              send_body: bool = True) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", INDEX_PATH.read_bytes(), False)
            return
        if self.path in ("/api/console", "/api/diagnostics", "/api/dashboard"):
            self._send(200, "application/json; charset=utf-8", b"", False)
            return
        if self.path == "/healthz":
            self._send(200, "text/plain; charset=utf-8", b"ok\n", False)
            return
        self._send(404, "text/plain; charset=utf-8", b"not found\n", False)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", INDEX_PATH.read_bytes())
            return
        if self.path == "/api/console":
            try:
                output = render(self.server.db_path)  # type: ignore[attr-defined]
                payload = {
                    "ok": True,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "output": output,
                }
                self._send(200, "application/json; charset=utf-8", json.dumps(payload).encode())
            except Exception as exc:
                LOG.exception("dashboard render failed")
                self._send(503, "application/json; charset=utf-8", json.dumps({
                    "ok": False,
                    "error": str(exc),
                }).encode())
            return
        if self.path == "/api/diagnostics":
            try:
                payload = build_diagnostics(self.server.db_path)  # type: ignore[attr-defined]
                self._send(200, "application/json; charset=utf-8",
                           json.dumps(payload, indent=2).encode())
            except Exception as exc:
                LOG.exception("diagnostics export failed")
                self._send(503, "application/json; charset=utf-8",
                           json.dumps({"ok": False, "error": str(exc)}).encode())
            return
        if self.path == "/api/dashboard":
            try:
                payload = dashboard_data(self.server.db_path)  # type: ignore[attr-defined]
                self._send(200, "application/json; charset=utf-8", json.dumps(payload).encode())
            except Exception as exc:
                LOG.exception("dashboard API failed")
                self._send(503, "application/json; charset=utf-8",
                           json.dumps({"ok": False, "error": str(exc)}).encode())
            return
        if self.path == "/healthz":
            self._send(200, "text/plain; charset=utf-8", b"ok\n")
            return
        self._send(404, "text/plain; charset=utf-8", b"not found\n")

    def log_message(self, fmt: str, *args: object) -> None:
        LOG.info("%s %s", self.client_address[0], fmt % args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Web view of the SDR terminal dashboard")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18090)
    parser.add_argument("--database", type=Path,
                        default=Path("/opt/sdr-activity-monitor/data/activity.db"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    server = ThreadingHTTPServer((args.bind, args.port), DashboardHandler)
    server.db_path = args.database  # type: ignore[attr-defined]
    LOG.info("listening on http://%s:%s", args.bind, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
