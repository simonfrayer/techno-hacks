"""SQLite storage for medicine records and climate reports."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT NOT NULL,
    device_record_id INTEGER,
    name             TEXT NOT NULL,
    expiry           TEXT NOT NULL,
    quantity         TEXT NOT NULL,
    place            TEXT NOT NULL,
    transcript       TEXT,
    audio_path       TEXT
);
CREATE TABLE IF NOT EXISTS climate (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    temp_c     REAL,
    humidity   REAL,
    status     TEXT,
    reason     TEXT,
    t_min      REAL,
    t_max      REAL,
    h_max      REAL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Record:
    name: str
    expiry: str
    quantity: str
    place: str
    device_record_id: Optional[int] = None
    transcript: Optional[str] = None
    audio_path: Optional[str] = None
    id: Optional[int] = None
    created_at: Optional[str] = None


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # the bridge is single-threaded; allow use from the thread that runs it
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def add_record(self, rec: Record) -> int:
        cur = self._db.execute(
            "INSERT INTO records (created_at, device_record_id, name, expiry, quantity, place, transcript, audio_path)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), rec.device_record_id, rec.name, rec.expiry, rec.quantity, rec.place,
             rec.transcript, rec.audio_path),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def records(self, limit: int = 50) -> list[Record]:
        rows = self._db.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [Record(**dict(r)) for r in rows]

    def add_climate(self, payload: dict) -> int:
        cur = self._db.execute(
            "INSERT INTO climate (created_at, temp_c, humidity, status, reason, t_min, t_max, h_max)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), payload.get("temp_c"), payload.get("humidity"), payload.get("status"),
             payload.get("reason"), payload.get("t_min"), payload.get("t_max"), payload.get("h_max")),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def climate(self, limit: int = 50) -> list[dict]:
        rows = self._db.execute("SELECT * FROM climate ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._db.close()
