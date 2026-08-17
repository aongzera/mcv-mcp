"""SQLite snapshot of MCV content, plus the diff log that powers `whats_new`.

MCV does not reliably expose a "posted at" timestamp, so `first_seen` - when this server
first observed a row - is the proxy for "newly uploaded". The very first sync therefore
backfills everything silently instead of reporting every existing item as new.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

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
    ok          INTEGER,
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
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

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
        self._conn.commit()

    @property
    def is_first_sync(self) -> bool:
        return self.get_meta("initial_sync_done") != "1"

    def mark_initial_sync_done(self) -> None:
        self.set_meta("initial_sync_done", "1")

    # ------------------------------------------------------------------ upsert

    def upsert(
        self,
        table: str,
        rows: Iterable[dict[str, Any]],
        *,
        record_events: bool = True,
    ) -> dict[str, int]:
        """Insert/update rows and log new/changed events. Returns counts."""
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

        self._conn.commit()
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
        self._conn.commit()

    def unnotified_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM events WHERE notified = 0 ORDER BY detected_at LIMIT ?", (limit,)
        )

    # -------------------------------------------------------------- sync runs

    def start_sync(self) -> int:
        cur = self._conn.execute(
            "INSERT INTO sync_runs(started_at, ok) VALUES(?, 0)", (utcnow(),)
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def finish_sync(
        self, run_id: int, ok: bool, error: str | None = None, counts: dict | None = None
    ) -> None:
        self._conn.execute(
            "UPDATE sync_runs SET finished_at = ?, ok = ?, error = ?, counts = ? WHERE id = ?",
            (utcnow(), 1 if ok else 0, error, json.dumps(counts or {}), run_id),
        )
        self._conn.commit()

    def last_sync(self) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def set_material_local_path(self, material_id: str, path: str) -> None:
        self._conn.execute(
            "UPDATE materials SET local_path = ? WHERE id = ?", (path, material_id)
        )
        self._conn.commit()
