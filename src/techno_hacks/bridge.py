"""USB bridge: board events in, backend calls, actions back to the board.

    board --audio_start/audio/audio_end--> bridge --understand()--> backend
    board <------------ voice / voice_error ------------------------
    board --record-------------------------> bridge --save_record()-->
    board <------------ saved / error -------------------------------
    board --climate------------------------> bridge --log_climate()-->

On connect, and whenever the board reboots (``app_info`` event), the bridge
pushes the configured storage places and climate limits to the board.
"""

from __future__ import annotations

import logging
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

from atech import ActionError, Event, NoAcknowledgement

from techno_hacks.audio import AudioAssembler
from techno_hacks.backend import Backend

log = logging.getLogger(__name__)

DEFAULT_PLACES = ["Pharmacy shelf A", "Pharmacy shelf B", "Fridge", "Cabinet 1", "Cabinet 2"]
SCREEN_MSG_MAX = 39      # the board keeps 39 characters of a result message
PLACE_MAX = 23           # and 23 per storage place, at most 10 places

_GERMAN = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


def screen_text(text: str) -> str:
    """Plain ASCII for the board: its font has no accents, and the firmware's
    JSON reader does not decode \\u escapes (json.dumps writes them for non-ASCII)."""
    text = unicodedata.normalize("NFKD", text.translate(_GERMAN))
    return text.encode("ascii", "ignore").decode().replace('"', "'").replace("\\", "/").strip()


@dataclass
class Settings:
    places: list[str] = field(default_factory=lambda: list(DEFAULT_PLACES))
    t_min: float = 2.0
    t_max: float = 25.0
    h_max: float = 60.0
    report_minutes: int = 60

    def __post_init__(self) -> None:
        if not 1 <= len(self.places) <= 10:
            raise ValueError("places: between 1 and 10 entries")
        for p in self.places:
            if not screen_text(p) or len(screen_text(p)) > PLACE_MAX or "," in p:
                raise ValueError(f"place {p!r}: 1-{PLACE_MAX} characters, no commas")

    @classmethod
    def load(cls, path: Path) -> "Settings":
        """Read ``[device]`` from a TOML file; missing file or keys keep the defaults."""
        if not path.is_file():
            return cls()
        data = tomllib.loads(path.read_text(encoding="utf-8")).get("device", {})
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class BoardLike(Protocol):
    def call(self, key: str, value=..., *, timeout: float = ...) -> None: ...
    def query(self, key: str, value=..., *, reply: str, timeout: float = ...): ...


class Bridge:
    def __init__(self, board: BoardLike, backend: Backend, settings: Settings | None = None):
        self.board = board
        self.backend = backend
        self.settings = settings or Settings()
        self._audio: Optional[AudioAssembler] = None

    # ------------------------------------------------------------ board config
    def configure_board(self) -> bool:
        """Send places, limits and report interval. False if the board did not answer."""
        s = self.settings
        for key, value in (
            ("places", ",".join(screen_text(p) for p in s.places)),
            ("limits", {"t_min": s.t_min, "t_max": s.t_max, "h_max": s.h_max}),
            ("report_minutes", s.report_minutes),
        ):
            try:
                self.board.call(key, value, timeout=2.0)
            except NoAcknowledgement:
                log.warning(
                    "the board does not answer actions. Is the medtracker firmware flashed? "
                    "Stop the bridge and run `uv run atech upload firmware` (an atech.dev build "
                    "never answers). The bridge keeps listening and configures the board when it boots."
                )
                return False
            except ActionError as exc:
                log.warning("board refused %s: %s", key, exc)
        log.info("board configured: %d places, %.1f-%.1f C, humidity <= %.0f %%",
                 len(s.places), s.t_min, s.t_max, s.h_max)
        return True

    # ------------------------------------------------------------ events
    def handle(self, event: Event) -> None:
        handler = getattr(self, f"_on_{event.key}", None)
        if handler is not None:
            handler(event.value)

    def _on_app_info(self, value) -> None:
        log.info("board started: %s", value)
        self.configure_board()

    def _on_ui_state(self, value) -> None:
        log.debug("board state: %s", value)

    def _on_audio_start(self, value: dict) -> None:
        self._audio = AudioAssembler.from_start_event(value)
        log.info("recording %s: %.1f s incoming", self._audio.record_id, self._audio.seconds)

    def _on_audio(self, value: dict) -> None:
        a = self._audio
        if a is not None and int(value.get("id", -1)) == a.record_id:
            a.add(int(value["seq"]), str(value["d"]))

    def _on_audio_end(self, value: dict) -> None:
        a = self._audio
        if a is None or int(value.get("id", -1)) != a.record_id:
            log.warning("audio_end for an unknown recording: %s", value)
            return
        self._audio = None
        rid = a.record_id
        for seq in a.missing():
            try:
                chunk = self.board.query("audio_resend", seq, reply="audio", timeout=2.0)
                if isinstance(chunk, dict) and int(chunk.get("id", -1)) == rid:
                    a.add(int(chunk["seq"]), str(chunk["d"]))
            except ActionError as exc:
                log.warning("resend of chunk %d failed: %s", seq, exc)
        if not a.complete:
            log.warning("recording %s incomplete, %d chunks missing", rid, len(a.missing()))
            self._call("voice_error", "Audio incomplete")
            return

        result = self.backend.understand(rid, a.samples(), a.rate)
        log.info("heard %r -> %s", result.transcript, result.details or result.error)
        if result.ok:
            d = result.details
            self._call("voice", {"id": rid, "name": screen_text(d.name)[:47],
                                 "expiry": d.expiry, "quantity": d.quantity})
        else:
            self._call("voice_error", screen_text(result.error or "Not understood")[:SCREEN_MSG_MAX])

    def _on_record(self, value: dict) -> None:
        ok, msg = self.backend.save_record(value)
        log.info("record %s -> %s (%s)", value, "saved" if ok else "error", msg)
        rid = value.get("id")
        if ok:
            self._call("saved", {"id": rid})
        else:
            self._call("error", {"id": rid, "msg": screen_text(msg)[:SCREEN_MSG_MAX]})

    def _on_climate(self, value: dict) -> None:
        log.info("climate: %s", value)
        self.backend.log_climate(value)

    def _on_places(self, value) -> None:
        log.debug("board has %s storage places", value)

    def _on_recording_full(self, value) -> None:
        log.info("recording hit the %s s limit", value.get("seconds") if isinstance(value, dict) else value)

    # ------------------------------------------------------------ helpers
    def _call(self, key: str, value) -> bool:
        try:
            self.board.call(key, value, timeout=2.0)
            return True
        except ActionError as exc:
            log.warning("board refused %s: %s", key, exc)
            return False

    def run(self, events) -> None:
        """Process events forever (``events`` is ``board.events()``)."""
        self.configure_board()
        for event in events:
            try:
                self.handle(event)
            except Exception:
                log.exception("error handling %s", event.key)
