"""Vault text state: `.analyst/*.jsonl`/`.json` I/O, applying integrate/rollback, queries used
by the viewer API (`report_changes`, `graph`). Every claim/topic/relation carries its
originating `report_id` so provenance stays exact (Graphiti/cognee-style: nothing is deleted,
invalidated rows are kept and flagged).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import db

STATE_DIR_NAME = ".analyst"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_UNSAFE_NAME_RE = re.compile(r"[^\w.\-가-힣]+")


# --- vault paths ---------------------------------------------------------------------------


def state_dir(vault: Path) -> Path:
    return Path(vault) / STATE_DIR_NAME


def reports_path(vault: Path) -> Path:
    return state_dir(vault) / "reports.jsonl"


def claims_path(vault: Path) -> Path:
    return state_dir(vault) / "claims.jsonl"


def topics_path(vault: Path) -> Path:
    return state_dir(vault) / "topics.json"


def relations_path(vault: Path) -> Path:
    return state_dir(vault) / "relations.jsonl"


def feedback_path(vault: Path) -> Path:
    return state_dir(vault) / "feedback.jsonl"


def events_path(vault: Path) -> Path:
    return state_dir(vault) / "events.jsonl"


def db_path(vault: Path) -> Path:
    """The queryable SQLite mirror + LLM call log -- one DB file holds everything (see
    rebuild_mirror_db); it lives at the vault root (not `.analyst/`) so a vault `.gitignore`
    of `analyst.sqlite*` doesn't have to reach into the git-diffable state directory."""
    return Path(vault) / "analyst.sqlite"


def broker_slug(name: str) -> str:
    slug = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    if slug:
        return slug
    return hashlib.sha1((name or "").encode("utf-8")).hexdigest()[:8]  # e.g. Korean-only names


def report_filename(report_date: str, broker: str, report_id: str) -> str:
    return f"{report_date}_{broker_slug(broker)}_{report_id[:8]}.md"


def safe_entity_name(name: str) -> str:
    cleaned = _UNSAFE_NAME_RE.sub("_", (name or "").strip())
    return cleaned or "unknown"


# --- generic jsonl helpers -------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in records)
    path.write_text(text + ("\n" if records else ""), encoding="utf-8")


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


# --- feedback / events (true append-only logs) --------------------------------------------


def read_feedback(vault: Path) -> list[dict]:
    return _read_jsonl(feedback_path(vault))


def append_feedback(vault: Path, record: dict) -> None:
    _append_jsonl(feedback_path(vault), record)


def append_event(vault: Path, event_type: str, data: dict, *, ts: str) -> dict:
    record = {"event": event_type, "data": data, "ts": ts}
    _append_jsonl(events_path(vault), record)
    return record


def read_events(vault: Path, limit: int | None = None) -> list[dict]:
    records = _read_jsonl(events_path(vault))
    return records[-limit:] if limit else records


_ITEM_DEFAULTS = {
    "extract_seconds": None, "integrate_seconds": None, "claims": None, "number_fail": None,
    "new": None, "supports": None, "updates": None, "contradicts": None, "error": None,
}


