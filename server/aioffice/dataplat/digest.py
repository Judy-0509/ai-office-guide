"""`python -m aioffice.dataplat.digest --db <path> --source <source.yaml> [--env .env]
[--dataset <name>]`

Generates, for each dataset with a load newer than its last digest, a short LLM-written summary:
(a) "이번 버전 주요 변화 요약" from `version_diff`'s top changes (versioned datasets) or the
load's own diff report (unversioned datasets), and (b) a one-line note for each of the top 5
changed entities. Every number is checked against the code-computed table first (the same guard
`dataplat.chat` uses) -- a sentence that fails is replaced by a template one, never left out.

Meant to run right after a snapshot (`schedule.py`'s generated wrapper does this), where waiting
on the LLM is harmless -- unlike the interactive chatbot, nothing is blocking on it. One
dataset's failure is logged and skipped; it never stops the others or fails the calling process
(so it never blocks a snapshot that otherwise succeeded).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..config import Settings
from . import chat, store
from .source import load_source

DIGEST_TIMEOUT_SEC = 600.0  # batch context -- waiting on the shared LLM endpoint is harmless here
TOP_ENTITY_NOTES = 5


def _version_summary(conn, llm, dataset: str, to_load_id: int) -> dict | None:
    try:
        diff = store.version_diff(conn, dataset, group_by=["entity"], top=TOP_ENTITY_NOTES)
    except ValueError:
        return None  # no previous version to compare against yet
    table = {"columns": ["entity", "old", "new", "diff", "pct"],
             "rows": [[r["entity"], r["old"], r["new"], r["diff"], r["pct"]] for r in diff["rows"]]}
    template_fn = lambda: chat._template_diff(  # noqa: SLF001 -- same-package reuse, not public API
        table["rows"], ["entity"], diff["from_label"], diff["to_label"], "")
    sentence, _warnings = chat.explain(llm, table, template_fn, timeout=DIGEST_TIMEOUT_SEC)
    return {"kind": "version_summary", "title": "이번 버전 주요 변화 요약", "text": sentence,
            "table": table, "diff_rows": diff["rows"]}


def _load_summary(conn, llm, load: dict) -> dict | None:
    diff = (load.get("report") or {}).get("diff")
    if not diff or not diff.get("top_changes"):
        return None
    rows = diff["top_changes"][:TOP_ENTITY_NOTES]
    table = {"columns": ["entity", "old", "new"], "rows": [[r.get("entity", ""), r["old"], r["new"]] for r in rows]}

    def template_fn() -> str:
        top = table["rows"][0]
        return f"가장 크게 바뀐 항목은 {top[0]}({chat._num_text(top[1])} → {chat._num_text(top[2])})입니다."

    sentence, _warnings = chat.explain(llm, table, template_fn, timeout=DIGEST_TIMEOUT_SEC)
    diff_rows = [{"entity": r.get("entity", ""), "old": r["old"], "new": r["new"],
                  "diff": r["new"] - r["old"], "pct": None} for r in rows]
    return {"kind": "load_summary", "title": "이번 적재 주요 변화 요약", "text": sentence,
            "table": table, "diff_rows": diff_rows}


def _entity_notes(llm, diff_rows: list[dict], unit: str) -> list[dict]:
    notes = []
    for r in diff_rows[:TOP_ENTITY_NOTES]:
        entity = r.get("entity", "")
        row = [entity, r.get("old"), r.get("new"), r.get("diff"), r.get("pct")]
        table = {"columns": ["entity", "old", "new", "diff", "pct"], "rows": [row]}
        template_fn = lambda row=row: chat._template_diff(  # noqa: SLF001
            [row], ["entity"], None, None, unit)
        sentence, _warnings = chat.explain(llm, table, template_fn, timeout=DIGEST_TIMEOUT_SEC)
        notes.append({"kind": "entity_note", "title": entity, "text": sentence, "table": table})
    return notes


def run_digest_for_dataset(conn, llm, dataset: str) -> list[dict]:
    """Generates + stores this dataset's digest for its current latest load, if it doesn't
    already have one (idempotent per load_id) and there's anything to summarize. Returns the
    inserted rows (possibly empty)."""
    load_id = store.latest_ok_load_id(conn, dataset)
    if load_id is None or store.has_digest_for_load(conn, dataset, load_id):
        return []

    version_labels = store.list_version_labels(conn, dataset)
    unit = ""
    catalog = next((d for d in store.catalog(conn) if d["name"] == dataset), None)
    if catalog and len(catalog["units"]) == 1:
        unit = catalog["units"][0]

    if version_labels:
        summary = _version_summary(conn, llm, dataset, load_id)
    else:
        summary = _load_summary(conn, llm, store.get_load(conn, load_id))
    if summary is None:
        return []

    inserted = []
    created_at = store.now_iso()
    digest_id = store.insert_digest(conn, dataset=dataset, load_id=load_id, kind=summary["kind"],
                                     title=summary["title"], text=summary["text"],
                                     table=summary["table"], created_at=created_at)
    inserted.append({"id": digest_id, **summary})
    for note in _entity_notes(llm, summary["diff_rows"], unit):
        note_id = store.insert_digest(conn, dataset=dataset, load_id=load_id, kind=note["kind"],
                                       title=note["title"], text=note["text"],
                                       table=note["table"], created_at=created_at)
        inserted.append({"id": note_id, **note})
    return inserted


def run(source, db_path: Path, llm, *, dataset: str | None = None) -> dict[str, list[dict]]:
    """One dataset's failure is caught and logged (as an empty result + a printed warning) --
    never stops the others or raises, so a caller running this after a snapshot never turns a
    good snapshot into a failed run."""
    conn = store.connect(db_path)
    try:
        names = [d["name"] for d in store.catalog(conn)]
        if dataset is not None:
            names = [n for n in names if n == dataset]
        results: dict[str, list[dict]] = {}
        for name in names:
            try:
                results[name] = run_digest_for_dataset(conn, llm, name)
            except Exception as exc:  # noqa: BLE001 -- one dataset's digest failure never stops others
                print(f"[경고] {name} 다이제스트 생성 실패: {exc}", flush=True)
                results[name] = []
        return results
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="AI Office 데이터 플랫폼 다이제스트 생성 (스냅샷 이후 실행)")
    parser.add_argument("--db", required=True, help="dataplat.sqlite 경로")
    parser.add_argument("--source", required=True, help="source.yaml 경로")
    parser.add_argument("--env", default=None, help=".env 파일 경로")
    parser.add_argument("--dataset", default=None, help="이 데이터셋만 처리 (기본: 전체)")
    args = parser.parse_args(argv)

    from ..llm.client import LLMClient

    settings = Settings.load(Path(args.env) if args.env else None)
    source = load_source(Path(args.source))
    conn_for_llm = store.connect(Path(args.db))
    try:
        llm = LLMClient(settings, conn_for_llm)
    finally:
        conn_for_llm.close()  # run() opens its own connection

    results = run(source, Path(args.db), llm, dataset=args.dataset)
    for name, rows in results.items():
        if rows:
            print(f"[OK] {name}: 다이제스트 {len(rows)}건 생성", flush=True)
        else:
            print(f"[건너뜀] {name}: 새 다이제스트 없음 (이미 생성됨 또는 비교할 데이터 없음)", flush=True)


if __name__ == "__main__":
    main()
