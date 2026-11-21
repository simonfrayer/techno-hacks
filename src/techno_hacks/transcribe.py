"""Speech-to-text. Pluggable: anything with ``transcribe(wav_path) -> str``.

Built in:
  * ``whisper`` (default): local Whisper via faster-whisper, offline after the
    model's first download (Hugging Face cache, ~/.cache/huggingface).
  * ``manual``: prints where the WAV is and asks you to type what was said.
  * ``fixed``: always returns the same sentence (demos and tests).

Another engine plugs in by adding a class here and a name in :func:`get_transcriber`.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional, Protocol

log = logging.getLogger(__name__)

# Primes Whisper with the vocabulary and the shape of the sentence we expect, so it
# writes "20 tablets" and "March 2027" as digits and spells drug names sensibly.
WHISPER_PROMPT = (
    "Medicine name, expiry date and tablet count. "
    "Ibuprofen 400, expires March 2027, 20 tablets. "
    "Paracetamol 500 mg, expiry 03/2026, 30 tablets."
)


class Transcriber(Protocol):
    def transcribe(self, wav_path: Path) -> str: ...


def load_wav_16k(path: Path):
    """16-bit PCM WAV -> mono float32 at 16 kHz in [-1, 1], what Whisper expects.

    Reading it ourselves avoids faster-whisper's PyAV decoder (faster-whisper
    1.2.1 passes an argument that current PyAV releases reject).
    """
    import wave

    import numpy as np

    with wave.open(str(path)) as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: need 16-bit PCM WAV")
        rate, channels = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    if rate != 16000 and len(x):
        n = int(round(len(x) * 16000 / rate))
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
    return x


class WhisperTranscriber:
    """Local speech-to-text with faster-whisper (CTranslate2, int8 on the CPU).

    Models: tiny.en, base.en (default, ~140 MB), small.en (~460 MB, more accurate),
    medium.en. The model loads on first use, or call :meth:`load` up front.
    """

    def __init__(self, model: str = "base.en", language: Optional[str] = "en", model_factory=None):
        self.model_name = model
        self.language = language       # None = let Whisper detect (multilingual models only)
        self._factory = model_factory
        self._model = None

    def load(self) -> None:
        if self._model is not None:
            return
        t0 = time.monotonic()
        if self._factory is not None:
            self._model = self._factory(self.model_name)
        else:
            import ctranslate2
            import onnxruntime
            from faster_whisper import WhisperModel

            ctranslate2.set_log_level(logging.ERROR)        # "MPS backend available" tip
            onnxruntime.set_default_logger_severity(3)       # VAD model's telemetry warnings
            try:  # cached model: no network at all
                self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                           local_files_only=True)
            except Exception:
                log.info("downloading Whisper model %s (once)", self.model_name)
                self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        log.info("Whisper %s ready in %.1f s", self.model_name, time.monotonic() - t0)

    def transcribe(self, wav_path: Path) -> str:
        self.load()
        t0 = time.monotonic()
        segments, _info = self._model.transcribe(
            load_wav_16k(wav_path),         # array input: no PyAV/ffmpeg decoding needed
            language=self.language,
            beam_size=5,
            initial_prompt=WHISPER_PROMPT,
            condition_on_previous_text=False,
            vad_filter=True,               # skip the silence before/after speaking
        )
        text = " ".join(s.text.strip() for s in segments).strip()  # segments is lazy
        log.info("transcribed in %.1f s: %r", time.monotonic() - t0, text)
        return text


class ManualTranscriber:
    def transcribe(self, wav_path: Path) -> str:
        print(f"\n  recording saved: {wav_path}  (play it with: afplay {wav_path})")
        return input("  what was said? > ").strip()


class FixedTranscriber:
    def __init__(self, text: str = "Ibuprofen 400, expires March 2027, 20 tablets"):
        self.text = text

    def transcribe(self, wav_path: Path) -> str:
        return self.text


def get_transcriber(name: str, **kwargs) -> Transcriber:
    if name == "whisper":
        return WhisperTranscriber(**kwargs)
    if name == "manual":
        return ManualTranscriber()
    if name == "fixed":
        return FixedTranscriber(**kwargs)
    raise ValueError(f"unknown transcriber {name!r} (choose: whisper, manual, fixed)")