def batch_progress(vault: Path, *, running: bool = True) -> dict | None:
    """Reconstructs per-report progress for the current (or most recently finished) batch by
    replaying events.jsonl from its last `batch_start` onward. Pure function of the event log
    (no in-memory state), so a CLI-started batch shows in the viewer exactly like one started
    through /api/learn. Returns None if no batch has ever started.

    `running`: whether a learner is currently attending to this vault IN THIS PROCESS (the
    caller passes its own in-memory flag). When False, any item still stuck in
    "extracting"/"integrating" (the process that was working it got killed) is reported as
    "error"/"중단됨" instead of spinning forever in a status UI.

    ponytail: this can't see a *different* process's `learn` CLI still working the same
    vault -- "running" is tied to whichever process answers /api/status. Fine for the common
    `analyst.run` case (viewer + learner share one process); a standalone `viewer.py` next to
    a separate `learn.py` CLI would misreport an active external run as stuck.
    """
    events = read_events(vault)
    start_idx = None
    for idx, e in enumerate(events):
        if e["event"] == "batch_start":
            start_idx = idx
    if start_idx is None:
        return None

    batch_events = events[start_idx:]
    batch_num = batch_events[0]["data"]["batch"]
    total = batch_events[0]["data"]["n"]

    items: list[dict] = []
    by_id: dict[str, dict] = {}
    current_id: str | None = None
    last_i = 0
    current_label: str | None = None

    for e in batch_events[1:]:
        etype, data = e["event"], e["data"]
        if etype == "report_start":
            item = {"report_id": data["report_id"], "date": data["date"], "broker": data["broker"],
                     "title": data["title"], "state": "waiting", **_ITEM_DEFAULTS}
            by_id[data["report_id"]] = item
            items.append(item)
            current_id = data["report_id"]
            last_i = data["i"]
            current_label = f"{data['broker']} · {data['title']}"
        elif etype == "extract_start":
            item = by_id.get(data.get("report_id"))
            if item is not None:
                item["state"] = "extracting"
        elif etype == "extracted":
            item = by_id.get(data.get("report_id"))
            if item is not None:
                item["claims"] = data["claims"]
                item["number_fail"] = data["number_fail"]
                item["extract_seconds"] = data["seconds"]
        elif etype == "integrate_start":
            item = by_id.get(data.get("report_id"))
            if item is not None:
                item["state"] = "integrating"
        elif etype == "integrated":
            item = by_id.get(data.get("report_id"))
            if item is not None:
                item["integrate_seconds"] = data["seconds"]
                item["new"] = data["new"]
                item["supports"] = data["supports"]
                item["updates"] = data["updates"]
                item["contradicts"] = data["contradicts"]
        elif etype == "report_done":
            item = by_id.get(data.get("report_id"))
            if item is not None and item["state"] != "error":  # a prior error event wins
                item["state"] = "done"
        elif etype == "error":
            # A report-scoped failure (e.g. a hard extract error) carries its own report_id.
            # Batch-scoped errors (an inbox skip warning, a consolidate failure) don't --
            # attribute those to whichever report is currently in flight, unless it's done.
            target_id = data.get("report_id") or current_id
            item = by_id.get(target_id) if target_id else None
            if item is not None and item["state"] != "done":
                item["state"] = "error"
                item["error"] = data["text"]

    if not running:
        for item in items:
            if item["state"] in ("extracting", "integrating"):
                item["state"] = "error"
                item["error"] = "중단됨"

    return {"batch": batch_num, "n": total, "i": last_i, "current": current_label, "items": items}


# --- state ---------------------------------------------------------------------------------


@dataclass
class AnalystState:
    vault: Path
    reports: list[dict] = field(default_factory=list)
    claims: list[dict] = field(default_factory=list)
    topics: dict[str, dict] = field(default_factory=dict)
    relations: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, vault: Path) -> "AnalystState":
        vault = Path(vault)
        tpath = topics_path(vault)
        topics = json.loads(tpath.read_text(encoding="utf-8")) if tpath.exists() else {}
        return cls(
            vault=vault, reports=_read_jsonl(reports_path(vault)),
            claims=_read_jsonl(claims_path(vault)), topics=topics,
            relations=_read_jsonl(relations_path(vault)),
        )

    def save_reports(self) -> None:
        _write_jsonl(reports_path(self.vault), self.reports)

    def save_claims(self) -> None:
        _write_jsonl(claims_path(self.vault), self.claims)

    def save_topics(self) -> None:
        path = topics_path(self.vault)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.topics, ensure_ascii=False, indent=2, sort_keys=True),
                         encoding="utf-8")

    def save_relations(self) -> None:
        _write_jsonl(relations_path(self.vault), self.relations)

    def save_all(self) -> None:
        self.save_reports()
        self.save_claims()
        self.save_topics()
        self.save_relations()

    def learned_ids(self) -> set[str]:
        """Report ids scan_inbox should skip -- anything already handled (successfully or via
        a deliberate rollback). A report marked "error" (a hard extract failure) is
        deliberately left out, so it comes back as pending and is retried in a later batch."""
        return {r["id"] for r in self.reports if r["status"] != "error"}

    def report_by_id(self, report_id: str | None) -> dict | None:
        if not report_id:
            return None
        return next((r for r in self.reports if r["id"] == report_id), None)

    def claims_for_report(self, report_id: str) -> list[dict]:
        return [c for c in self.claims if c["report_id"] == report_id]

    def claim_by_id(self, claim_id: str | None) -> dict | None:
        if not claim_id:
            return None
        return next((c for c in self.claims if c["id"] == claim_id), None)

    def claims_for_topic(self, topic_id: str, *, valid_only: bool = True) -> list[dict]:
        claims = [c for c in self.claims if c.get("topic_id") == topic_id
                  and (not valid_only or c.get("valid", True))]
        return sorted(claims, key=lambda c: c["report_date"], reverse=True)

    def next_topic_id(self) -> str:
        existing = [int(t[1:]) for t in self.topics if t.startswith("t") and t[1:].isdigit()]
        return f"t{(max(existing) + 1) if existing else 1:03d}"


