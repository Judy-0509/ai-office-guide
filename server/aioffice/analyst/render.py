"""Markdown page rendering from vault state, and the small file-writing wrappers learn.py
and viewer.py call after each mutation.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from . import store

BADGE = {"new": "{새로움}", "supports": "{강화}", "updates": "{수정}", "contradicts": "{반대}"}
INVALID_BADGE = "{무효}"
TREND_LABEL = {"up": "상승", "down": "하락", "flat": "보합", "mixed": "혼조"}


def wiki_link(path: str, label: str | None = None) -> str:
    return f"[[{path}|{label}]]" if label else f"[[{path}]]"


def _report_node(report: dict) -> str:
    return f"reports/{Path(report['path']).stem}"


def _topic_node(topic_id: str) -> str:
    return f"topics/{topic_id}"


# --- event -> one Korean line (CLI stdout + log.md) -------------------------------------------


def log_line(event_type: str, data: dict) -> str:
    if event_type == "batch_start":
        return f"[배치 {data['batch']}] {data['n']}건 학습 시작"
    if event_type == "report_start":
        return f"  ({data['i']}/{data['n']}) {data['broker']} · {data['title']} 학습 중..."
    if event_type == "extract_start":
        return "    추출 중..."
    if event_type == "integrate_start":
        return "    통합 중..."
    if event_type == "extracted":
        return f"    추출 완료: 주장 {data['claims']}건 (숫자확인실패 {data['number_fail']}, {data['seconds']}초)"
    if event_type == "integrated":
        return (f"    통합 완료: 신규 {data['new']} 강화 {data['supports']} 수정 {data['updates']} "
                f"반대 {data['contradicts']} ({data['seconds']}초)")
    if event_type == "report_done":
        return f"    저장: {data['path']}"
    if event_type == "consolidated":
        return f"[배치 정리] 주제 {data['topics']}개 갱신, 관계 {data['relations']}개 ({data['seconds']}초)"
    if event_type == "batch_done":
        return f"[배치 완료] {data['path']}"
    if event_type == "review_done":
        return f"검토: {data['report_id'][:8]} -> {data['verdict']}"
    if event_type == "error":
        return f"    경고: {data['text']}"
    return f"{event_type}: {data}"


# --- report page ------------------------------------------------------------------------------


def report_markdown(state: "store.AnalystState", report_id: str) -> str:
    report = state.report_by_id(report_id)
    claims = state.claims_for_report(report_id)

    fm = {"id": report["id"], "broker": report["broker"], "date": report["date"],
          "title": report["title"], "review": report.get("review"), "status": report["status"]}
    lines = ["---", yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).rstrip("\n"), "---",
              "", f"# {report['broker']} · {report['title']}", "", "## 요약"]

    for c in claims:
        topic = state.topics.get(c.get("topic_id")) or {}
        topic_link = wiki_link(_topic_node(c["topic_id"]), topic.get("name", "")) if c.get("topic_id") else ""
        if c.get("valid", True):
            badge = BADGE.get(c.get("relation"), "")
            lines.append(f"- {c['text']} {badge} — {topic_link}".rstrip())
        else:
            note = ""
            invalidator = state.claim_by_id(c.get("invalidated_by"))
            if invalidator is not None:
                inv_report = state.report_by_id(invalidator["report_id"])
                if inv_report is not None:
                    link = wiki_link(_report_node(inv_report), invalidator["report_date"])
                    note = f" → {link}에 의해 무효 ({invalidator['report_date']})"
            lines.append(f"- ~~{c['text']}~~ {INVALID_BADGE}{note} — {topic_link}".rstrip())
    if not claims:
        lines.append("(없음)")

    lines += ["", "## 기존 기억과 비교"]
    compare_lines = []
    for c in claims:
        target_id = c.get("target_claim_id")
        if c.get("relation") in ("updates", "contradicts") and target_id:
            target = state.claim_by_id(target_id)
            if target is not None:
                old_label = f"{target['broker']} {target['report_date']}"
                new_label = f"{report['broker']} {report['date']}"
                compare_lines.append(
                    f"- {old_label} \"{target['text']}\" -> {new_label} \"{c['text']}\"")
    lines += compare_lines or ["(없음)"]

    lines += ["", "## 원문 근거"]
    for c in claims:
        lines.append(f"- {c['text']} ({', '.join(c['sids'])})")
        lines.append(f"  > {c['quote']}")
    if not claims:
        lines.append("(없음)")

    lines += ["", "## 숫자 확인 필요"]
    bad = [c for c in claims if not c.get("number_ok", True)]
    lines += [f"- {c['text']} (값: {c.get('value')})" for c in bad] or ["(없음)"]

    return "\n".join(lines) + "\n"


def write_report_page(vault: Path, state: "store.AnalystState", report_id: str) -> str:
    report = state.report_by_id(report_id)
    path = Path(vault) / report["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report_markdown(state, report_id), encoding="utf-8")
    return report["path"]


# --- topic page -------------------------------------------------------------------------------


def topic_markdown(state: "store.AnalystState", topic_id: str) -> str:
    topic = state.topics[topic_id]
    claims = sorted([c for c in state.claims if c.get("topic_id") == topic_id],
                     key=lambda c: c["report_date"], reverse=True)

    fm = {"id": topic_id, "name": topic["name"]}
    trend_label = TREND_LABEL.get(topic.get("trend"), topic.get("trend"))
    lines = ["---", yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).rstrip("\n"), "---",
              "", f"# {topic['name']}", "", "## 현재 판단",
              f"{topic.get('summary') or '(요약 없음)'} (추세: {trend_label})", "", "## 흐름"]
    for c in claims:
        report = state.report_by_id(c["report_id"])
        link = wiki_link(_report_node(report), f"{c['broker']} {c['report_date']}") if report else ""
        text = c["text"] if c.get("valid", True) else f"~~{c['text']}~~"
        badge = BADGE.get(c.get("relation"), "") if c.get("valid", True) else INVALID_BADGE
        lines.append(f"- {c['report_date']} · {c['broker']} · {text} {badge} — {link}".rstrip())
    if not claims:
        lines.append("(없음)")

    lines += ["", "## 관련 주제"]
    related = [r for r in state.relations if r["from"] == topic_id or r["to"] == topic_id]
    for r in related:
        other = r["to"] if r["from"] == topic_id else r["from"]
        other_name = state.topics.get(other, {}).get("name", other)
        link = wiki_link(_topic_node(other), other_name)
        lines.append(f"- {r['kind']}: {link} — {r.get('why', '')}".rstrip())
    if not related:
        lines.append("(없음)")

    lines += ["", "## 판단 변화"]
    history = topic.get("summary_history") or []
    for h in history:
        h_trend = TREND_LABEL.get(h.get("trend"), h.get("trend"))
        lines.append(f"- {h['date']}: {h['summary']} (추세: {h_trend})")
    if not history:
        lines.append("(없음)")

    lines += ["", "## 언급 기업·제품"]
    entities: dict[str, list[str]] = {}
    for c in claims:
        for name in c.get("entities") or []:
            entities.setdefault(name, []).append(c["report_date"])
    for name, dates in sorted(entities.items()):
        link = wiki_link(f"entities/{store.safe_entity_name(name)}", name)
        lines.append(f"- {link}: {len(dates)}회 (최근 {max(dates)})")
    if not entities:
        lines.append("(없음)")

    return "\n".join(lines) + "\n"


def write_topic_page(vault: Path, state: "store.AnalystState", topic_id: str) -> str:
    path = Path(vault) / "topics" / f"{topic_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(topic_markdown(state, topic_id), encoding="utf-8")
    return f"topics/{topic_id}.md"


# --- entity page ------------------------------------------------------------------------------


def entity_markdown(state: "store.AnalystState", entity_name: str) -> str:
    claims = sorted([c for c in state.claims if entity_name in (c.get("entities") or [])],
                     key=lambda c: c["report_date"], reverse=True)
    lines = [f"# {entity_name}", "", f"- 총 언급: {len(claims)}회", "", "## 언급 내역"]
    for c in claims:
        report = state.report_by_id(c["report_id"])
        topic = state.topics.get(c.get("topic_id")) or {}
        report_link = wiki_link(_report_node(report), f"{c['broker']} {c['report_date']}") if report else ""
        topic_link = wiki_link(_topic_node(c["topic_id"]), topic.get("name", "")) if c.get("topic_id") else ""
        lines.append(f"- {c['report_date']} · {c['text']} — {report_link} · {topic_link}".rstrip())
    if not claims:
        lines.append("(없음)")
    return "\n".join(lines) + "\n"


def write_entity_pages(vault: Path, state: "store.AnalystState", entity_names: set[str]) -> None:
    for name in entity_names:
        path = Path(vault) / "entities" / f"{store.safe_entity_name(name)}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(entity_markdown(state, name), encoding="utf-8")


# --- index / log / batch ------------------------------------------------------------------------


def index_markdown(state: "store.AnalystState") -> str:
    learned = [r for r in state.reports if r["status"] == "learned"]
    recent_changes = []
    for tid, t in state.topics.items():
        history = t.get("summary_history") or []
        if history:
            recent_changes.append((history[-1]["date"], tid, t["name"]))
    recent_changes.sort(reverse=True)

    lines = ["# 개요", "", f"- 학습한 리포트 수: {len(learned)}", f"- 주제 수: {len(state.topics)}",
              "", "## 최근 바뀐 판단"]
    lines += [f"- {date}: {wiki_link(_topic_node(tid), name)}"
              for date, tid, name in recent_changes[:10]] or ["(없음)"]

    lines += ["", "## 최근 배치"]
    batches_dir = Path(state.vault) / "batches"
    batch_files = sorted(batches_dir.glob("*.md")) if batches_dir.exists() else []
    lines += [f"- {wiki_link(f'batches/{p.stem}', p.stem)}" for p in batch_files[-5:][::-1]] or ["(없음)"]

    return "\n".join(lines) + "\n"


def write_index_and_log(vault: Path, state: "store.AnalystState") -> None:
    (Path(vault) / "index.md").write_text(index_markdown(state), encoding="utf-8")


def append_log_line(vault: Path, line: str) -> None:
    path = Path(vault) / "log.md"
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def batch_markdown(state: "store.AnalystState", batch_n: int, report_results: list[dict],
                    touched_topics: set[str]) -> str:
    lines = [f"# 배치 {batch_n:03d}", "", "## 새 주제"]
    new_topics: dict[str, str] = {}
    invalid_lines = []
    for r in report_results:
        cs = r["change_summary"]
        for t in cs["topics_created"]:
            new_topics[t["id"]] = t["name"]
        for inv in cs["invalidated"]:
            invalid_lines.append(f"- {inv['text']} ({inv.get('topic_name', '')}) — by {inv['by_claim_id']}")
    lines += [f"- {wiki_link(_topic_node(tid), name)}" for tid, name in new_topics.items()] or ["(없음)"]

    lines += ["", "## 판단 갱신"]
    update_lines = []
    for tid in sorted(touched_topics):
        topic = state.topics.get(tid)
        if not topic:
            continue
        history = topic.get("summary_history") or []
        before = history[-2]["summary"] if len(history) >= 2 else "(이전 없음)"
        after = history[-1]["summary"] if history else topic.get("summary", "")
        update_lines.append(f"- {wiki_link(_topic_node(tid), topic['name'])}: {before} → {after}")
    lines += update_lines or ["(없음)"]

    lines += ["", "## 무효화"]
    lines += invalid_lines or ["(없음)"]

    lines += ["", "## 새 관계"]
    relation_lines = []
    for r in report_results:
        for rel in r["change_summary"].get("relations_created", []):
            from_name = state.topics.get(rel["from"], {}).get("name", rel["from"])
            to_name = state.topics.get(rel["to"], {}).get("name", rel["to"])
            relation_lines.append(f"- {from_name} --{rel['kind']}--> {to_name}")
    lines += relation_lines or ["(없음)"]

    lines += ["", "## 리포트별 요약"]
    for r in report_results:
        report = r["report"]
        link = wiki_link(_report_node(report), f"{report['broker']} {report['title']}")
        lines.append(f"- {link}: 주장 {len(r['processed_claims'])}건 "
                      f"(추출 {round(r['extract_seconds'], 1)}s, 통합 {round(r['integrate_seconds'], 1)}s)")
    if not report_results:
        lines.append("(없음)")

    return "\n".join(lines) + "\n"


def write_batch_page(vault: Path, state: "store.AnalystState", batch_n: int,
                      report_results: list[dict], touched_topics: set[str]) -> str:
    path = Path(vault) / "batches" / f"{batch_n:03d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(batch_markdown(state, batch_n, report_results, touched_topics), encoding="utf-8")
    return f"batches/{batch_n:03d}.md"
