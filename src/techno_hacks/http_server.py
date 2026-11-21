"""HTTP API for direct board-to-laptop communication."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlsplit

from techno_hacks.audio import decode_ulaw
from techno_hacks.backend import Backend
from techno_hacks.bridge import Settings

log = logging.getLogger(__name__)
DEFAULT_LIST_LIMIT = 500
MAX_LIST_LIMIT = 5000


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
        origin = self._allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _allowed_origin(self) -> str | None:
        default = "http://localhost:5173,http://127.0.0.1:5173"
        allowed = {
            entry.strip()
            for entry in os.environ.get("MEDTRACKER_WEB_ORIGINS", default).split(",")
            if entry.strip()
        }
        origin = self.headers.get("Origin", "")
        if origin in allowed:
            return origin
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return None
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            if parsed.hostname.lower() == "localhost":
                return origin
            try:
                if ipaddress.ip_address(parsed.hostname).is_loopback:
                    return origin
            except ValueError:
                pass
        return None

    def _list_limit(self) -> int:
        query = parse_qs(urlparse(self.path).query)
        try:
            limit = int(query.get("limit", [str(DEFAULT_LIST_LIMIT)])[0])
        except ValueError as exc:
            raise ValueError("limit must be an integer") from exc
        if not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
        return limit

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
                "report_seconds": s.report_seconds,
            })
            return
        if path in ("/api/v1/records", "/api/v1/climate"):
            try:
                limit = self._list_limit()
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            if path.endswith("/records"):
                rows = [asdict(row) for row in self.server.backend.store.records(limit=limit)]
                self._json(200, {"records": rows})
            else:
                self._json(200, {"climate": self.server.backend.store.climate(limit=limit)})
            return
        self._json(404, {"ok": False, "error": "Not found"})

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        origin = self._allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

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
