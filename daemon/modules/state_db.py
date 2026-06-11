import sqlite3
import threading
from datetime import datetime, timezone


class StateDB:
    """
    SQLite-backed session persistence.
    Survives daemon crashes and server reboots.
    WAL mode + threading.Lock for safe concurrent access from Flask worker threads.
    """

    def __init__(self, db_path: str = ":memory:"):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._lock = threading.Lock()
        self._create_table()

    def _create_table(self) -> None:
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    user_id     TEXT NOT NULL,
                    machine_id  TEXT NOT NULL,
                    group_id    TEXT NOT NULL DEFAULT '',
                    status      TEXT NOT NULL DEFAULT 'active',
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    PRIMARY KEY (user_id, machine_id)
                )
            """)
            self._conn.commit()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def open_session(self, user_id: str, machine_id: str, group_id: str = "") -> None:
        now = self._now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions (user_id, machine_id, group_id, status, created_at, updated_at)
                VALUES (?, ?, ?, 'active', ?, ?)
                ON CONFLICT(user_id, machine_id) DO UPDATE SET
                    group_id=excluded.group_id,
                    status='active',
                    updated_at=excluded.updated_at
                    -- created_at intentionally not updated: preserves original session start time
                """,
                (user_id, machine_id, group_id, now, now),
            )
            self._conn.commit()

    def begin_unmount(self, user_id: str, machine_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status='unmounting', updated_at=? WHERE user_id=? AND machine_id=?",
                (self._now(), user_id, machine_id),
            )
            self._conn.commit()

    def close_session(self, user_id: str, machine_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status='closed', updated_at=? WHERE user_id=? AND machine_id=?",
                (self._now(), user_id, machine_id),
            )
            self._conn.commit()

    def get_session(self, user_id: str, machine_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE user_id=? AND machine_id=?",
            (user_id, machine_id),
        ).fetchone()
        return dict(row) if row else None

    def get_active_sessions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE status IN ('active', 'unmounting')"
        ).fetchall()
        return [dict(r) for r in rows]

    def all_sessions(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM sessions").fetchall()
        return [dict(r) for r in rows]