# --- applying an integrate() result ---------------------------------------------------------


def apply_integration(state: AnalystState, report: dict, processed_claims: list[dict],
                       integrate_result: dict) -> dict:
    """Applies one report's integrate() output in place: creates/reuses topics, attaches
    claims, invalidates updated/contradicted targets, upserts topic-topic relations.

    Returns a summary used for events + rendering: relation-kind counts, topics_created,
    topics_updated, invalidated claims, and relations newly created this call.
    """
    report_id, report_date, broker = report["id"], report["date"], report["broker"]
    decisions = {d.get("cid"): d for d in integrate_result.get("claims", [])}
    name_to_new_topic: dict[str, str] = {}
    global_id: dict[str, str] = {}

    counts = {"new": 0, "supports": 0, "updates": 0, "contradicts": 0}
    topics_created: list[dict] = []
    topics_updated_ids: set[str] = set()
    invalidated: list[dict] = []
    relations_created: list[dict] = []

    for claim in processed_claims:
        local_id = claim["local_id"]
        decision = decisions.get(local_id) or {}
        global_id[local_id] = f"{report_id[:8]}-{local_id}"
        topic_field = decision.get("topic")
        relation = decision.get("relation") or "new"
        target = decision.get("target")

        topic_id: str | None = None
        if isinstance(topic_field, dict) and "new" in topic_field:
            name = str(topic_field["new"]).strip() or "미분류"
            existing = next((tid for tid, t in state.topics.items() if t["name"] == name), None)
            if existing:
                topic_id = existing
            elif name in name_to_new_topic:
                topic_id = name_to_new_topic[name]
            else:
                topic_id = state.next_topic_id()
                state.topics[topic_id] = {
                    "id": topic_id, "name": name, "summary": "", "trend": "flat",
                    "summary_history": [], "created_report_id": report_id,
                    "created_date": report_date, "needs_summary": False,
                }
                name_to_new_topic[name] = topic_id
                topics_created.append({"id": topic_id, "name": name})
        elif isinstance(topic_field, str) and topic_field in state.topics:
            topic_id = topic_field

        if topic_id is None:
            continue  # an unresolvable decision -- learn.py's fallback path replaces the whole result

        if relation not in counts:
            relation = "new"
        counts[relation] += 1
        if topic_id not in {t["id"] for t in topics_created}:
            topics_updated_ids.add(topic_id)

        record = {
            "id": global_id[local_id], "report_id": report_id, "report_date": report_date,
            "broker": broker, "sids": claim["sids"], "text": claim["text"],
            "type": claim.get("type"), "direction": claim.get("direction"),
            "entities": claim.get("entities") or [], "metric": claim.get("metric"),
            "value": claim.get("value"), "unit": claim.get("unit"), "period": claim.get("period"),
            "quote": claim.get("quote", ""), "number_ok": claim.get("number_ok", True),
            "topic_id": topic_id, "relation": relation, "target_claim_id": None,
            "valid": True, "invalid_at": None, "invalidated_by": None,
        }

        if relation in ("updates", "contradicts") and target:
            target_claim = state.claim_by_id(target)
            if target_claim is not None and target_claim.get("valid", True):
                target_claim["valid"] = False
                target_claim["invalid_at"] = report_date
                target_claim["invalidated_by"] = record["id"]
                record["target_claim_id"] = target_claim["id"]
                invalidated.append({
                    "id": target_claim["id"], "text": target_claim["text"],
                    "topic_id": target_claim.get("topic_id"),
                    "topic_name": state.topics.get(target_claim.get("topic_id"), {}).get("name"),
                    "by_claim_id": record["id"],
                })

        state.claims.append(record)

    def _resolve_topic_ref(ref: Any) -> str | None:
        if ref in state.topics:
            return ref
        return name_to_new_topic.get(ref)

    for link in integrate_result.get("links", []):
        src, dst = _resolve_topic_ref(link.get("from")), _resolve_topic_ref(link.get("to"))
        kind = link.get("kind")
        evidence_id = global_id.get(link.get("cid"))
        if not src or not dst or not kind:
            continue
        rel = next((r for r in state.relations
                    if r["from"] == src and r["to"] == dst and r["kind"] == kind), None)
        if rel is None:
            rel = {"from": src, "to": dst, "kind": kind, "why": "",
                   "first_seen": report_date, "last_seen": report_date, "evidence": []}
            state.relations.append(rel)
            relations_created.append(rel)
        rel["last_seen"] = report_date
        if evidence_id and evidence_id not in rel["evidence"]:
            rel["evidence"].append(evidence_id)

    return {
        **counts, "topics_created": topics_created,
        "topics_updated": [{"id": tid, "name": state.topics[tid]["name"]}
                            for tid in sorted(topics_updated_ids)],
        "invalidated": invalidated, "relations_created": relations_created,
    }


