"""SQLite snapshot of MCV content, plus the diff log that powers `whats_new`.

MCV does not reliably expose a "posted at" timestamp, so `first_seen` - when this server
first observed a row - is the proxy for "newly uploaded". The very first sync therefore
backfills everything silently instead of reporting every existing item as new.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    cv_cid      TEXT PRIMARY KEY,
    course_no   TEXT,
    title       TEXT,
    yearsem     TEXT,
    raw         TEXT,
    content_hash TEXT,
    first_seen  TEXT,
    last_seen   TEXT,
    last_changed TEXT
);

CREATE TABLE IF NOT EXISTS assignments (
    id          TEXT PRIMARY KEY,   -- '<cv_cid>:<item_id>'
    cv_cid      TEXT,
    item_id     TEXT,
    title       TEXT,
    description TEXT,
    due_at      TEXT,               -- ISO-8601, may be NULL
    due_text    TEXT,               -- original string as MCV showed it
    url         TEXT,
    content_hash TEXT,
    first_seen  TEXT,
    last_seen   TEXT,
    last_changed TEXT
);

CREATE TABLE IF NOT EXISTS materials (
    id          TEXT PRIMARY KEY,   -- '<cv_cid>:<folder>:<name>'
    cv_cid      TEXT,
    folder      TEXT,
    name        TEXT,
    url         TEXT,
    local_path  TEXT,
    content_hash TEXT,
    first_seen  TEXT,
    last_seen   TEXT,
    last_changed TEXT
);

CREATE TABLE IF NOT EXISTS grades (
    id          TEXT PRIMARY KEY,   -- '<cv_cid>:<item_id>'
    cv_cid      TEXT,
    item_id     TEXT,
    item_title  TEXT,
    score       TEXT,
    max_score   TEXT,
    content_hash TEXT,
    first_seen  TEXT,
    last_seen   TEXT,
    last_changed TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,      -- assignment_new | grade_new | ...
    ref_id      TEXT NOT NULL,
    cv_cid      TEXT,
    summary     TEXT,
    payload     TEXT,
    detected_at TEXT NOT NULL,
    notified    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    ok          INTEGER,             -- kept for old readers; `status` is authoritative
    status      TEXT,                -- running | ok | error | stale
    error       TEXT,
    counts      TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_detected ON events(detected_at);
CREATE INDEX IF NOT EXISTS idx_assignments_course ON assignments(cv_cid);
CREATE INDEX IF NOT EXISTS idx_materials_course ON materials(cv_cid);
CREATE INDEX IF NOT EXISTS idx_grades_course ON grades(cv_cid);
"""

# Fields that make up a row's identity for change detection. Timestamps are excluded so
# re-observing an unchanged item is not treated as an edit.
_TRACKED_FIELDS: dict[str, Sequence[str]] = {
    "courses": ("course_no", "title", "yearsem"),
    "assignments": ("title", "description", "due_at", "due_text", "url"),
    "materials": ("folder", "name", "url"),
    "grades": ("item_title", "score", "max_score"),
}

# `courses` is keyed by MCV's own course id; the other tables use a synthetic '<cv_cid>:<x>'.
_KEY_COLUMN: dict[str, str] = {"courses": "cv_cid"}

