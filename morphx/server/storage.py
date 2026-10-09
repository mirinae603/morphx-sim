"""Server-side storage: one SQLite table holding every record ever received."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from morphx.models import Record, utc_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,  -- arrival order, never used for reads
    event_id        TEXT NOT NULL UNIQUE,               -- makes retries harmless
    device_id       TEXT NOT NULL,
    sample_id       TEXT NOT NULL,
    sequence        INTEGER NOT NULL,
    measured_at     TEXT NOT NULL,                      -- device clock, UTC
    received_at     TEXT NOT NULL,                      -- server clock, UTC
    wbc_10e3_per_ul REAL NOT NULL,
    rbc_10e6_per_ul REAL NOT NULL,
    hb_g_per_dl     REAL NOT NULL,
    UNIQUE (device_id, sequence)                        -- also serves the read query
);
"""


class SequenceConflict(Exception):
    """The device already has a different record stored under this sequence number."""


class Storage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        # A connection per call: the web framework serves requests from several threads.
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = FULL")  # once we acknowledge, the record is on disk
        try:
            yield db
        finally:
            db.close()

    def add(self, record: Record) -> bool:
        """Store a record. Returns False if this event_id was already stored (a retry)."""
        measurements = record.measurements
        try:
            with self._connect() as db:
                cursor = db.execute(
                    "INSERT INTO records (event_id, device_id, sample_id, sequence, measured_at,"
                    " received_at, wbc_10e3_per_ul, rbc_10e6_per_ul, hb_g_per_dl)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (event_id) DO NOTHING",
                    (
                        str(record.event_id),
                        record.device_id,
                        record.sample_id,
                        record.sequence,
                        utc_iso(record.measured_at),
                        utc_iso(),
                        measurements.wbc.value,
                        measurements.rbc.value,
                        measurements.hb.value,
                    ),
                )
        except sqlite3.IntegrityError as exc:  # only (device_id, sequence) can still collide
            raise SequenceConflict(
                f"{record.device_id} already has a different record with sequence {record.sequence}"
            ) from exc
        return cursor.rowcount == 1

    def for_device(self, device_id: str, after_sequence: int, limit: int) -> list[dict]:
        """A device's records ordered by sequence, after the given cursor."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM records WHERE device_id = ? AND sequence > ?"
                " ORDER BY sequence LIMIT ?",
                (device_id, after_sequence, limit),
            ).fetchall()
        return [_as_dict(row) for row in rows]

    def recent(self, limit: int, device_id: str | None = None) -> list[dict]:
        """The latest records in arrival order, newest first (for the dashboard)."""
        where, args = ("WHERE device_id = ?", (device_id,)) if device_id else ("", ())
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM records {where} ORDER BY id DESC LIMIT ?", (*args, limit)
            ).fetchall()
        return [_as_dict(row) for row in rows]

    def devices(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT device_id, COUNT(*) AS n, MIN(sequence) AS first, MAX(sequence) AS last,"
                " MAX(received_at) AS last_received FROM records GROUP BY device_id"
                " ORDER BY device_id"
            ).fetchall()
        return [
            {
                "device_id": row["device_id"],
                "count": row["n"],
                "first_sequence": row["first"],
                "last_sequence": row["last"],
                "missing": row["last"] - row["first"] + 1 - row["n"],
                "last_received_at": row["last_received"],
            }
            for row in rows
        ]


def _as_dict(row: sqlite3.Row) -> dict:
    return {
        "arrival": row["id"],
        "event_id": row["event_id"],
        "device_id": row["device_id"],
        "sample_id": row["sample_id"],
        "sequence": row["sequence"],
        "measured_at": row["measured_at"],
        "received_at": row["received_at"],
        "measurements": {
            "wbc": {"value": row["wbc_10e3_per_ul"], "unit": "10^3/uL"},
            "rbc": {"value": row["rbc_10e6_per_ul"], "unit": "10^6/uL"},
            "hb": {"value": row["hb_g_per_dl"], "unit": "g/dL"},
        },
    }
