import json
import sqlite3
import threading
from datetime import datetime, timezone


class StateDB:
    """
    SQLite-backed session, account/project, and membership persistence.
    Survives daemon crashes and server reboots.

    THREADING: check_same_thread=False because Flask handles each HTTP
    request in its own thread, and IdleMonitor/NemoSync run background
    threads. WAL mode allows concurrent readers alongside a single writer,
    and busy_timeout retries instead of immediately raising
    "database is locked". All writes are additionally serialized through
    self._lock to prevent lost-update races at the Python layer.
    """

    def __init__(self, db_path: str = ":memory:"):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._lock = threading.Lock()
        self._create_tables()

    def _create_tables(self) -> None:
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    user_id     INTEGER NOT NULL,
                    machine_id  TEXT NOT NULL,
                    project_ids TEXT NOT NULL DEFAULT '[]',
                    status      TEXT NOT NULL DEFAULT 'active',
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    PRIMARY KEY (user_id, machine_id)
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id  INTEGER PRIMARY KEY,
                    name        TEXT NOT NULL,
                    active      INTEGER NOT NULL DEFAULT 1,
                    updated_at  TEXT NOT NULL
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    project_id  INTEGER PRIMARY KEY,
                    account_id  INTEGER NOT NULL,
                    name        TEXT NOT NULL,
                    path        TEXT NOT NULL,
                    linux_group TEXT NOT NULL,
                    active      INTEGER NOT NULL DEFAULT 1,
                    updated_at  TEXT NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS memberships (
                    user_id     INTEGER NOT NULL,
                    project_id  INTEGER NOT NULL,
                    updated_at  TEXT NOT NULL,
                    PRIMARY KEY (user_id, project_id),
                    FOREIGN KEY (project_id) REFERENCES projects(project_id)
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id     INTEGER PRIMARY KEY,
                    username    TEXT NOT NULL,
                    full_name   TEXT NOT NULL DEFAULT '',
                    updated_at  TEXT NOT NULL
                )
            """)
            self._conn.commit()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------

    def open_session(self, user_id: int, machine_id: str, project_ids: list[int]) -> None:
        now = self._now()
        project_ids_json = json.dumps(project_ids)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions (user_id, machine_id, project_ids, status, created_at, updated_at)
                VALUES (?, ?, ?, 'active', ?, ?)
                ON CONFLICT(user_id, machine_id) DO UPDATE SET
                    project_ids=excluded.project_ids,
                    status='active',
                    updated_at=excluded.updated_at
                    -- created_at intentionally not updated: preserves original session start time
                """,
                (user_id, machine_id, project_ids_json, now, now),
            )
            self._conn.commit()

    def begin_unmount(self, user_id: int, machine_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status='unmounting', updated_at=? WHERE user_id=? AND machine_id=?",
                (self._now(), user_id, machine_id),
            )
            self._conn.commit()

    def close_session(self, user_id: int, machine_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status='closed', updated_at=? WHERE user_id=? AND machine_id=?",
                (self._now(), user_id, machine_id),
            )
            self._conn.commit()

    def get_session(self, user_id: int, machine_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE user_id=? AND machine_id=?",
            (user_id, machine_id),
        ).fetchone()
        return self._session_row_to_dict(row) if row else None

    def get_active_sessions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE status IN ('active', 'unmounting')"
        ).fetchall()
        return [self._session_row_to_dict(r) for r in rows]

    def all_sessions(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM sessions").fetchall()
        return [self._session_row_to_dict(r) for r in rows]

    def _session_row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["project_ids"] = json.loads(d["project_ids"])
        return d

    # ------------------------------------------------------------------
    # accounts / projects / memberships (maintained by NemoSync)
    # ------------------------------------------------------------------

    def upsert_user(self, user_id: int, username: str, full_name: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO users (user_id, username, full_name, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    full_name=excluded.full_name,
                    updated_at=excluded.updated_at
                """,
                (user_id, username, full_name, self._now()),
            )
            self._conn.commit()

    def get_username(self, user_id: int) -> str:
        row = self._conn.execute(
            "SELECT username FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        return row["username"] if row else f"u{user_id}"

    def upsert_account(self, account_id: int, name: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO accounts (account_id, name, active, updated_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    name=excluded.name,
                    updated_at=excluded.updated_at
                """,
                (account_id, name, self._now()),
            )
            self._conn.commit()

    def upsert_project(self, project_id: int, account_id: int, name: str,
                        linux_group: str, path: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO projects (project_id, account_id, name, path, linux_group, active, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    account_id=excluded.account_id,
                    name=excluded.name,
                    path=excluded.path,
                    linux_group=excluded.linux_group,
                    updated_at=excluded.updated_at
                """,
                (project_id, account_id, name, path, linux_group, self._now()),
            )
            self._conn.commit()

    def get_projects(self) -> list[dict]:
        rows = self._conn.execute("""
            SELECT projects.project_id, projects.account_id, projects.name AS project_name,
                   projects.path, projects.linux_group, projects.active, projects.updated_at,
                   accounts.name AS account_name
            FROM projects
            JOIN accounts ON accounts.account_id = projects.account_id
        """).fetchall()
        return [dict(r) for r in rows]

    def set_memberships(self, user_id: int, project_ids: list[int]) -> None:
        now = self._now()
        with self._lock:
            self._conn.execute("DELETE FROM memberships WHERE user_id=?", (user_id,))
            self._conn.executemany(
                "INSERT INTO memberships (user_id, project_id, updated_at) VALUES (?, ?, ?)",
                [(user_id, project_id, now) for project_id in project_ids],
            )
            self._conn.commit()

    def get_memberships(self, user_id: int) -> list[dict]:
        rows = self._conn.execute("""
            SELECT projects.project_id, projects.account_id,
                   projects.name AS project_name, accounts.name AS account_name
            FROM memberships
            JOIN projects ON projects.project_id = memberships.project_id
            JOIN accounts ON accounts.account_id = projects.account_id
            WHERE memberships.user_id=?
        """, (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_memberships_for_project(self, project_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT user_id, project_id FROM memberships WHERE project_id=?",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]
