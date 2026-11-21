"""Command line: ``techno-hacks bridge`` and ``techno-hacks records``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

DEFAULT_DB = Path("data/medtracker.sqlite3")
DEFAULT_RECORDINGS = Path("data/recordings")
DEFAULT_CONFIG = Path("medtracker.toml")


def _cmd_bridge(args: argparse.Namespace) -> int:
    from atech import Board, NoBoardFoundError, PortUnavailableError

    from techno_hacks.backend import Backend
    from techno_hacks.bridge import Bridge, Settings
    from techno_hacks.store import Store
    from techno_hacks.transcribe import get_transcriber

    settings = Settings.load(Path(args.config))
    kwargs = {"model": args.whisper_model} if args.transcriber == "whisper" else {}
    transcriber = get_transcriber(args.transcriber, **kwargs)
    if hasattr(transcriber, "load"):
        transcriber.load()   # download/load now, not on the first recording
    backend = Backend(Store(args.db), transcriber, Path(args.recordings))
    try:
        board = Board.connect(port=args.port)
    except (NoBoardFoundError, PortUnavailableError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"bridge running on {args.port or 'auto-detected port'} (Ctrl-C to stop)")
    with board:
        try:
            Bridge(board, backend, settings).run(board.events())
        except KeyboardInterrupt:
            pass
    return 0


def _cmd_stt_check(args: argparse.Namespace) -> int:
    from techno_hacks.sttcheck import check
    from techno_hacks.transcribe import WhisperTranscriber

    if not args.wav and not args.say:
        print("error: give a WAV file or --say TEXT", file=sys.stderr)
        return 2
    transcriber = WhisperTranscriber(model=args.whisper_model)
    status = 0
    for wav in args.wav or [None]:
        if wav:
            print(f"--- {wav}")
        status |= check(transcriber, wav=Path(wav) if wav else None, say=args.say)
    return status


def _cmd_records(args: argparse.Namespace) -> int:
    from techno_hacks.store import Store

    store = Store(args.db)
    rows = store.records(limit=args.limit)
    if not rows:
        print("no records yet")
    for r in rows:
        print(f"{r.id:>4}  {r.created_at}  {r.name:<24} exp {r.expiry:<10} qty {r.quantity:<5} @ {r.place}")
    if args.climate:
        print()
        for c in store.climate(limit=args.limit):
            print(f"{c['created_at']}  {c['temp_c']} C  {c['humidity']} %  {c['status']} ({c['reason']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="techno-hacks", description="Medicine intake + storage monitor")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("bridge", help="connect to the board on USB and run the backend")
    p.add_argument("--port", default=None, help="serial port (auto-detected if omitted)")
    p.add_argument("--transcriber", default="whisper",
                   help="speech-to-text: whisper | manual | fixed (default: whisper)")
    p.add_argument("--whisper-model", default="base.en",
                   help="tiny.en | base.en | small.en | medium.en (default: base.en)")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--recordings", default=str(DEFAULT_RECORDINGS))
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="TOML with a [device] table")
    p.set_defaults(func=_cmd_bridge)

    p = sub.add_parser("stt-check", help="test Whisper + extraction on a WAV or a spoken sentence (no board)")
    p.add_argument("wav", nargs="*", help="16-bit WAV files, e.g. data/recordings/<id>.wav")
    p.add_argument("--say", help="speak this with macOS `say` and transcribe it")
    p.add_argument("--whisper-model", default="base.en")
    p.set_defaults(func=_cmd_stt_check)

    p = sub.add_parser("records", help="list stored records")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--climate", action="store_true", help="also list climate reports")
    p.set_defaults(func=_cmd_records)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    # third-party chatter: per-request HTTP lines, the HF token hint, VAD details
    for noisy in ("httpx", "httpcore", "huggingface_hub", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if args.verbose else logging.ERROR)
    if not args.verbose:
        import warnings

        warnings.filterwarnings("ignore", message=".*unauthenticated requests to the HF Hub.*")
    return args.func(args)