def fallback_integrate_result(processed_claims: list[dict]) -> dict:
    """Used when integrate()'s output is unusable after a retry: every claim goes to (or
    reuses) a topic named 미분류, contract-mandated fallback."""
    return {
        "claims": [{"cid": c["local_id"], "topic": {"new": "미분류"}, "relation": "new", "target": None}
                   for c in processed_claims],
        "links": [],
    }


# --- rollback --------------------------------------------------------------------------------


def rollback(state: AnalystState, report_id: str) -> dict:
    """Undoes one report's learning (review verdict `wrong`). Restores claims it invalidated,
    drops its relation evidence (deleting relations left with none), deletes topics it created
    that end up with no claims, and marks other touched topics `needs_summary` for the next
    consolidate. Returns the touched topic ids so a caller can consolidate them immediately.
    """
    report = state.report_by_id(report_id)
    if report is None:
        raise KeyError(report_id)

    this_report_claims = [c for c in state.claims if c["report_id"] == report_id]
    this_ids = {c["id"] for c in this_report_claims}
    touched_topics: set[str] = {c["topic_id"] for c in this_report_claims if c.get("topic_id")}

    for c in state.claims:
        if c.get("invalidated_by") in this_ids:
            c["valid"] = True
            c["invalid_at"] = None
            c["invalidated_by"] = None
            if c.get("topic_id"):
                touched_topics.add(c["topic_id"])

    state.claims = [c for c in state.claims if c["report_id"] != report_id]

    kept_relations = []
    for rel in state.relations:
        rel["evidence"] = [e for e in rel["evidence"] if e not in this_ids]
        if rel["evidence"]:
            kept_relations.append(rel)
    state.relations = kept_relations

    remaining_topic_ids = {c["topic_id"] for c in state.claims if c.get("topic_id")}
    removed_topics = [
        tid for tid, t in list(state.topics.items())
        if t.get("created_report_id") == report_id and tid not in remaining_topic_ids
    ]
    for tid in removed_topics:
        del state.topics[tid]
    touched_topics -= set(removed_topics)

    for tid in touched_topics:
        if tid in state.topics:
            state.topics[tid]["needs_summary"] = True

    report["status"] = "rolled_back"
    report["review"] = "wrong"

    return {"touched_topics": sorted(touched_topics), "removed_topics": removed_topics}


# --- viewer API queries -----------------------------------------------------------------------


