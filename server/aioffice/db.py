"""SQLite helpers.

Any write from a worker thread must hold WRITE_LOCK through its commit, or use a
per-thread connection.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# `insert` executes without committing, so it leaves a transaction open on the connection.
# Transaction boundaries are per-connection, not per-statement: one thread's commit would
# otherwise commit another thread's half-written work. RLock so a writer may nest helpers.
WRITE_LOCK = threading.RLock()


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: callers may log an llm_calls row from a worker thread.
    # Writers serialize on WRITE_LOCK, see the module docstring.
    conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def insert(conn: sqlite3.Connection, table: str, **values: Any) -> int:
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    return int(cur.lastrowid)


def fetchone(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, tuple(params)).fetchone()


def fetchall(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, tuple(params)).fetchall())
