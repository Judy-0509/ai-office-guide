from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path

import numpy as np
import yaml

from aioffice.library import mdlibrary as ml
from aioffice.tools import stack_reports

# --- synthetic report markdown (broker/company names from brokers.yaml/companies.yaml) --------

KR_REPORT_WITH_FRONT_MATTER = """---
broker: 삼성증권
title: 삼성전자 카메라 모듈 전망
pub_date: 2026-09-19
companies: [Samsung Electronics]
analyst: 홍길동
---

# 삼성전자 카메라 모듈 전망

## 요약
- Apple 공급망 확대
- 카메라 화소 증가

## 부품 동향
<!-- p.2 -->
이번 분기 카메라 모듈 출하량이 확대되었습니다. 관련 부품 수요가 늘었습니다.

## 면책조항
<!-- p.3 -->
This report is subject to Regulatory Disclosures.
"""

EN_REPORT_NO_FRONT_MATTER = """# Global Panel Supply Outlook

Morgan Stanley Research | Published: 2026-09-12

## Key takeaways
- Apple demand improving
- Panel supply tightening

## Overview
<!-- page 2 -->
Our supply chain checks indicate panel shipments are recovering this quarter.

## Important Disclosures
<!-- page 3 -->
This report is subject to Regulatory Disclosures and is intended for institutional clients only.
"""

