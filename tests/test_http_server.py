import json
import threading
from urllib.error import HTTPError
import urllib.request

from techno_hacks.backend import Backend
from techno_hacks.bridge import Settings
from techno_hacks.http_server import serve
from techno_hacks.store import Store
from techno_hacks.transcribe import FixedTranscriber
from techno_hacks.audio import linear_to_ulaw


def start_server(tmp_path):
    backend = Backend(
        Store(tmp_path / "db.sqlite3"),
        FixedTranscriber("Ibuprofen 400, expires March 2027, 20 tablets"),
        tmp_path / "rec",
    )
    server = serve("127.0.0.1", 0, backend, Settings(report_seconds=15))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, backend, thread


def request(server, path, method="GET", body=None, content_type="application/json"):
    url = f"http://127.0.0.1:{server.server_port}{path}"
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=5) as response:
        return response.status, json.loads(response.read())


def test_http_config_and_audio_flow(tmp_path):
    server, backend, thread = start_server(tmp_path)
    try:
        status, config = request(server, "/api/v1/config")
        assert status == 200
        assert config["places"][0] == "Pharmacy shelf A"
        assert config["t_max"] == 26.0
        assert config["h_max"] == 50.0
        assert config["report_seconds"] == 15

        climate = json.dumps({
            "temp_c": 21.5,
            "humidity": 48.0,
            "status": "ok",
            "reason": "periodic",
            "t_min": 2.0,
            "t_max": 26.0,
            "h_max": 50.0,
        }).encode()
        status, response = request(server, "/api/v1/climate", method="POST", body=climate)
        assert status == 200
        assert response["ok"] is True
        assert backend.store.climate()[0]["temp_c"] == 21.5

        audio = bytes(linear_to_ulaw(1000 if i % 2 else -1000) for i in range(300))
        status, voice = request(
            server,
            "/api/v1/audio?id=42&rate=8000",
            method="POST",
            body=audio,
            content_type="application/octet-stream",
        )
        assert status == 200
        assert voice["name"] == "Ibuprofen 400"

        record = json.dumps({
            "id": 42,
            "name": voice["name"],
            "expiry": voice["expiry"],
            "quantity": voice["quantity"],
            "place": "Fridge",
        }).encode()
        status, saved = request(server, "/api/v1/records", method="POST", body=record)
        assert status == 200
        assert saved["ok"] is True
        assert backend.store.records()[0].place == "Fridge"

        status, records = request(server, "/api/v1/records?limit=10")
        assert status == 200
        assert records["records"][0]["name"] == "Ibuprofen 400"

        status, climate_data = request(server, "/api/v1/climate?limit=10")
        assert status == 200
        assert climate_data["climate"][0]["temp_c"] == 21.5
    finally:
        server.shutdown()
        server.server_close()


def test_http_list_limit_must_be_valid(tmp_path):
    server, _, _ = start_server(tmp_path)
    try:
        try:
            request(server, "/api/v1/records?limit=0")
        except HTTPError as exc:
            assert exc.code == 400
        else:
            raise AssertionError("invalid limit should return HTTP 400")
    finally:
        server.shutdown()
        server.server_close()


def test_http_api_allows_local_web_app_origin_but_not_arbitrary_sites(tmp_path):
    server, _, _ = start_server(tmp_path)
    try:
        for origin, expected in (
            ("http://localhost:5173", "http://localhost:5173"),
            ("http://localhost:3000", "http://localhost:3000"),
            ("http://127.0.0.1:4173", "http://127.0.0.1:4173"),
            ("https://untrusted.example", None),
        ):
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/v1/records",
                headers={"Origin": origin},
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                assert response.headers.get("Access-Control-Allow-Origin") == expected
    finally:
        server.shutdown()
        server.server_close()