def report_changes(state: AnalystState, report_id: str) -> dict:
    claims = state.claims_for_report(report_id)
    created_topic_ids = {tid for tid, t in state.topics.items() if t.get("created_report_id") == report_id}
    updated_topic_ids: set[str] = set()

    out_claims = []
    for c in claims:
        topic = state.topics.get(c.get("topic_id")) or {}
        target = None
        target_claim = state.claim_by_id(c.get("target_claim_id"))
        if target_claim is not None:
            target = {"id": target_claim["id"], "text": target_claim["text"],
                       "report_date": target_claim["report_date"]}
        out_claims.append({
            "id": c["id"], "text": c["text"], "type": c.get("type"), "direction": c.get("direction"),
            "topic": {"id": c.get("topic_id"), "name": topic.get("name")},
            "relation": c.get("relation"), "target": target,
            "number_ok": c.get("number_ok", True),
        })
        if c.get("topic_id") and c["topic_id"] not in created_topic_ids:
            updated_topic_ids.add(c["topic_id"])

    this_ids = {c["id"] for c in claims}
    invalidated = []
    for c in state.claims:
        if c.get("invalidated_by") in this_ids:
            topic = state.topics.get(c.get("topic_id")) or {}
            invalidated.append({"id": c["id"], "text": c["text"], "topic_name": topic.get("name"),
                                 "by_claim_id": c["invalidated_by"]})

    return {
        "claims": out_claims, "invalidated": invalidated,
        "topics_created": [{"id": tid, "name": state.topics[tid]["name"]}
                            for tid in sorted(created_topic_ids) if tid in state.topics],
        "topics_updated": [{"id": tid, "name": state.topics[tid]["name"]}
                            for tid in sorted(updated_topic_ids) if tid in state.topics],
    }


def graph(state: AnalystState, as_of: str | None = None) -> dict:
    def keep(d: str | None) -> bool:
        return d is None or as_of is None or d <= as_of

    nodes: list[dict] = []
    edges: list[dict] = []
    dates: list[str] = []

    entity_counts: dict[str, dict] = {}
    for c in state.claims:
        if not keep(c["report_date"]):
            continue
        for name in c.get("entities") or []:
            info = entity_counts.setdefault(name, {"count": 0, "first_seen": c["report_date"]})
            info["count"] += 1
            info["first_seen"] = min(info["first_seen"], c["report_date"])

    for r in state.reports:
        if not keep(r["date"]):
            continue
        report_node = f"reports/{Path(r['path']).stem}"
        nodes.append({"id": report_node, "type": "report", "label": r["title"], "date": r["date"],
                       "size": 1, "status": "rolled_back" if r["status"] == "rolled_back" else "active"})
        dates.append(r["date"])
        report_topics = {c["topic_id"] for c in state.claims
                          if c["report_id"] == r["id"] and c.get("topic_id") and keep(c["report_date"])}
        for tid in report_topics:
            edges.append({"source": report_node, "target": f"topics/{tid}", "kind": "about", "date": r["date"]})

    for tid, t in state.topics.items():
        any_claims = [c for c in state.claims if c.get("topic_id") == tid and keep(c["report_date"])]
        if not any_claims:
            continue
        valid_claims = [c for c in any_claims if c.get("valid", True)]
        first_seen = min(c["report_date"] for c in any_claims)
        status = "active" if valid_claims else "invalid"
        nodes.append({"id": f"topics/{tid}", "type": "topic", "label": t["name"], "date": first_seen,
                       "size": len(valid_claims), "status": status})
        dates.append(first_seen)
        entity_names = {name for c in any_claims for name in (c.get("entities") or [])}
        for name in entity_names:
            if entity_counts.get(name, {}).get("count", 0) < 2:
                continue
            edges.append({"source": f"topics/{tid}", "target": f"entities/{safe_entity_name(name)}",
                           "kind": "mentions", "date": first_seen})

    for name, info in entity_counts.items():
        if info["count"] < 2:
            continue
        nodes.append({"id": f"entities/{safe_entity_name(name)}", "type": "entity", "label": name,
                       "date": info["first_seen"], "size": info["count"], "status": "active"})
        dates.append(info["first_seen"])

    for rel in state.relations:
        if not keep(rel["last_seen"]):
            continue
        edges.append({"source": f"topics/{rel['from']}", "target": f"topics/{rel['to']}",
                       "kind": rel["kind"], "date": rel["last_seen"]})

    dates.sort()
    return {"nodes": nodes, "edges": edges, "dates": [dates[0], dates[-1]] if dates else []}