NO_DATE_REPORT = """# 무제 리포트

특정 날짜 언급이 전혀 없는 리포트 본문입니다.
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _zip_two_reports(tmp_path: Path) -> Path:
    kr = _write(tmp_path, "kr_report.md", KR_REPORT_WITH_FRONT_MATTER)
    en = _write(tmp_path, "en_report.md", EN_REPORT_NO_FRONT_MATTER)
    zip_path = tmp_path / "batch.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(kr, "kr_report.md")
        zf.write(en, "en_report.md")
    return zip_path


# --- Test 1: stack a folder with a front-matter report and a plain-markdown report -----------


def test_stack_folder_creates_md_catalog_index(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    _write(src, "kr_report.md", KR_REPORT_WITH_FRONT_MATTER)
    _write(src, "en_report.md", EN_REPORT_NO_FRONT_MATTER)
    library = tmp_path / "library"

    rc = stack_reports.main([str(src), "--library", str(library), "--no-embed"])
    assert rc == 0

    # the two synthetic reports land in different ISO weeks (different pub_dates), so
    # target report files directly rather than rglob("*.md") -- that would also pick up
    # originals/*.md, which is markdown too now that the input itself is markdown.
    md_files = sorted(library.glob("*/reports/*.md"))
    assert len(md_files) == 2

    for md in md_files:
        text = md.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        front, _sep, body = text[4:].partition("\n---\n")
        meta = yaml.safe_load(front)
        assert meta["id"] and meta["broker"] and meta["title"]
        assert meta["converter"] == "md-v1"
        # the original body survives untouched -- including disclaimer text this pipeline
        # never removes (that step belongs to the upstream company pipeline, not here).
        assert "Regulatory Disclosures" in body

    originals = list((library / "originals").glob("*.md"))
    assert len(originals) == 2

    catalog = ml.read_catalog(library)
    assert len(catalog) == 2
    brokers = {r["broker"] for r in catalog}
    assert "삼성증권" in brokers and "Morgan Stanley" in brokers

    for r in catalog:
        filename = Path(r["md_path"]).name
        index_text = (library / r["week"] / "INDEX.md").read_text(encoding="utf-8")
        assert f"[{filename}](reports/{filename})" in index_text


# --- Test 2: front matter fields are used as-is, including merging an extra field -------------


def test_front_matter_fields_are_used_as_is_and_extra_fields_merge_through(tmp_path: Path):
    path = _write(tmp_path, "kr_report.md", KR_REPORT_WITH_FRONT_MATTER)
    library = tmp_path / "library"

    results = ml.stack_batch([path], library, embed=False)
    assert results[0].status == "ok"

    text = (library / results[0].md_path).read_text(encoding="utf-8")
    front, _sep, _body = text[4:].partition("\n---\n")
    meta = yaml.safe_load(front)
    assert meta["broker"] == "삼성증권"
    assert meta["title"] == "삼성전자 카메라 모듈 전망"
    assert meta["pub_date"] == "2026-09-19"
    assert meta["date_source"] == "front_matter"
    assert meta["companies"] == ["Samsung Electronics"]
    assert meta["analyst"] == "홍길동"  # not one of our own fields -- merged through


# --- Test 3: no front matter -- title/broker/pub_date/companies are all guessed --------------


def test_missing_front_matter_falls_back_to_heading_broker_alias_and_regex_date(tmp_path: Path):
    path = _write(tmp_path, "en_report.md", EN_REPORT_NO_FRONT_MATTER)
    library = tmp_path / "library"

    results = ml.stack_batch([path], library, embed=False)
    assert results[0].status == "ok"

    text = (library / results[0].md_path).read_text(encoding="utf-8")
    front, _sep, _body = text[4:].partition("\n---\n")
    meta = yaml.safe_load(front)
    assert meta["title"] == "Global Panel Supply Outlook"
    assert meta["broker"] == "Morgan Stanley"
    assert meta["pub_date"] == "2026-09-12"
    assert meta["date_source"] == "regex"
    assert "Apple" in meta["companies"]


# --- Test 3b: BOM-prefixed input (the company pipeline writes utf-8-sig) is stripped ----------


def test_bom_prefixed_front_matter_report_is_parsed_and_library_copy_has_no_bom(tmp_path: Path):
    path = tmp_path / "kr_report.md"
    path.write_text(KR_REPORT_WITH_FRONT_MATTER, encoding="utf-8-sig")
    library = tmp_path / "library"

    results = ml.stack_batch([path], library, embed=False)
    assert results[0].status == "ok"

    text = (library / results[0].md_path).read_text(encoding="utf-8")
    assert not text.startswith("﻿")
    front, _sep, _body = text[4:].partition("\n---\n")
    meta = yaml.safe_load(front)
    assert meta["broker"] == "삼성증권"
    assert meta["title"] == "삼성전자 카메라 모듈 전망"
    assert meta["pub_date"] == "2026-09-19"


def test_bom_prefixed_report_without_front_matter_falls_back_to_heading_title(tmp_path: Path):
    path = tmp_path / "en_report.md"
    path.write_text(EN_REPORT_NO_FRONT_MATTER, encoding="utf-8-sig")
    library = tmp_path / "library"

    results = ml.stack_batch([path], library, embed=False)
    assert results[0].status == "ok"

    text = (library / results[0].md_path).read_text(encoding="utf-8")
    assert not text.startswith("﻿")
    front, _sep, _body = text[4:].partition("\n---\n")
    meta = yaml.safe_load(front)
    assert meta["title"] == "Global Panel Supply Outlook"
    assert meta["broker"] == "Morgan Stanley"
    assert meta["pub_date"] == "2026-09-12"


# --- Test 4: re-running skips as duplicate; --force restacks without duplicating rows --------


def test_rerun_skips_duplicate_then_force_restacks(tmp_path: Path):
    zip_path = _zip_two_reports(tmp_path)
    library = tmp_path / "library"

    stack_reports.main([str(zip_path), "--library", str(library), "--no-embed"])
    catalog_after_first = ml.read_catalog(library)
    assert len(catalog_after_first) == 2

    stack_reports.main([str(zip_path), "--library", str(library), "--no-embed"])
    catalog_after_rerun = ml.read_catalog(library)
    assert len(catalog_after_rerun) == 2  # no duplicate lines

    total_rows = sum(
        index_file.read_text(encoding="utf-8").count("](reports/")
        for index_file in library.rglob("INDEX.md")
    )
    assert total_rows == 2  # still 2 table rows total (across both weeks), not duplicated to 4

    stack_reports.main([str(zip_path), "--library", str(library), "--no-embed", "--force"])
    catalog_after_force = ml.read_catalog(library)
    assert len(catalog_after_force) == 2
    assert {r["id"] for r in catalog_after_force} == {r["id"] for r in catalog_after_first}


# --- Test 5: unknown date -> nodate_ filename, date_unknown flag, current ISO week -----------


def test_unknown_date_report_uses_nodate_filename(tmp_path: Path):
    path = _write(tmp_path, "nodate.md", NO_DATE_REPORT)
    library = tmp_path / "library"

    results = ml.stack_batch([path], library, embed=False)
    assert results[0].status == "ok"

    expected_week = ml.iso_week(date.today())
    md_path = Path(results[0].md_path)
    assert md_path.name.startswith("nodate_")
    assert md_path.parts[0] == expected_week

    catalog = ml.read_catalog(library)
    assert catalog[0]["date_unknown"] is True
    assert catalog[0]["week"] == expected_week

    full_text = (library / results[0].md_path).read_text(encoding="utf-8")
    assert "date_unknown: true" in full_text
    assert "pub_date: null" in full_text


# --- Test 6: company word-boundary matching + ordering by hit count --------------------------


def test_guess_companies_word_boundary_and_ordering():
    companies = {"Apple": [], "LG Innotek": ["LG"]}

    # "Apple's" -- trailing apostrophe is not a letter, so the boundary still matches.
    assert "Apple" in ml.guess_companies("Apple's new phone sold well.", companies)
    # "LGD" -- trailing 'D' is a letter, so the bare "LG" alias must NOT match inside it.
    assert "LG Innotek" not in ml.guess_companies("LGD 부품 공급 확대 소식", companies)
    # a real mention of "LG Innotek" still matches.
    assert "LG Innotek" in ml.guess_companies("LG Innotek 실적이 개선되었습니다.", companies)

    # ordering by hit count: Apple mentioned twice outranks a single LG Innotek mention.
    text = "Apple 공급망 확대. Apple 수요도 견조. LG Innotek 실적도 개선."
    assert ml.guess_companies(text, companies) == ["Apple", "LG Innotek"]


# --- Test 7: search chunking -- heading split, page markers, size-based merge -----------------


def test_chunk_body_splits_by_heading_and_reads_page_markers():
    body = (
        "## 부품 동향\n\n"
        "<!-- p.2 -->\n"
        "이번 분기 카메라 모듈 출하량이 확대되었습니다.\n\n"
        "## 면책조항\n\n"
        "<!-- page 3 -->\n"
        "This report is subject to Regulatory Disclosures.\n"
    )
    chunks = ml._chunk_body(body)
    assert [c.section for c in chunks] == ["부품 동향", "면책조항"]
    assert [c.page for c in chunks] == [2, 3]
    assert "<!-- p.2 -->" not in chunks[0].text


def test_chunk_body_page_is_null_without_a_recognizable_marker():
    chunks = ml._chunk_body("## 본문\n\n마커가 전혀 없는 문단입니다.")
    assert chunks[0].page is None


def test_chunk_body_merges_paragraphs_within_a_section_until_the_token_budget():
    paragraph = "가" * 800  # well over the ~350-estimated-token (1400 char) budget alone
    body = "## 섹션\n\n" + "\n\n".join([paragraph] * 3)
    chunks = ml._chunk_body(body)
    assert len(chunks) > 1
    assert all(c.section == "섹션" for c in chunks)


# --- helpers for search-index tests (index a body string directly) ---------------------------


def _index_fake_doc(conn, doc_id: str, *, week: str, broker: str, title: str, body: str,
                     env: dict | None = None, embed: bool = True) -> None:
    meta = ml.ReportMeta(id=doc_id, id12=doc_id[:12], id8=doc_id[:8], broker=broker, title=title,
                          pub_date="2026-01-05", date_source="regex", date_unknown=False, week=week)
    ml.index_document(conn, meta, body, f"{week}/reports/{doc_id[:8]}.md", env or {}, embed=embed)


# --- Test 8: FTS finds a Korean substring even when query/text spacing differs ---------------


def test_fts_search_finds_korean_substring_across_space_mismatch(tmp_path: Path):
    library = tmp_path / "library"
    conn = ml.open_search_db(library)
    try:
        _index_fake_doc(
            conn, "a" * 16, week="2026-W02", broker="삼성증권", title="반도체 전망",
            body="이번 분기 생산감축 가능성은 낮다는 판단입니다.", embed=False,
        )
        _index_fake_doc(
            conn, "b" * 16, week="2026-W02", broker="미래에셋증권", title="다른 리포트",
            body="전혀 관련 없는 내용의 문장입니다.", embed=False,
        )
    finally:
        conn.close()

    results = ml.search_chunks(library, "생산 감축", env={})
    assert any("생산감축" in r["text"] for r in results)

    filtered = ml.search_chunks(library, "생산 감축", broker="삼성증권", env={})
    assert filtered and all(r["broker"] == "삼성증권" for r in filtered)

    filtered_out = ml.search_chunks(library, "생산 감축", broker="미래에셋증권", env={})
    assert not filtered_out


# --- Test 9: vector search (monkeypatched embedder), RRF, per-report cap, rerank fallback ----


def _fake_embed_factory():
    """Maps text containing "[HIT]" to one unit vector, everything else to another."""
    def fake_embed(texts, env):
        out = []
        for t in texts:
            v = np.array([1.0, 0.0], dtype=np.float32) if "[HIT]" in t else np.array([0.0, 1.0], dtype=np.float32)
            out.append(v.tolist())
        return out
    return fake_embed


def test_vector_search_rrf_merge_and_per_report_cap(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ml, "embed_texts", _fake_embed_factory())
    library = tmp_path / "library"
    env = {"EMBED_BASE_URL": "http://fake-embed.local"}

    conn = ml.open_search_db(library)
    try:
        # doc A has three chunks (one per heading) that all vector-match -- only 2 come back.
        _index_fake_doc(
            conn, "c" * 16, week="2026-W03", broker="삼성증권", title="문서 A", env=env,
            body=(
                "## 하나\n\n[HIT] 첫 번째 관련 내용\n\n"
                "## 둘\n\n[HIT] 두 번째 관련 내용\n\n"
                "## 셋\n\n[HIT] 세 번째 관련 내용\n"
            ),
        )
        # doc B has one vector-matching chunk with no literal FTS overlap with the query text.
        _index_fake_doc(
            conn, "d" * 16, week="2026-W03", broker="미래에셋증권", title="문서 B", env=env,
            body="[HIT] 완전히 다른 표현의 관련 내용",
        )
        # an unrelated chunk that should rank last (opposite embedding bucket, no literal match).
        _index_fake_doc(
            conn, "e" * 16, week="2026-W03", broker="키움증권", title="문서 C", env=env,
            body="이 내용은 전혀 무관합니다.",
        )
    finally:
        conn.close()

    results = ml.search_chunks(library, "[HIT] 이 쿼리와 무관한 표현", k=8, env=env)
    per_report: dict[str, int] = {}
    for r in results:
        per_report[r["file"]] = per_report.get(r["file"], 0) + 1
    assert all(count <= 2 for count in per_report.values())

    doc_a_file = "2026-W03/reports/cccccccc.md"
    doc_b_file = "2026-W03/reports/dddddddd.md"
    doc_c_file = "2026-W03/reports/eeeeeeee.md"
    assert per_report.get(doc_a_file) == 2  # capped from 3 matching chunks down to 2
    assert per_report.get(doc_b_file) == 1
    # doc C has neither a literal "[HIT]" (FTS) nor a matching embedding (vector) hit, so
    # every doc A/B result it shares the list with must outrank it.
    scores = {r["file"]: r["score"] for r in results}
    if doc_c_file in scores:
        assert all(scores[doc_c_file] <= scores[f] for f in (doc_a_file, doc_b_file) if f in scores)


def test_reranker_failure_falls_back_to_rrf_order(monkeypatch):
    merged = [({"id": 1, "doc_id": "x", "text": "a"}, 0.9), ({"id": 2, "doc_id": "y", "text": "b"}, 0.5)]

    def _raise_post(self, *args, **kwargs):
        raise RuntimeError("reranker unreachable")

    monkeypatch.setattr(ml.httpx.Client, "post", _raise_post)
    out = ml._rerank("query", merged, {"RERANK_BASE_URL": "http://fake-rerank.local"})
    assert out == merged


def test_rerank_reorders_on_success(monkeypatch):
    merged = [({"id": 1, "doc_id": "x", "text": "a"}, 0.9), ({"id": 2, "doc_id": "y", "text": "b"}, 0.5)]

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"index": 1, "relevance_score": 0.99},
                                 {"index": 0, "relevance_score": 0.1}]}

    def _fake_post(self, url, json=None, headers=None):
        return _Resp()

    monkeypatch.setattr(ml.httpx.Client, "post", _fake_post)
    out = ml._rerank("query", merged, {"RERANK_BASE_URL": "http://fake-rerank.local"})
    assert [row["id"] for row, _score in out] == [2, 1]


# --- Test 10: list_reports filters by week / broker / company ---------------------------------


def test_list_reports_filters(tmp_path: Path):
    library = tmp_path / "library"
    library.mkdir(parents=True)
    ml.upsert_catalog(library, {
        "id": "1" * 16, "broker": "삼성증권", "title": "리포트 A", "pub_date": "2026-01-05",
        "week": "2026-W02", "companies": ["Apple"], "type": "company", "md_path": "2026-W02/reports/a.md",
        "summary": "요약 A",
    })
    ml.upsert_catalog(library, {
        "id": "2" * 16, "broker": "미래에셋증권", "title": "리포트 B", "pub_date": "2026-01-06",
        "week": "2026-W02", "companies": ["Samsung Electronics"], "type": "company",
        "md_path": "2026-W02/reports/b.md", "summary": "요약 B",
    })
    ml.upsert_catalog(library, {
        "id": "3" * 16, "broker": "삼성증권", "title": "리포트 C", "pub_date": "2026-01-10",
        "week": "2026-W03", "companies": ["Apple"], "type": "company", "md_path": "2026-W03/reports/c.md",
        "summary": "요약 C",
    })

    assert {r["title"] for r in ml.list_reports(library, week="2026-W02")} == {"리포트 A", "리포트 B"}
    assert {r["title"] for r in ml.list_reports(library, broker="삼성증권")} == {"리포트 A", "리포트 C"}
    assert {r["title"] for r in ml.list_reports(library, company="Apple")} == {"리포트 A", "리포트 C"}
    assert ml.list_reports(library, week="2026-W02", broker="삼성증권") == [
        r for r in ml.list_reports(library, week="2026-W02") if r["broker"] == "삼성증권"
    ]
