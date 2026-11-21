from array import array
from types import SimpleNamespace

import numpy as np
import pytest

from techno_hacks.audio import write_wav
from techno_hacks.transcribe import WhisperTranscriber, get_transcriber, load_wav_16k


class FakeWhisperModel:
    def __init__(self, name):
        self.name = name
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        segments = (SimpleNamespace(text=t) for t in [" Ibuprofen 400,", " expires March 2027, 20 tablets. "])
        return segments, SimpleNamespace(language="en")


def test_whisper_joins_segments_and_loads_once(tmp_path):
    wav = write_wav(tmp_path / "r.wav", array("h", [0, 1000, -1000, 0] * 2000), 8000)
    made = []
    t = WhisperTranscriber(model="tiny.en", model_factory=lambda n: made.append(FakeWhisperModel(n)) or made[-1])
    assert t.transcribe(wav) == "Ibuprofen 400, expires March 2027, 20 tablets."
    t.transcribe(wav)
    assert len(made) == 1 and made[0].name == "tiny.en"
    audio, kwargs = made[0].calls[0]
    assert audio.dtype == np.float32 and len(audio) == 16000      # 8000 samples at 8 kHz -> 1 s at 16 kHz
    assert kwargs["language"] == "en" and "tablets" in kwargs["initial_prompt"]


def test_load_wav_16k_scales_and_resamples(tmp_path):
    wav = write_wav(tmp_path / "r.wav", array("h", [16384, -16384] * 400), 8000)
    x = load_wav_16k(wav)
    assert len(x) == 1600 and x.max() == pytest.approx(0.5) and x.min() == pytest.approx(-0.5)


def test_get_transcriber_names():
    assert isinstance(get_transcriber("whisper", model="small.en"), WhisperTranscriber)
    with pytest.raises(ValueError):
        get_transcriber("nope")