# --- SQLite mirror ("DB화") -------------------------------------------------------------------
#
# A queryable mirror of the JSON/JSONL state, rebuilt wholesale (drop + recreate, one
# transaction) after every report/batch/review/claim edit. The JSONL files stay the source of
# truth (git-diffable); this is a read-optimized copy for SQL access, sharing the same sqlite
# file as LLMClient's llm_calls/embeddings tables ("one DB holds everything").

_MIRROR_TABLES = ("reports", "claims", "topics", "topic_summaries", "relations", "feedback")

_MIRROR_DDL = """
CREATE TABLE reports (
    id TEXT PRIMARY KEY, path TEXT, broker TEXT, title TEXT, date TEXT, date_source TEXT,
    status TEXT, review TEXT, fictional INTEGER
);
CREATE TABLE claims (
    id TEXT PRIMARY KEY, report_id TEXT, report_date TEXT, broker TEXT, text TEXT,
    type TEXT, direction TEXT, metric TEXT, value TEXT, unit TEXT, period TEXT,
    quote TEXT, number_ok INTEGER, topic_id TEXT, relation TEXT, target_claim_id TEXT,
    valid INTEGER, invalid_at TEXT, invalidated_by TEXT
);
CREATE TABLE topics (
    id TEXT PRIMARY KEY, name TEXT, summary TEXT, trend TEXT,
    created_report_id TEXT, created_date TEXT
);
CREATE TABLE topic_summaries (
    topic_id TEXT, date TEXT, summary TEXT, trend TEXT
);
CREATE TABLE relations (
    src TEXT, dst TEXT, kind TEXT, why TEXT, first_seen TEXT, last_seen TEXT, evidence TEXT
);
CREATE TABLE feedback (
    report_id TEXT, claim_id TEXT, text TEXT, corrected_text TEXT, corrected_topic_id TEXT
);
"""


def rebuild_mirror_db(state: AnalystState, conn: sqlite3.Connection) -> None:
    """Drops and recreates the six mirror tables and repopulates them from `state` (plus
    feedback.jsonl), all in one transaction. Never touches llm_calls/embeddings (owned by
    aioffice.db) living in the same file -- held under db.WRITE_LOCK since LLMClient may log
    a call on this same connection from another thread."""
    with db.WRITE_LOCK:
        _rebuild_mirror_db_locked(state, conn)


def _rebuild_mirror_db_locked(state: AnalystState, conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table in _MIRROR_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript(_MIRROR_DDL)

        for r in state.reports:
            conn.execute(
                "INSERT INTO reports VALUES (?,?,?,?,?,?,?,?,?)",
                (r["id"], r["path"], r["broker"], r["title"], r["date"], r["date_source"],
                 r["status"], r.get("review"), int(bool(r.get("fictional")))),
            )
        for c in state.claims:
            conn.execute(
                "INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (c["id"], c["report_id"], c["report_date"], c["broker"], c["text"],
                 c.get("type"), c.get("direction"), c.get("metric"), c.get("value"),
                 c.get("unit"), c.get("period"), c.get("quote"),
                 int(bool(c.get("number_ok", True))), c.get("topic_id"), c.get("relation"),
                 c.get("target_claim_id"), int(bool(c.get("valid", True))),
                 c.get("invalid_at"), c.get("invalidated_by")),
            )
        for tid, t in state.topics.items():
            conn.execute(
                "INSERT INTO topics VALUES (?,?,?,?,?,?)",
                (tid, t["name"], t.get("summary"), t.get("trend"),
                 t.get("created_report_id"), t.get("created_date")),
            )
            for h in t.get("summary_history") or []:
                conn.execute("INSERT INTO topic_summaries VALUES (?,?,?,?)",
                              (tid, h.get("date"), h.get("summary"), h.get("trend")))
        for rel in state.relations:
            conn.execute(
                "INSERT INTO relations VALUES (?,?,?,?,?,?,?)",
                (rel["from"], rel["to"], rel["kind"], rel.get("why", ""), rel["first_seen"],
                 rel["last_seen"], json.dumps(rel.get("evidence", []), ensure_ascii=False)),
            )
        for f in read_feedback(state.vault):
            conn.execute(
                "INSERT INTO feedback VALUES (?,?,?,?,?)",
                (f.get("report_id"), f.get("claim_id"), f.get("text"),
                 f.get("corrected_text"), f.get("corrected_topic_id")),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
