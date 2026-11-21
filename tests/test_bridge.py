"""Bridge behaviour against a fake board (records the actions it is sent)."""

import base64

import pytest
from atech import ActionRejected, Event, NoAcknowledgement

from techno_hacks.audio import linear_to_ulaw
from techno_hacks.backend import Backend
from techno_hacks.bridge import Bridge, Settings, screen_text
from techno_hacks.store import Store
from techno_hacks.transcribe import FixedTranscriber


def test_screen_text_is_plain_ascii():
    assert screen_text("Gelomyrtol für Kühlschrank") == "Gelomyrtol fuer Kuehlschrank"
    assert screen_text("Crème \"forte\" ß") == "Creme 'forte' ss"


def test_non_ascii_name_reaches_board_as_ascii(tmp_path):
    board, _, bridge = make(tmp_path, text="Schüßler Salz, expires March 2027, 20 tablets")
    start, chunks, end = audio_events()
    bridge.handle(ev("audio_start", start))
    for c in chunks:
        bridge.handle(ev("audio", c))
    bridge.handle(ev("audio_end", end))
    assert board.calls[-1][1]["name"] == "Schuessler Salz"


class FakeBoard:
    def __init__(self, resend=None, reject=(), silent=False):
        self.calls: list[tuple[str, object]] = []
        self.resend = resend or {}      # seq -> chunk value the board answers audio_resend with
        self.reject = set(reject)
        self.silent = silent            # e.g. still running the atech.dev firmware

    def call(self, key, value="", *, timeout=2.0):
        self.calls.append((key, value))
        if self.silent:
            raise NoAcknowledgement(f"no answer from the board for action {key!r}")
        if key in self.reject:
            raise ActionRejected(f"the board could not carry out {key!r}: busy.")

    def query(self, key, value="", *, reply, timeout=2.0):
        self.calls.append((key, value))
        return self.resend[int(value)]


def ev(key, value):
    return Event(type="state", key=key, value=value)


def make(tmp_path, text="Ibuprofen 400, expires March 2027, 20 tablets", **board_kw):
    board = FakeBoard(**board_kw)
    backend = Backend(Store(tmp_path / "db.sqlite3"), FixedTranscriber(text), tmp_path / "rec")
    return board, backend, Bridge(board, backend, Settings())


