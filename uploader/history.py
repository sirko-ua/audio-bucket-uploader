"""Durable run history and failure log.

A restarted run must not redo work: re-extracting tracks and re-asking the
server whether a hash is already published is expensive for us and rude to the
server. Every source file (and every track inside it) therefore gets a row
keyed by its path plus a size+mtime fingerprint, so an edited or replaced file
is reprocessed while an untouched one is skipped without touching disk or
network.

Statuses:
  uploaded   track was accepted by the server
  duplicate  server's hash-check reported it as already published
  skipped    deliberately not uploadable (no language, no source video, ...)
  done       whole source file finished (all its tracks reached a final state)
  failed     something broke; retried on the next run until MAX_ATTEMPTS

Nothing in here may raise: the history exists to make the run survivable, and
it is written from inside the very handlers that keep the run alive. A history
that cannot be written degrades the run to "no resume", never to "no run".
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .logs import log_event

MAX_ATTEMPTS = 3
DATABASE_NAME = "history.sqlite3"

# item key of the row that stands for the whole source file
FILE_ITEM = "*"

FINAL = "final"
GIVEN_UP = "given_up"

FINAL_STATUSES = {"uploaded", "duplicate", "skipped", "done"}
# skipped/done depend on which languages were requested; an uploaded or duplicate
# track is on the server regardless, so only these expire on a filter change.
FILTER_DEPENDENT_STATUSES = {"skipped", "done"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    path        TEXT NOT NULL,
    item        TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    filters     TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    attempts    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (path, item)
)
"""

UPSERT = """
INSERT INTO entries (path, item, fingerprint, filters, status, detail, attempts, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(path, item) DO UPDATE SET
    fingerprint = excluded.fingerprint,
    filters = excluded.filters,
    status = excluded.status,
    detail = excluded.detail,
    attempts = excluded.attempts,
    updated_at = excluded.updated_at
"""


def file_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def storable(value: object) -> str:
    """Text sqlite and json can always encode.

    A media file whose name holds a byte that is not valid UTF-8 (a cp1252 name
    off a NAS) reaches us as a lone surrogate, and sqlite raises UnicodeEncodeError
    on it — a ValueError, not a sqlite3.Error, so it slips past the guards and
    escapes from inside the very handler that records failures. The file could
    then never even be recorded as failed, and the whole library stopped.
    """
    text = os.fsdecode(value) if isinstance(value, (Path, os.PathLike)) else str(value)
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class History:
    def __init__(self, state_dir: Path, filters: str) -> None:
        self.state_dir = state_dir
        self.filters = storable(filters)
        self.failures_path = state_dir / "failures.jsonl"
        self._connection: sqlite3.Connection | None = None
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            self._connection = self._connect(state_dir / DATABASE_NAME)
        except (OSError, sqlite3.Error) as exc:
            log_event(
                "WARNING", "history", "open",
                f"cannot use {state_dir / DATABASE_NAME} ({exc}); "
                "continuing without resume, nothing will be skipped",
            )

    def _connect(self, database_path: Path) -> sqlite3.Connection:
        try:
            return self._open(database_path)
        except sqlite3.OperationalError:
            # A full disk, a read-only mount, a locked file: the database is
            # fine, the environment is not. Destroying a healthy history here
            # would throw away the whole point of it. Let the caller degrade to
            # "no resume" instead.
            raise
        except sqlite3.DatabaseError as exc:
            # Genuine corruption ("file is not a database", "disk image is
            # malformed"): a half-written file from a yanked drive would fail
            # identically on every future run until a human deletes it. Start
            # over instead; the cost is re-doing work, never losing it.
            quarantine = database_path.with_name(database_path.name + ".corrupt")
            quarantine.unlink(missing_ok=True)
            database_path.replace(quarantine)
            log_event(
                "WARNING", "history", "open",
                f"{database_path} is corrupt ({exc}); moved it to {quarantine.name} and started fresh",
            )
            return self._open(database_path)

    @staticmethod
    def _open(database_path: Path) -> sqlite3.Connection:
        # isolation_level=None: autocommit, so a kill -9 keeps everything already
        # recorded. timeout: two uploaders sharing a state dir wait instead of failing.
        connection = sqlite3.connect(database_path, timeout=30.0, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(SCHEMA)
        except BaseException:
            # connect() opens lazily, so a bad file only fails here — with the
            # handle still held. On Windows that would block the quarantine.
            connection.close()
            raise
        return connection

    def check(self, path: Path, item: str, fingerprint: str) -> tuple[str, str] | None:
        """(FINAL | GIVEN_UP, reason) when this item needs no work, else None.

        GIVEN_UP is not success: it is a failure the run has stopped retrying,
        and the caller must report it as such.
        """
        if self._connection is None:
            return None
        try:
            row = self._connection.execute(
                "SELECT status, detail, attempts, filters FROM entries "
                "WHERE path = ? AND item = ? AND fingerprint = ?",
                (storable(path), storable(item), fingerprint),
            ).fetchone()
        except (sqlite3.Error, UnicodeError) as exc:
            log_event("WARNING", "history", "read", f"{storable(path.name)}: {exc}")
            return None
        if row is None:
            return None

        status, detail, attempts, filters = row
        if status == "failed":
            if attempts < MAX_ATTEMPTS:
                return None
            return (
                GIVEN_UP,
                f"failed {attempts} time(s) in earlier runs, not retried again "
                f"(last error: {detail}). Re-run with --retry-failed to try once more.",
            )
        if status not in FINAL_STATUSES:
            return None
        if status in FILTER_DEPENDENT_STATUSES and filters != self.filters:
            return None
        return (FINAL, f"{status} in an earlier run" + (f" ({detail})" if detail else ""))

    def mark(self, path: Path, item: str, fingerprint: str, status: str, detail: str = "") -> None:
        if self._connection is None:
            return
        key = storable(path)
        try:
            attempts = 0
            if status == "failed":
                previous = self._connection.execute(
                    "SELECT attempts FROM entries WHERE path = ? AND item = ? AND fingerprint = ?",
                    (key, storable(item), fingerprint),
                ).fetchone()
                attempts = (previous[0] if previous else 0) + 1
            self._connection.execute(
                UPSERT,
                (key, storable(item), fingerprint, self.filters, status, storable(detail), attempts, _now()),
            )
        except (sqlite3.Error, UnicodeError) as exc:
            # Called from inside the handlers that keep the run alive: a full
            # disk or a read-only state dir must not sink it. Losing the row
            # costs one re-extract next run, and the server's hash check dedupes.
            log_event("WARNING", "history", "write", f"{storable(path.name)}: {exc}")

    def record_failure(
        self, path: Path, item: str, fingerprint: str, stage: str, error: str
    ) -> None:
        self.mark(path, item, fingerprint, "failed", error)
        record = {
            "time": _now(),
            "path": storable(path),
            "item": storable(item),
            "stage": stage,
            "error": storable(error),
        }
        try:
            with self.failures_path.open("a", encoding="utf-8", errors="backslashreplace") as log_file:
                log_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        except (OSError, UnicodeError) as exc:
            log_event("WARNING", "history", "write", f"cannot append to {self.failures_path}: {exc}")

    def clear_failures(self) -> int:
        """Forget every failure, so files given up on are attempted again."""
        if self._connection is None:
            return 0
        try:
            cursor = self._connection.execute("DELETE FROM entries WHERE status = 'failed'")
            return cursor.rowcount or 0
        except sqlite3.Error as exc:
            log_event("WARNING", "history", "write", f"cannot clear failures: {exc}")
            return 0

    def close(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.close()
        except sqlite3.Error:
            pass
