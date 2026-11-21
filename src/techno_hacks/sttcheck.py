"""Try speech-to-text + extraction without the board.

    techno-hacks stt-check --say "Ibuprofen 400, expires March 2027, 20 tablets"
    techno-hacks stt-check data/recordings/123456.wav

Audio goes through the same path as on the board (16 kHz -> pair-averaged
8 kHz -> mu-law -> back), so the result shows what the board's audio quality
allows. Board recordings in data/recordings are already 8 kHz and are used as is.
"""

from __future__ import annotations

import subprocess
import tempfile
import wave
from array import array
from pathlib import Path

import numpy as np

from techno_hacks.audio import decode_ulaw, linear_to_ulaw, normalize, write_wav
from techno_hacks.extract import NotUnderstood, parse_details
from techno_hacks.transcribe import Transcriber


def read_wav_mono16(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: need 16-bit PCM WAV")
        rate, channels = w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def board_quality(samples: np.ndarray, rate: int) -> array:
    """What the board would send for this audio: 8 kHz mu-law, decoded again."""
    if rate != 16000:  # the mic runs at 16 kHz; resample other inputs to that first
        n = int(len(samples) * 16000 / rate)
        samples = np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples)
    pairs = samples[: len(samples) // 2 * 2].reshape(-1, 2).mean(axis=1)
    encoded = bytes(linear_to_ulaw(int(s)) for s in np.clip(pairs, -32768, 32767))
    return decode_ulaw(encoded)


def say_to_wav(text: str, out: Path) -> Path:
    subprocess.run(["say", "--file-format=WAVE", "--data-format=LEI16@16000", "-o", str(out), text], check=True)
    return out


def check(transcriber: Transcriber, wav: Path | None = None, say: str | None = None) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        if say:
            wav = say_to_wav(say, Path(tmp) / "say.wav")
        samples, rate = read_wav_mono16(wav)
        if rate == 8000:
            audio = array("h", samples.astype(np.int16))   # already a board recording
        else:
            audio = board_quality(samples, rate)
        board_wav = write_wav(Path(tmp) / "board.wav", normalize(audio), 8000)
        print(f"audio: {len(audio) / 8000:.1f} s at 8 kHz mu-law")
        text = transcriber.transcribe(board_wav)
    print(f"heard: {text!r}")
    try:
        d = parse_details(text)
    except NotUnderstood as exc:
        print(f"NOT UNDERSTOOD: {exc}")
        return 1
    print(f"name={d.name!r} expiry={d.expiry!r} quantity={d.quantity!r}")
    return 0