def audio_events(rid=42, n_bytes=300, chunk=96):
    data = bytes(linear_to_ulaw(1000 if i % 2 else -1000) for i in range(n_bytes))
    chunks = [
        {"id": rid, "seq": s, "d": base64.b64encode(data[s * chunk:(s + 1) * chunk]).decode()}
        for s in range(-(-n_bytes // chunk))
    ]
    start = {"id": rid, "rate": 8000, "bits": 8, "encoding": "mulaw", "bytes": n_bytes, "chunk": chunk}
    end = {"id": rid, "chunks": len(chunks), "bytes": n_bytes}
    return start, chunks, end


def test_configures_board(tmp_path):
    board, _, bridge = make(tmp_path)
    bridge.handle(ev("app_info", {"app": "medtracker"}))
    assert board.calls == [
        ("places", "Pharmacy shelf A,Pharmacy shelf B,Fridge,Cabinet 1,Cabinet 2"),
        ("limits", {"t_min": 2.0, "t_max": 26.0, "h_max": 50.0}),
        ("report_seconds", 15),
    ]


def test_silent_board_gets_one_clear_warning(tmp_path, caplog):
    board, _, bridge = make(tmp_path, silent=True)
    assert bridge.configure_board() is False
    assert board.calls == [("places", "Pharmacy shelf A,Pharmacy shelf B,Fridge,Cabinet 1,Cabinet 2")]
    assert "atech upload firmware" in caplog.text


def test_full_flow_voice_then_saved(tmp_path):
    board, backend, bridge = make(tmp_path)
    start, chunks, end = audio_events()
    bridge.handle(ev("audio_start", start))
    for c in chunks:
        bridge.handle(ev("audio", c))
    bridge.handle(ev("audio_end", end))
    assert board.calls[-1] == ("voice", {"id": 42, "name": "Ibuprofen 400", "expiry": "2027-03", "quantity": "20"})
    assert (tmp_path / "rec" / "42.wav").is_file()

    bridge.handle(ev("record", {"id": 42, "name": "Ibuprofen 400", "expiry": "2027-03",
                                "quantity": "20", "place": "Fridge"}))
    assert board.calls[-1] == ("saved", {"id": 42})
    [row] = backend.store.records()
    assert (row.name, row.place, row.device_record_id) == ("Ibuprofen 400", "Fridge", 42)
    assert row.transcript.startswith("Ibuprofen") and row.audio_path.endswith("42.wav")


def test_missing_chunk_is_requested_again(tmp_path):
    start, chunks, end = audio_events()
    board, _, bridge = make(tmp_path, resend={1: chunks[1]})
    bridge.handle(ev("audio_start", start))
    for c in chunks:
        if c["seq"] != 1:
            bridge.handle(ev("audio", c))
    bridge.handle(ev("audio_end", end))
    assert ("audio_resend", 1) in board.calls
    assert board.calls[-1][0] == "voice"


def test_unrecoverable_audio_is_a_voice_error(tmp_path):
    start, chunks, end = audio_events()
    board, _, bridge = make(tmp_path, resend={1: {"id": 999, "seq": 1, "d": ""}})
    bridge.handle(ev("audio_start", start))
    for c in chunks:
        if c["seq"] != 1:
            bridge.handle(ev("audio", c))
    bridge.handle(ev("audio_end", end))
    assert board.calls[-1] == ("voice_error", "Audio incomplete")


def test_not_understood_is_a_voice_error(tmp_path):
    board, _, bridge = make(tmp_path, text="something about the weather")
    start, chunks, end = audio_events()
    bridge.handle(ev("audio_start", start))
    for c in chunks:
        bridge.handle(ev("audio", c))
    bridge.handle(ev("audio_end", end))
    assert board.calls[-1] == ("voice_error", "No expiry date")


def test_bad_record_is_an_error(tmp_path):
    board, backend, bridge = make(tmp_path)
    bridge.handle(ev("record", {"id": 5, "name": "X", "expiry": "", "quantity": "1", "place": "Fridge"}))
    assert board.calls[-1] == ("error", {"id": 5, "msg": "Missing expiry"})
    assert backend.store.records() == []


def test_board_refusal_does_not_crash(tmp_path):
    board, _, bridge = make(tmp_path, reject={"saved"})
    bridge.handle(ev("record", {"id": 5, "name": "X", "expiry": "2027-01", "quantity": "1", "place": "Fridge"}))
    assert board.calls[-1][0] == "saved"


def test_climate_is_logged(tmp_path):
    _, backend, bridge = make(tmp_path)
    bridge.handle(ev("climate", {"temp_c": 24.5, "humidity": 40.0, "status": "warning", "reason": "hourly",
                                 "t_min": 2.0, "t_max": 26.0, "h_max": 50.0}))
    bridge.handle(ev("climate", {"ok": False, "reason": "startup"}))
    [row] = backend.store.climate()
    assert (row["temp_c"], row["status"]) == (24.5, "warning")


def test_settings_validation_and_toml(tmp_path):
    with pytest.raises(ValueError):
        Settings(places=["A" * 24])
    with pytest.raises(ValueError):
        Settings(places=["a,b"])
    cfg = tmp_path / "medtracker.toml"
    cfg.write_text('[device]\nplaces = ["Fridge", "Shelf"]\nt_max = 8.0\n')
    s = Settings.load(cfg)
    assert (s.places, s.t_max, s.t_min) == (["Fridge", "Shelf"], 8.0, 2.0)
    assert Settings.load(tmp_path / "nope.toml") == Settings()


def test_report_interval_in_seconds(tmp_path):
    assert Settings().report_seconds == 15
    with pytest.raises(ValueError):
        Settings(report_seconds=2)
    old = tmp_path / "old.toml"
    old.write_text("[device]\nreport_minutes = 15\n")   # pre-seconds config files still work
    assert Settings.load(old).report_seconds == 900
