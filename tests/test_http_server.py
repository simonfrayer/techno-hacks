import json
import threading
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
    server = serve("127.0.0.1", 0, backend, Settings())
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
    finally:
        server.shutdown()
        server.server_close()
