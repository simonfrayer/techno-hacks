"""The backend: understand a recording, store records, log climate.

Runs in-process with the bridge. It knows nothing about the board or USB, so it
can later sit behind an HTTP API without changes.
"""

from __future__ import annotations

import logging
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from techno_hacks.audio import normalize, write_wav
from techno_hacks.extract import Details, NotUnderstood, parse_details
from techno_hacks.store import Record, Store
from techno_hacks.transcribe import Transcriber

log = logging.getLogger(__name__)


@dataclass
class VoiceResult:
    details: Optional[Details]
    error: Optional[str]          # short, shown on the 13-character screen lines
    transcript: str = ""
    audio_path: Optional[Path] = None

    @property
    def ok(self) -> bool:
        return self.details is not None


class Backend:
    def __init__(self, store: Store, transcriber: Transcriber, recordings_dir: Path):
        self.store = store
        self.transcriber = transcriber
        self.recordings_dir = Path(recordings_dir)
        self._voice: dict[int, VoiceResult] = {}   # device record id -> what was heard

    def understand(self, record_id: int, samples: array, rate: int) -> VoiceResult:
        wav = write_wav(self.recordings_dir / f"{record_id}.wav", normalize(samples), rate)
        try:
            transcript = self.transcriber.transcribe(wav)
        except Exception as exc:  # a broken engine must not kill the bridge
            log.exception("transcription failed")
            result = VoiceResult(None, "STT failed", "", wav)
            self._voice[record_id] = result
            return result
        try:
            result = VoiceResult(parse_details(transcript), None, transcript, wav)
        except NotUnderstood as exc:
            result = VoiceResult(None, str(exc), transcript, wav)
        self._voice[record_id] = result
        return result

    def save_record(self, value: dict) -> tuple[bool, str]:
        """Store a record from the board's ``record`` event. Returns (ok, message)."""
        missing = [k for k in ("name", "expiry", "quantity", "place") if not str(value.get(k, "")).strip()]
        if missing:
            return False, f"Missing {missing[0]}"
        record_id = int(value["id"]) if str(value.get("id", "")).isdigit() else None
        heard = self._voice.pop(record_id, None) if record_id is not None else None
        try:
            row = self.store.add_record(Record(
                name=str(value["name"]), expiry=str(value["expiry"]), quantity=str(value["quantity"]),
                place=str(value["place"]), device_record_id=record_id,
                transcript=heard.transcript if heard else None,
                audio_path=str(heard.audio_path) if heard and heard.audio_path else None,
            ))
        except Exception:
            log.exception("could not store record")
            return False, "DB error"
        return True, f"row {row}"

    def log_climate(self, value: dict) -> None:
        if value.get("ok") is False:
            log.warning("climate sensor not readable (%s)", value.get("reason"))
            return
        self.store.add_climate(value)
