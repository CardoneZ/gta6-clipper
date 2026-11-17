"""
Tenancy database: users, API keys, memberships, uploads, jobs, webhook idempotency.

Portable by construction: TEXT ids (random hex), INTEGER epoch-second timestamps, no
AUTOINCREMENT, no JSON column types, `?` placeholders. `Database` speaks SQLite through the
standard library; for PostgreSQL, set `DATABASE_URL=postgresql://...` and install `psycopg`
(v3): placeholders are rewritten to `%s` and the same DDL runs unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterable

SCHEMA_VERSION = 1

DDL = (
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        is_admin INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS api_keys (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id),
        prefix TEXT NOT NULL UNIQUE,
        key_hash TEXT NOT NULL,
        label TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        last_used_at INTEGER,
        revoked_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS memberships (
        user_id TEXT PRIMARY KEY REFERENCES users(id),
        plan TEXT NOT NULL,
        status TEXT NOT NULL,
        price_cents INTEGER NOT NULL,
        currency TEXT NOT NULL,
        current_period_end INTEGER NOT NULL,
        provider TEXT NOT NULL,
        provider_ref TEXT,
        updated_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS uploads (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id),
        filename TEXT NOT NULL,
        total_bytes INTEGER NOT NULL,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id),
        upload_id TEXT NOT NULL REFERENCES uploads(id),
        status TEXT NOT NULL,
        progress REAL NOT NULL DEFAULT 0,
        stage TEXT NOT NULL,
        options_json TEXT NOT NULL,
        result_json TEXT,
        error TEXT,
        created_at INTEGER NOT NULL,
        finished_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS webhook_events (
        id TEXT PRIMARY KEY,
        received_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS schema_meta (
        k TEXT PRIMARY KEY,
        v TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_jobs_user ON jobs(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_api_keys_user ON api_keys(user_id)",
)


def now() -> int:
    return int(time.time())


class Database:
    """Tiny thread-safe repository. One connection, serialised by a lock (SQLite WAL mode)."""

    def __init__(self, url: str = "sqlite:///:memory:") -> None:
        self.url = url
        self._lock = threading.RLock()
        if url.startswith(("postgres://", "postgresql://")):
            try:
                import psycopg  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - depends on deployment
                raise RuntimeError("PostgreSQL DATABASE_URL needs `pip install psycopg[binary]`") from exc
            self.dialect = "postgres"
            self._conn: Any = psycopg.connect(url, autocommit=True)
        elif url.startswith("sqlite:///"):
            path = url[len("sqlite:///"):]
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.dialect = "sqlite"
            self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
        else:
            raise ValueError("DATABASE_URL must start with sqlite:/// or postgresql://")
        self.migrate()

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(self._sql(sql), tuple(params))
            return int(cur.rowcount or 0)

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            cur = self._conn.execute(self._sql(sql), tuple(params))
            row = cur.fetchone()
            if row is None:
                return None
            if self.dialect == "sqlite":
                return dict(row)
            return dict(zip([c.name for c in cur.description], row))  # pragma: no cover

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(self._sql(sql), tuple(params))
            rows = cur.fetchall()
            if self.dialect == "sqlite":
                return [dict(r) for r in rows]
            names = [c.name for c in cur.description]  # pragma: no cover
            return [dict(zip(names, r)) for r in rows]  # pragma: no cover

    def migrate(self) -> None:
        for stmt in DDL:
            self.execute(stmt)
        self.execute("INSERT INTO schema_meta (k, v) VALUES ('version', ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v",
                     (str(SCHEMA_VERSION),))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- users --------------------------------------------------------------
    def create_user(self, user_id: str, email: str, password_hash: str) -> dict[str, Any]:
        self.execute("INSERT INTO users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                     (user_id, email, password_hash, now()))
        return self.user_by_id(user_id) or {}

    def user_by_email(self, email: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM users WHERE email = ?", (email,))

    def user_by_id(self, user_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM users WHERE id = ?", (user_id,))

    # --- api keys -----------------------------------------------------------
    def add_api_key(self, key_id: str, user_id: str, prefix: str, key_hash: str, label: str) -> None:
        self.execute("INSERT INTO api_keys (id, user_id, prefix, key_hash, label, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (key_id, user_id, prefix, key_hash, label, now()))

    def api_key_by_prefix(self, prefix: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM api_keys WHERE prefix = ? AND revoked_at IS NULL", (prefix,))

    def touch_api_key(self, key_id: str) -> None:
        self.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now(), key_id))

    def list_api_keys(self, user_id: str) -> list[dict[str, Any]]:
        return self.all("SELECT id, prefix, label, created_at, last_used_at, revoked_at FROM api_keys WHERE user_id = ? ORDER BY created_at",
                        (user_id,))

    def revoke_api_key(self, user_id: str, key_id: str) -> bool:
        return self.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                            (now(), key_id, user_id)) > 0

    # --- memberships --------------------------------------------------------
    def membership(self, user_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM memberships WHERE user_id = ?", (user_id,))

    def upsert_membership(self, user_id: str, plan: str, status: str, price_cents: int, currency: str,
                          period_end: int, provider: str, provider_ref: str | None) -> None:
        self.execute(
            """INSERT INTO memberships (user_id, plan, status, price_cents, currency, current_period_end, provider, provider_ref, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (user_id) DO UPDATE SET plan = excluded.plan, status = excluded.status,
                 price_cents = excluded.price_cents, currency = excluded.currency,
                 current_period_end = excluded.current_period_end, provider = excluded.provider,
                 provider_ref = excluded.provider_ref, updated_at = excluded.updated_at""",
            (user_id, plan, status, int(price_cents), currency, int(period_end), provider, provider_ref, now()),
        )

    def claim_webhook_event(self, event_id: str) -> bool:
        """True the first time `event_id` is seen (idempotent webhook processing)."""
        try:
            self.execute("INSERT INTO webhook_events (id, received_at) VALUES (?, ?)", (event_id, now()))
            return True
        except sqlite3.IntegrityError:
            return False
        except Exception as exc:  # pragma: no cover - psycopg UniqueViolation
            if exc.__class__.__name__ == "UniqueViolation":
                return False
            raise

    # --- uploads / jobs -----------------------------------------------------
    def add_upload(self, upload_id: str, user_id: str, filename: str, total_bytes: int) -> None:
        self.execute("INSERT INTO uploads (id, user_id, filename, total_bytes, status, created_at) VALUES (?, ?, ?, ?, 'uploading', ?)",
                     (upload_id, user_id, filename, total_bytes, now()))

    def set_upload_status(self, upload_id: str, status: str) -> None:
        self.execute("UPDATE uploads SET status = ? WHERE id = ?", (status, upload_id))

    def add_job(self, job_id: str, user_id: str, upload_id: str, options: dict[str, Any]) -> None:
        self.execute("INSERT INTO jobs (id, user_id, upload_id, status, stage, options_json, created_at) VALUES (?, ?, ?, 'queued', 'queued', ?, ?)",
                     (job_id, user_id, upload_id, json.dumps(options), now()))

    def update_job(self, job_id: str, status: str, progress: float, stage: str, result: dict[str, Any] | None, error: str | None,
                   finished: bool) -> None:
        self.execute("UPDATE jobs SET status = ?, progress = ?, stage = ?, result_json = ?, error = ?, finished_at = ? WHERE id = ?",
                     (status, float(progress), stage, json.dumps(result) if result is not None else None, error,
                      now() if finished else None, job_id))

    def job(self, job_id: str, user_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id))

    def jobs_for(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self.all("SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, int(limit)))
