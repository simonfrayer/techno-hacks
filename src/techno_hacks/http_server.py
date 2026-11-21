"""HTTP API for direct board-to-laptop communication."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from techno_hacks.audio import decode_ulaw
from techno_hacks.backend import Backend
from techno_hacks.bridge import Settings

log = logging.getLogger(__name__)


class MedTrackerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], backend: Backend, settings: Settings):
        super().__init__(address, MedTrackerHandler)
        self.backend = backend
        self.settings = settings


class MedTrackerHandler(BaseHTTPRequestHandler):
    server: MedTrackerHTTPServer

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - " + fmt, self.address_string(), *args)

    def _json(self, status: int, value: dict) -> None:
        data = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/v1/health":
            self._json(200, {"ok": True})
            return
        if path == "/api/v1/config":
            s = self.server.settings
            self._json(200, {
                "places": s.places,
                "t_min": s.t_min,
                "t_max": s.t_max,
                "h_max": s.h_max,
                "report_minutes": s.report_minutes,
            })
            return
        self._json(404, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/v1/audio":
            self._post_audio()
            return
        if path == "/api/v1/records":
            try:
                value = self._body_json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json(400, {"ok": False, "error": "Invalid JSON"})
                return
            ok, msg = self.server.backend.save_record(value)
            self._json(200 if ok else 422, {"ok": ok, "error": None if ok else msg, "message": msg})
            return
        if path == "/api/v1/climate":
            try:
                value = self._body_json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json(400, {"ok": False, "error": "Invalid JSON"})
                return
            self.server.backend.log_climate(value)
            self._json(200, {"ok": True})
            return
        self._json(404, {"ok": False, "error": "Not found"})

    def _post_audio(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        try:
            record_id = int(query["id"][0])
            rate = int(query.get("rate", ["8000"])[0])
        except (KeyError, ValueError, IndexError):
            self._json(400, {"ok": False, "error": "id and rate are required"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length)
        if not data:
            self._json(400, {"ok": False, "error": "No audio data"})
            return
        samples = decode_ulaw(data)
        result = self.server.backend.understand(record_id, samples, rate)
        if not result.ok:
            self._json(422, {"ok": False, "error": result.error or "Not understood"})
            return
        details = result.details
        self._json(200, {
            "ok": True,
            "id": record_id,
            "name": details.name,
            "expiry": details.expiry,
            "quantity": details.quantity,
        })


def serve(host: str, port: int, backend: Backend, settings: Settings) -> MedTrackerHTTPServer:
    server = MedTrackerHTTPServer((host, port), backend, settings)
    log.info("HTTP server listening on http://%s:%d", host, port)
    return server
