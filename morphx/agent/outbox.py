"""Where the device keeps its measurements until the server has them."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    sequence     INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL UNIQUE,
    device_id    TEXT NOT NULL,
    sample_id    TEXT NOT NULL,
    measured_at  TEXT NOT NULL,
    measurements TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'synced', 'rejected')),
    error        TEXT
);
CREATE INDEX IF NOT EXISTS records_pending ON records (sequence) WHERE status = 'pending';
"""


class Outbox:
    """Measurements in a SQLite table, each marked pending, synced or rejected.

    `sequence` is AUTOINCREMENT, so it is handed out by the same statement that saves the
    record and keeps counting after a restart or crash.
    """

    def __init__(self, path: str | Path, device_id: str):
        self.path = Path(path)
        self.device_id = device_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        # a fresh connection per call keeps the measuring and syncing threads apart
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = FULL")  # an acknowledged write survives power loss
        try:
            yield db
        finally:
            db.close()

    def add(self, sample: dict) -> int:
        """Store a new measurement and return the sequence number it was given."""
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO records (event_id, device_id, sample_id, measured_at, measurements)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    sample["event_id"],
                    self.device_id,
                    sample["sample_id"],
                    sample["measured_at"],
                    json.dumps(sample["measurements"]),
                ),
            )
            return cursor.lastrowid

    def oldest_pending(self) -> dict | None:
        """The next record to send, shaped the way the server expects it."""
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM records WHERE status = 'pending' ORDER BY sequence LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return {
            "event_id": row["event_id"],
            "device_id": row["device_id"],
            "sample_id": row["sample_id"],
            "sequence": row["sequence"],
            "measured_at": row["measured_at"],
            "measurements": json.loads(row["measurements"]),
        }

    def mark(self, sequence: int, status: str, error: str | None = None) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE records SET status = ?, error = ? WHERE sequence = ?",
                (status, error, sequence),
            )

    def counts(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute("SELECT status, COUNT(*) FROM records GROUP BY status").fetchall()
        counts = {"pending": 0, "synced": 0, "rejected": 0}
        counts.update({status: n for status, n in rows})
        return counts
