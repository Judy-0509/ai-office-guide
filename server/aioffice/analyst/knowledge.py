"""Knowledge service: one set of read functions over `<vault>/analyst.sqlite` (the mirror),
shared by the HTTP API (`aioffice.analyst.api`) and the MCP tools (`aioffice.analyst.mcp`).
Read-only, offline, no LLM generation -- the only optional model call is embedding/rerank,
used solely to RANK `search()` results (see `steps._similarities`).

Every item that comes from a report carries a citation:
`{"report_id", "report_path", "date", "broker", "title", "quote"}`.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import steps, store

MAX_QUOTE_CHARS = 300
TOP_TOPICS_LIMIT = 10

_CLAIM_JOIN = (
    "SELECT c.*, r.path AS report_path, r.title AS report_title FROM claims c "
    "JOIN reports r ON r.id = c.report_id"
)


# --- connection ----------------------------------------------------------------------------


def _connect_ro(vault: Path) -> sqlite3.Connection:
    """Read-only, WAL-safe connection to the mirror DB (never writes, so it's safe to open
    freely even while a learner is mid-rebuild elsewhere). Raises FileNotFoundError only when
    the vault itself doesn't exist -- a vault that exists but hasn't learned anything yet (no
    analyst.sqlite) is a normal empty state, not an error: an in-memory stand-in is returned
    so `_mirror_ready()` correctly reports "no data yet" and every function's own empty-shape
    guard takes it from there."""
    vault = Path(vault)
    if not vault.exists():
        raise FileNotFoundError(f"vault 경로가 없습니다: {vault}")
    path = store.db_path(vault)
    if not path.exists():
        conn = sqlite3.connect(":memory:")
    else:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _mirror_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reports'").fetchone()
    return row is not None


def _topic_names(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM topics")}


def _resolve_topic(conn: sqlite3.Connection, ref: str) -> str | None:
    row = conn.execute("SELECT id FROM topics WHERE id=?", (ref,)).fetchone()
    if row is not None:
        return row["id"]
    row = conn.execute("SELECT id FROM topics WHERE name=?", (ref,)).fetchone()
    return row["id"] if row is not None else None


def _claim_dict(row: sqlite3.Row, topic_names: dict[str, str]) -> dict:
    entities = json.loads(row["entities"]) if row["entities"] else []
    topic_id = row["topic_id"]
    return {
        "id": row["id"], "date": row["report_date"], "broker": row["broker"],
        "text": row["text"], "entities": entities, "metric": row["metric"],
        "value": row["value"], "unit": row["unit"], "period": row["period"],
        "type": row["type"], "direction": row["direction"],
        "valid": bool(row["valid"]), "invalid_at": row["invalid_at"],
        "number_ok": bool(row["number_ok"]),
        "topic": {"id": topic_id, "name": topic_names.get(topic_id) if topic_id else None},
        "citation": {
            "report_id": row["report_id"], "report_path": row["report_path"],
            "date": row["report_date"], "broker": row["broker"], "title": row["report_title"],
            "quote": (row["quote"] or "")[:MAX_QUOTE_CHARS],
        },
    }


def _last_batch(vault: Path) -> dict | None:
    batches_dir = Path(vault) / "batches"
    files = sorted(batches_dir.glob("*.md")) if batches_dir.exists() else []
    if not files:
        return None
    last = files[-1]
    return {"n": int(last.stem), "path": f"batches/{last.name}"}


# --- 1. overview -----------------------------------------------------------------------------


def overview(vault: Path, as_of: str | None = None) -> dict:
    conn = _connect_ro(vault)
    try:
        empty = {"reports": 0, "claims_valid": 0, "claims_invalid": 0, "topics": 0,
                 "relations": 0, "date_range": [None, None], "top_topics": [],
                 "last_batch": _last_batch(vault)}
        if not _mirror_ready(conn):
            return empty

        if as_of:
            reports_n = conn.execute(
                "SELECT COUNT(*) n FROM reports WHERE status='learned' AND date<=?", (as_of,)
            ).fetchone()["n"]
            valid_n = conn.execute(
                "SELECT COUNT(*) n FROM claims WHERE report_date<=? AND (invalid_at IS NULL OR invalid_at>=?)",
                (as_of, as_of),
            ).fetchone()["n"]
            invalid_n = conn.execute(
                "SELECT COUNT(*) n FROM claims WHERE report_date<=? AND invalid_at IS NOT NULL AND invalid_at<?",
                (as_of, as_of),
            ).fetchone()["n"]
            topics_n = conn.execute(
                "SELECT COUNT(DISTINCT topic_id) n FROM claims WHERE topic_id IS NOT NULL AND report_date<=?",
                (as_of,),
            ).fetchone()["n"]
            relations_n = conn.execute(
                "SELECT COUNT(*) n FROM relations WHERE first_seen<=?", (as_of,)
            ).fetchone()["n"]
            top_rows = conn.execute(
                "SELECT topic_id, COUNT(*) n FROM claims WHERE topic_id IS NOT NULL AND report_date<=? "
                "AND (invalid_at IS NULL OR invalid_at>=?) GROUP BY topic_id ORDER BY n DESC LIMIT ?",
                (as_of, as_of, TOP_TOPICS_LIMIT),
            ).fetchall()
        else:
            reports_n = conn.execute("SELECT COUNT(*) n FROM reports WHERE status='learned'").fetchone()["n"]
            valid_n = conn.execute("SELECT COUNT(*) n FROM claims WHERE valid=1").fetchone()["n"]
            invalid_n = conn.execute("SELECT COUNT(*) n FROM claims WHERE valid=0").fetchone()["n"]
            topics_n = conn.execute("SELECT COUNT(*) n FROM topics").fetchone()["n"]
            relations_n = conn.execute("SELECT COUNT(*) n FROM relations").fetchone()["n"]
            top_rows = conn.execute(
                "SELECT topic_id, COUNT(*) n FROM claims WHERE topic_id IS NOT NULL AND valid=1 "
                "GROUP BY topic_id ORDER BY n DESC LIMIT ?",
                (TOP_TOPICS_LIMIT,),
            ).fetchall()

        topic_names = _topic_names(conn)
        top_topics = [{"id": r["topic_id"], "name": topic_names.get(r["topic_id"], r["topic_id"]),
                       "valid_claims": r["n"]} for r in top_rows]

        date_row = conn.execute(
            "SELECT MIN(date) lo, MAX(date) hi FROM reports WHERE status='learned'"
        ).fetchone()

        return {
            "reports": reports_n, "claims_valid": valid_n, "claims_invalid": invalid_n,
            "topics": topics_n, "relations": relations_n,
            "date_range": [date_row["lo"], date_row["hi"]], "top_topics": top_topics,
            "last_batch": _last_batch(vault),
        }
    finally:
        conn.close()


# --- topics list (backs GET /api/v1/topics; not one of the six, but the same read style) ----


def topics_list(vault: Path) -> list[dict]:
    conn = _connect_ro(vault)
    try:
        if not _mirror_ready(conn):
            return []
        rows = conn.execute("SELECT * FROM topics").fetchall()
        out = []
        for r in rows:
            valid_n = conn.execute(
                "SELECT COUNT(*) n FROM claims WHERE topic_id=? AND valid=1", (r["id"],)
            ).fetchone()["n"]
            updated = conn.execute(
                "SELECT MAX(report_date) d FROM claims WHERE topic_id=?", (r["id"],)
            ).fetchone()["d"]
            out.append({"id": r["id"], "name": r["name"], "valid_claims": valid_n,
                        "trend": r["trend"], "updated": updated})
        out.sort(key=lambda t: t["valid_claims"], reverse=True)
        return out
    finally:
        conn.close()


# --- 2. search ---------------------------------------------------------------------------------


def search(vault: Path, query: str, k: int = 10, date_from: str | None = None,
           date_to: str | None = None, llm: Any = None) -> dict:
    conn = _connect_ro(vault)
    try:
        if not _mirror_ready(conn):
            return {"topics": [], "claims": []}

        topic_rows = conn.execute("SELECT id, name, summary FROM topics").fetchall()
        topic_texts = [f"{r['name']} {r['summary'] or ''}" for r in topic_rows]
        topic_scores = steps._similarities(query, topic_texts, llm) if topic_texts else []
        ranked_topics = sorted(zip(topic_rows, topic_scores), key=lambda p: p[1], reverse=True)[:k]
        topics_out = [{"id": r["id"], "name": r["name"], "summary": r["summary"],
                       "score": round(float(s), 4)} for r, s in ranked_topics]

        sql = _CLAIM_JOIN + " WHERE 1=1"
        params: list = []
        if date_from:
            sql += " AND c.report_date >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND c.report_date <= ?"
            params.append(date_to)
        claim_rows = conn.execute(sql, params).fetchall()
        claim_texts = [r["text"] for r in claim_rows]
        claim_scores = steps._similarities(query, claim_texts, llm) if claim_texts else []
        ranked_claims = sorted(zip(claim_rows, claim_scores), key=lambda p: p[1], reverse=True)[:k]

        topic_names = _topic_names(conn)
        claims_out = []
        for r, s in ranked_claims:
            d = _claim_dict(r, topic_names)
            d["score"] = round(float(s), 4)
            claims_out.append(d)

        return {"topics": topics_out, "claims": claims_out}
    finally:
        conn.close()


# --- 3. topic ------------------------------------------------------------------------------


def topic(vault: Path, topic_id_or_name: str, as_of: str | None = None) -> dict | None:
    conn = _connect_ro(vault)
    try:
        if not _mirror_ready(conn):
            return None
        tid = _resolve_topic(conn, topic_id_or_name)
        if tid is None:
            return None
        trow = conn.execute("SELECT * FROM topics WHERE id=?", (tid,)).fetchone()

        if as_of:
            hist_row = conn.execute(
                "SELECT summary, trend FROM topic_summaries WHERE topic_id=? AND date<=? "
                "ORDER BY date DESC LIMIT 1", (tid, as_of),
            ).fetchone()
            summary = hist_row["summary"] if hist_row else ""
            trend = hist_row["trend"] if hist_row else None
        else:
            summary, trend = trow["summary"], trow["trend"]

        topic_names = _topic_names(conn)
        claim_sql = _CLAIM_JOIN + " WHERE c.topic_id=?"
        params: list = [tid]
        if as_of:
            claim_sql += " AND c.report_date<=?"
            params.append(as_of)
        rows = conn.execute(claim_sql + " ORDER BY c.report_date DESC", params).fetchall()

        valid_rows, invalid_rows = [], []
        for r in rows:
            is_valid = (r["invalid_at"] is None or r["invalid_at"] >= as_of) if as_of else bool(r["valid"])
            (valid_rows if is_valid else invalid_rows).append(r)

        valid_claims = [_claim_dict(r, topic_names) for r in valid_rows]
        invalidated_claims = []
        for r in invalid_rows:
            d = _claim_dict(r, topic_names)
            d["invalidated_by"] = r["invalidated_by"]
            invalidated_claims.append(d)

        related_topics = []
        for rel in conn.execute("SELECT * FROM relations WHERE src=? OR dst=?", (tid, tid)):
            other = rel["dst"] if rel["src"] == tid else rel["src"]
            related_topics.append({"id": other, "name": topic_names.get(other, other),
                                    "kind": rel["kind"], "why": rel["why"]})

        return {
            "id": tid, "name": trow["name"], "summary": summary, "trend": trend,
            "valid_claims": valid_claims, "invalidated_claims": invalidated_claims,
            "related_topics": related_topics,
        }
    finally:
        conn.close()


# --- 4. changes ------------------------------------------------------------------------------


def _judgment_changes(conn: sqlite3.Connection, date_from: str, date_to: str,
                       topic_names: dict[str, str]) -> list[dict]:
    topic_ids = [r["topic_id"] for r in conn.execute(
        "SELECT DISTINCT topic_id FROM topic_summaries WHERE date BETWEEN ? AND ?",
        (date_from, date_to),
    )]
    out = []
    for tid in topic_ids:
        history = conn.execute(
            "SELECT date, summary, trend FROM topic_summaries WHERE topic_id=? ORDER BY date",
            (tid,),
        ).fetchall()
        in_window = [h for h in history if date_from <= h["date"] <= date_to]
        if not in_window:
            continue
        before_candidates = [h for h in history if h["date"] < in_window[0]["date"]]
        out.append({
            "topic_id": tid, "name": topic_names.get(tid, tid),
            "before": before_candidates[-1]["summary"] if before_candidates else None,
            "after": in_window[-1]["summary"], "trend": in_window[-1]["trend"],
        })
    return out


def _invalidations(conn: sqlite3.Connection, date_from: str, date_to: str,
                    topic_names: dict[str, str]) -> list[dict]:
    rows = conn.execute(
        _CLAIM_JOIN + " WHERE c.invalid_at BETWEEN ? AND ?", (date_from, date_to)
    ).fetchall()
    out = []
    for r in rows:
        old = _claim_dict(r, topic_names)
        new_row = conn.execute(_CLAIM_JOIN + " WHERE c.id=?", (r["invalidated_by"],)).fetchone()
        out.append({"old": old, "new": _claim_dict(new_row, topic_names) if new_row else None})
    return out


def changes(vault: Path, date_from: str, date_to: str) -> dict:
    conn = _connect_ro(vault)
    try:
        if not _mirror_ready(conn):
            return {"reports": [], "new_topics": [], "judgment_changes": [], "invalidations": [],
                    "relations": []}

        topic_names = _topic_names(conn)

        report_rows = conn.execute(
            "SELECT * FROM reports WHERE status='learned' AND date BETWEEN ? AND ? ORDER BY date",
            (date_from, date_to),
        ).fetchall()
        reports_out = [{"id": r["id"], "path": r["path"], "date": r["date"], "broker": r["broker"],
                        "title": r["title"]} for r in report_rows]

        new_topic_rows = conn.execute(
            "SELECT * FROM topics WHERE created_date BETWEEN ? AND ?", (date_from, date_to)
        ).fetchall()
        new_topics_out = [{"id": r["id"], "name": r["name"]} for r in new_topic_rows]

        rel_rows = conn.execute(
            "SELECT * FROM relations WHERE last_seen BETWEEN ? AND ?", (date_from, date_to)
        ).fetchall()
        relations_out = [{
            "from": {"id": r["src"], "name": topic_names.get(r["src"], r["src"])},
            "to": {"id": r["dst"], "name": topic_names.get(r["dst"], r["dst"])},
            "kind": r["kind"], "why": r["why"], "is_new": r["first_seen"] >= date_from,
        } for r in rel_rows]

        return {
            "reports": reports_out, "new_topics": new_topics_out,
            "judgment_changes": _judgment_changes(conn, date_from, date_to, topic_names),
            "invalidations": _invalidations(conn, date_from, date_to, topic_names),
            "relations": relations_out,
        }
    finally:
        conn.close()


# --- 5. claims (the dashboard door) ------------------------------------------------------------


def claims(vault: Path, entity: str | None = None, metric: str | None = None,
           broker: str | None = None, type: str | None = None, topic: str | None = None,
           date_from: str | None = None, date_to: str | None = None, valid: str = "valid",
           numeric_only: bool = False, limit: int = 200, offset: int = 0) -> dict:
    conn = _connect_ro(vault)
    try:
        if not _mirror_ready(conn):
            return {"rows": [], "total": 0}

        topic_id = None
        if topic:
            topic_id = _resolve_topic(conn, topic)
            if topic_id is None:
                return {"rows": [], "total": 0}

        clauses = ["1=1"]
        params: list = []
        if entity:
            clauses.append("c.entities LIKE ?")
            params.append(f"%{entity}%")
        if metric:
            clauses.append("c.metric LIKE ?")
            params.append(f"%{metric}%")
        if broker:
            clauses.append("c.broker = ?")
            params.append(broker)
        if type:
            clauses.append("c.type = ?")
            params.append(type)
        if topic_id:
            clauses.append("c.topic_id = ?")
            params.append(topic_id)
        if date_from:
            clauses.append("c.report_date >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("c.report_date <= ?")
            params.append(date_to)
        if valid == "valid":
            clauses.append("c.valid = 1")
        elif valid == "invalid":
            clauses.append("c.valid = 0")
        if numeric_only:
            clauses.append("c.value IS NOT NULL AND c.value != ''")

        where = " AND ".join(clauses)
        total = conn.execute(f"SELECT COUNT(*) n FROM claims c WHERE {where}", params).fetchone()["n"]
        rows = conn.execute(
            f"{_CLAIM_JOIN} WHERE {where} ORDER BY c.report_date DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()

        topic_names = _topic_names(conn)
        return {"rows": [_claim_dict(r, topic_names) for r in rows], "total": total}
    finally:
        conn.close()


# --- 6. metric_history --------------------------------------------------------------------------


def metric_history(vault: Path, entity: str, metric: str, period: str | None = None) -> dict:
    conn = _connect_ro(vault)
    try:
        if not _mirror_ready(conn):
            return {"groups": []}

        sql = _CLAIM_JOIN + " WHERE c.entities LIKE ? AND c.metric LIKE ?"
        params: list = [f"%{entity}%", f"%{metric}%"]
        if period:
            sql += " AND c.period = ?"
            params.append(period)
        sql += " ORDER BY c.broker, c.period, c.unit, c.report_date"
        rows = conn.execute(sql, params).fetchall()

        groups: dict[tuple, list] = {}
        for r in rows:
            groups.setdefault((r["broker"], r["period"], r["unit"]), []).append(r)

        topic_names = _topic_names(conn)
        out = []
        for (broker, period_val, unit), group_rows in groups.items():
            chain = [{"value": r["value"], "date": r["report_date"], "valid": bool(r["valid"]),
                      "citation": _claim_dict(r, topic_names)["citation"]} for r in group_rows]
            latest_valid = next((c for c in reversed(chain) if c["valid"]), None)
            out.append({
                "broker": broker, "period": period_val, "unit": unit,
                "latest_value": latest_valid["value"] if latest_valid else None,
                "chain": chain,
            })
        return {"groups": out}
    finally:
        conn.close()
