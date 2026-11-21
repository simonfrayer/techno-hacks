import base64
import wave
from array import array

import pytest

from techno_hacks.audio import AudioAssembler, decode_ulaw, linear_to_ulaw, normalize, write_wav


def test_ulaw_round_trip_is_close():
    for s in [0, 1, -1, 100, -100, 1000, -5000, 12345, -32768, 32767]:
        decoded = decode_ulaw(bytes([linear_to_ulaw(s)]))[0]
        # mu-law keeps about 3% relative error, plus a small absolute step near zero
        assert abs(decoded - max(-32635, min(32635, s))) <= max(8, abs(s) * 0.07)


def test_ulaw_known_values():
    # G.711: 0xFF and 0x7F are the two zeros, 0x00 / 0x80 the extremes
    assert decode_ulaw(bytes([0xFF, 0x7F])).tolist() == [0, 0]
    assert decode_ulaw(bytes([0x00]))[0] == -32124
    assert decode_ulaw(bytes([0x80]))[0] == 32124


def _chunks(data: bytes, size: int):
    return [base64.b64encode(data[i:i + size]).decode() for i in range(0, len(data), size)]


def test_assembler_any_order_and_missing():
    data = bytes(range(250))
    a = AudioAssembler(record_id=7, total_bytes=len(data), chunk_size=96, rate=8000)
    parts = _chunks(data, 96)
    assert a.expected_chunks == 3
    a.add(2, parts[2])
    a.add(0, parts[0])
    assert a.missing() == [1]
    with pytest.raises(ValueError):
        a.ulaw_bytes()
    a.add(1, parts[1])
    a.add(1, parts[1])  # duplicate from a resend
    a.add(9, parts[1])  # out of range: ignored
    assert a.complete
    assert a.ulaw_bytes() == data


def test_assembler_from_start_event_rejects_other_encodings():
    with pytest.raises(ValueError):
        AudioAssembler.from_start_event({"id": 1, "bytes": 10, "encoding": "pcm_s8"})


def test_normalize_and_wav(tmp_path):
    quiet = array("h", [0, 100, -200, 50])
    loud = normalize(quiet)
    assert max(abs(s) for s in loud) == int(0.9 * 32767)
    path = write_wav(tmp_path / "x" / "r.wav", loud, 8000)
    with wave.open(str(path)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (8000, 1, 2, 4)
    assert normalize(array("h", [0, 0])).tolist() == [0, 0]