_NEW_EVENT = {
    "assignments": "assignment_new",
    "materials": "material_new",
    "grades": "grade_new",
    "courses": "course_new",
}
_CHANGED_EVENT = {
    "assignments": "assignment_changed",
    "materials": "material_changed",
    "grades": "grade_changed",
    "courses": "course_changed",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(row: dict[str, Any], fields: Sequence[str]) -> str:
    blob = json.dumps({f: row.get(f) for f in fields}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class Store:
    """Snapshot store, safe to share between the MCP tool threads and the poller.

    Each thread gets its own SQLite connection (WAL mode), so one thread's transaction
    can never be committed or observed half-done by another. Writes that must be atomic
    as a group go through explicit BEGIN IMMEDIATE ... COMMIT; everything else is a
    single autocommitted statement.
    """

    # A 'running' sync run older than this is presumed dead (process killed mid-run).
    RUNNING_GRACE = timedelta(minutes=30)

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()

        conn = self._conn
        conn.executescript(SCHEMA)
        self._migrate(conn)

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # isolation_level=None -> autocommit; transactions are managed explicitly.
            conn = sqlite3.connect(
                self._path, timeout=10, isolation_level=None, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            with self._conns_lock:
                self._all_conns.append(conn)
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sync_runs)")}
        if "status" not in cols:
            conn.execute("ALTER TABLE sync_runs ADD COLUMN status TEXT")
            # Rows from before the status column: a null finished_at means the run was
            # never finalized, which we can no longer distinguish from crashed - 'stale'.
            conn.execute(
                "UPDATE sync_runs SET status = CASE "
                "WHEN finished_at IS NULL THEN 'stale' "
                "WHEN ok = 1 THEN 'ok' ELSE 'error' END"
            )

    def close(self) -> None:
        with self._conns_lock:
            for conn in self._all_conns:
                conn.close()
            self._all_conns.clear()
        self._local = threading.local()

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @property
    def is_first_sync(self) -> bool:
        return self.get_meta("initial_sync_done") != "1"

    def mark_initial_sync_done(self) -> None:
        self.set_meta("initial_sync_done", "1")

    # ------------------------------------------------------------------ upsert

    def apply_sync(
        self,
        tables: dict[str, Iterable[dict[str, Any]]],
        *,
        record_events: bool = True,
        mark_initial_done: bool = True,
    ) -> dict[str, dict[str, int]]:
        """Apply a whole sync's rows in ONE transaction.

        Readers on other connections see either the previous complete snapshot or the
        new one - never a half-applied sync (WAL readers do not observe uncommitted
        writes). Returns per-table counts.
        """
        conn = self._conn
        counts: dict[str, dict[str, int]] = {}
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table, rows in tables.items():
                counts[table] = self._upsert_rows(table, rows, record_events=record_events)
            if mark_initial_done:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('initial_sync_done', '1') "
                    "ON CONFLICT(key) DO UPDATE SET value = '1'"
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return counts

    def _upsert_rows(
        self,
        table: str,
        rows: Iterable[dict[str, Any]],
        *,
        record_events: bool = True,
    ) -> dict[str, int]:
        """Insert/update rows and log new/changed events. Returns counts.

        Runs inside the caller's transaction (apply_sync); does not commit.
        """
        fields = _TRACKED_FIELDS[table]
        key = _KEY_COLUMN.get(table, "id")
        now = utcnow()
        added = changed = unchanged = 0

        for row in rows:
            row_id = str(row[key])
            digest = _hash(row, fields)
            existing = self._conn.execute(
                f"SELECT content_hash FROM {table} WHERE {key} = ?", (row_id,)
            ).fetchone()

            columns = [c for c in row if c != key]
            if existing is None:
                placeholders = ", ".join("?" for _ in range(len(columns) + 5))
                self._conn.execute(
                    f"INSERT INTO {table} ({key}, {', '.join(columns)}, content_hash, "
                    f"first_seen, last_seen, last_changed) VALUES ({placeholders})",
                    [row_id, *(row[c] for c in columns), digest, now, now, now],
                )
                added += 1
                if record_events:
                    self._log_event(_NEW_EVENT[table], row_id, row, now)
            elif existing["content_hash"] != digest:
                assignments = ", ".join(f"{c} = ?" for c in columns)
                self._conn.execute(
                    f"UPDATE {table} SET {assignments}, content_hash = ?, "
                    f"last_seen = ?, last_changed = ? WHERE {key} = ?",
                    [*(row[c] for c in columns), digest, now, now, row_id],
                )
                changed += 1
                if record_events:
                    self._log_event(_CHANGED_EVENT[table], row_id, row, now)
            else:
                self._conn.execute(
                    f"UPDATE {table} SET last_seen = ? WHERE {key} = ?", (now, row_id)
                )
                unchanged += 1

        return {"added": added, "changed": changed, "unchanged": unchanged}

    def _log_event(self, kind: str, ref_id: str, row: dict[str, Any], when: str) -> None:
        title = row.get("title") or row.get("name") or row.get("item_title") or ref_id
        if row.get("item_title"):
            # For grades the score is the point of the event, so put it in the summary.
            score = row.get("score")
            max_score = row.get("max_score")
            if score:
                title = f"{title}: {score}" + (f" / {max_score}" if max_score else "")
        self._conn.execute(
            "INSERT INTO events(kind, ref_id, cv_cid, summary, payload, detected_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (
                kind,
                ref_id,
                row.get("cv_cid"),
                str(title),
                json.dumps(row, ensure_ascii=False, default=str),
                when,
            ),
        )

    # ------------------------------------------------------------------ reads

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def events_since(
        self, since_iso: str, kinds: Sequence[str] | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT e.*, c.title AS course_title, c.course_no "
            "FROM events e LEFT JOIN courses c ON c.cv_cid = e.cv_cid "
            "WHERE e.detected_at >= ?"
        )
        params: list[Any] = [since_iso]
        if kinds:
            sql += f" AND e.kind IN ({', '.join('?' for _ in kinds)})"
            params.extend(kinds)
        sql += " ORDER BY e.detected_at DESC, e.id DESC LIMIT ?"
        params.append(limit)
        return self.query(sql, params)

    def mark_notified(self, event_ids: Sequence[int]) -> None:
        if not event_ids:
            return
        self._conn.execute(
            f"UPDATE events SET notified = 1 WHERE id IN "
            f"({', '.join('?' for _ in event_ids)})",
            list(event_ids),
        )

    def unnotified_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM events WHERE notified = 0 ORDER BY detected_at LIMIT ?", (limit,)
        )

    # -------------------------------------------------------------- sync runs

    def start_sync(self) -> int:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            # A run still 'running' when a new one starts was interrupted before it
            # could finalize (killed process). Label it so callers can tell it apart
            # from an actually-running sync. If it IS still alive, its own finish_sync
            # will overwrite this with the real outcome.
            conn.execute(
                "UPDATE sync_runs SET status = 'stale', "
                "error = COALESCE(error, 'never finalized (interrupted?)') "
                "WHERE status = 'running'"
            )
            cur = conn.execute(
                "INSERT INTO sync_runs(started_at, ok, status) VALUES(?, 0, 'running')",
                (utcnow(),),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return int(cur.lastrowid or 0)

    def finish_sync(
        self, run_id: int, status: str, error: str | None = None, counts: dict | None = None
    ) -> None:
        """Finalize a run. `status` is 'ok' or 'error'; the row always gets finished_at."""
        cur = self._conn.execute(
            "UPDATE sync_runs SET finished_at = ?, ok = ?, status = ?, error = ?, "
            "counts = ? WHERE id = ?",
            (utcnow(), 1 if status == "ok" else 0, status, error, json.dumps(counts or {}), run_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError(
                f"finish_sync matched {cur.rowcount} rows for run {run_id}; "
                "the run record was not finalized"
            )

    def last_sync(self) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def sync_status(self) -> dict[str, Any]:
        """Freshness info for read responses: is a sync running, and when one last completed."""
        last = self.last_sync()
        in_progress = False
        if last and last.get("status") == "running":
            try:
                started = datetime.fromisoformat(str(last["started_at"]))
                in_progress = datetime.now(timezone.utc) - started < self.RUNNING_GRACE
            except (TypeError, ValueError):
                in_progress = True
        completed = self.query(
            "SELECT finished_at FROM sync_runs WHERE status = 'ok' ORDER BY id DESC LIMIT 1"
        )
        return {
            "sync_in_progress": in_progress,
            "last_completed_sync": completed[0]["finished_at"] if completed else None,
        }

    def set_material_local_path(self, material_id: str, path: str) -> None:
        self._conn.execute(
            "UPDATE materials SET local_path = ? WHERE id = ?", (path, material_id)
        )
