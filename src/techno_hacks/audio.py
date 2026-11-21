"""Audio from the board: reassemble the chunks, decode G.711 mu-law, write WAV.

The firmware sends one recording as ``audio_start``, many ``audio`` chunks
(base64, ``chunk`` bytes each, numbered by ``seq``) and ``audio_end``.
"""

from __future__ import annotations

import base64
import wave
from array import array
from pathlib import Path


def _ulaw_to_linear(u: int) -> int:
    u = ~u & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return -sample if sign else sample


ULAW_TABLE: tuple[int, ...] = tuple(_ulaw_to_linear(i) for i in range(256))


def linear_to_ulaw(sample: int) -> int:
    """Encode one 16-bit sample (mirror of the firmware's linearToUlaw, for tests)."""
    bias, clip = 0x84, 32635
    sign = 0
    if sample < 0:
        sign, sample = 0x80, -sample
    sample = min(sample, clip) + bias
    exponent = 7
    mask = 0x4000
    while (sample & mask) == 0 and exponent > 0:
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def decode_ulaw(data: bytes) -> array:
    """mu-law bytes -> signed 16-bit samples."""
    return array("h", (ULAW_TABLE[b] for b in data))


def normalize(samples: array, peak: float = 0.9) -> array:
    """Scale so the loudest sample sits at ``peak`` of full scale (helps quiet mics)."""
    loudest = max((abs(s) for s in samples), default=0)
    if loudest == 0:
        return array("h", samples)
    gain = peak * 32767 / loudest
    return array("h", (max(-32768, min(32767, int(s * gain))) for s in samples))


def write_wav(path: Path, samples: array, rate: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return path


class AudioAssembler:
    """Collects the chunks of one recording, in any order, duplicates ignored."""

    def __init__(self, record_id: int, total_bytes: int, chunk_size: int, rate: int, encoding: str = "mulaw"):
        if encoding != "mulaw":
            raise ValueError(f"unsupported audio encoding {encoding!r}")
        self.record_id = record_id
        self.total_bytes = total_bytes
        self.chunk_size = chunk_size
        self.rate = rate
        self._chunks: dict[int, bytes] = {}

    @classmethod
    def from_start_event(cls, value: dict) -> "AudioAssembler":
        return cls(
            record_id=int(value["id"]),
            total_bytes=int(value["bytes"]),
            chunk_size=int(value.get("chunk", 96)),
            rate=int(value.get("rate", 8000)),
            encoding=str(value.get("encoding", "mulaw")),
        )

    @property
    def expected_chunks(self) -> int:
        return -(-self.total_bytes // self.chunk_size)

    def add(self, seq: int, b64: str) -> None:
        if 0 <= seq < self.expected_chunks:
            self._chunks[seq] = base64.b64decode(b64)

    def missing(self) -> list[int]:
        return [s for s in range(self.expected_chunks) if s not in self._chunks]

    @property
    def complete(self) -> bool:
        return not self.missing()

    def ulaw_bytes(self) -> bytes:
        if not self.complete:
            raise ValueError(f"recording {self.record_id} is missing chunks {self.missing()[:10]}")
        return b"".join(self._chunks[s] for s in range(self.expected_chunks))[: self.total_bytes]

    def samples(self) -> array:
        return decode_ulaw(self.ulaw_bytes())

    @property
    def seconds(self) -> float:
        return self.total_bytes / self.rate
