from __future__ import annotations

from pathlib import Path

from aioffice import db

EXPECTED_TABLES = {"llm_calls", "embeddings"}


def test_init_schema_creates_every_table(tmp_path: Path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    names = {r["name"] for r in db.fetchall(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= names


def test_init_schema_is_idempotent(tmp_path: Path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    db.init_schema(conn)  # must not raise


def test_insert_returns_rowid_and_fetchone_reads_back(tmp_path: Path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    call_id = db.insert(conn, "llm_calls", step="card", backend="direct", model="qwen", status="ok")
    conn.commit()
    row = db.fetchone(conn, "SELECT step, backend, status FROM llm_calls WHERE id=?", (call_id,))
    assert row["step"] == "card" and row["backend"] == "direct" and row["status"] == "ok"


def test_embeddings_roundtrip(tmp_path: Path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    vector = (1.0).hex().encode()  # any bytes blob stands in for a packed float32 vector
    db.insert(conn, "embeddings", text_hash="h1", model="m", dim=2, vector=vector)
    conn.commit()
    row = db.fetchone(conn, "SELECT dim, vector FROM embeddings WHERE text_hash=? AND model=?",
                      ("h1", "m"))
    assert row["dim"] == 2 and row["vector"] == vector
