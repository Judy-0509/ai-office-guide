from __future__ import annotations

from pathlib import Path

from aioffice.analyst import ingest


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --- front matter vs fallback rules --------------------------------------------------------


def test_extract_metadata_prefers_front_matter(tmp_path):
    content = "---\nbroker: A증권\ntitle: 프론트매터 제목\ndate: 2026-07-03\n---\n\n본문입니다.\n"
    meta, body = ingest.extract_metadata(content, tmp_path / "r.md")
    assert meta == {"broker": "A증권", "title": "프론트매터 제목", "date": "2026-07-03",
                     "date_source": "front_matter", "fictional": False}
    assert body.strip() == "본문입니다."


def test_extract_metadata_falls_back_to_first_heading_and_regex_date(tmp_path):
    content = "# 첫 헤딩 제목\n\n2026년 8월 14일 발표된 내용이다.\n"
    meta, _body = ingest.extract_metadata(content, tmp_path / "r.md")
    assert meta["title"] == "첫 헤딩 제목"
    assert meta["date"] == "2026-08-14"
    assert meta["date_source"] == "regex"


def test_extract_metadata_falls_back_to_first_line_when_no_heading(tmp_path):
    content = "그냥 첫 줄\n\n본문\n"
    path = _write(tmp_path / "r.md", content)
    meta, _body = ingest.extract_metadata(content, path)
    assert meta["title"] == "그냥 첫 줄"


def test_extract_metadata_uses_mtime_when_no_date_found(tmp_path):
    path = _write(tmp_path / "r.md", "# 제목\n\n날짜 없는 본문\n")
    meta, _body = ingest.extract_metadata(path.read_text(encoding="utf-8"), path)
    assert meta["date_source"] == "mtime"
    assert meta["date"]  # some ISO date string


def test_extract_metadata_guesses_broker_from_body_when_no_front_matter(tmp_path):
    content = "# 제목\n\n삼성증권 리서치센터가 발간한 자료. 2026-07-01 발간.\n"
    path = _write(tmp_path / "r.md", content)
    meta, _body = ingest.extract_metadata(content, path)
    assert meta["broker"] == "삼성증권"


def test_extract_metadata_reads_fictional_flag(tmp_path):
    content = "---\nbroker: A증권\ntitle: t\ndate: 2026-07-01\nfictional: true\n---\n\n본문\n"
    meta, _body = ingest.extract_metadata(content, tmp_path / "r.md")
    assert meta["fictional"] is True


# --- ordering ----------------------------------------------------------------------------


def test_scan_inbox_orders_pending_by_date_then_broker_then_title(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write(inbox / "z.md", "---\nbroker: Z증권\ntitle: 늦은순서\ndate: 2026-07-01\n---\n\n본문\n")
    _write(inbox / "a.md", "---\nbroker: A증권\ntitle: 이른순서\ndate: 2026-07-01\n---\n\n본문\n")
    _write(inbox / "b.md", "---\nbroker: A증권\ntitle: 더 이른 날짜\ndate: 2026-06-01\n---\n\n본문\n")

    pending, warnings = ingest.scan_inbox(inbox, learned_ids=set())
    assert warnings == []
    assert [p.title for p in pending] == ["더 이른 날짜", "이른순서", "늦은순서"]


def test_scan_inbox_skips_already_learned_ids(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    path = _write(inbox / "a.md", "---\nbroker: A증권\ntitle: t\ndate: 2026-07-01\n---\n\n본문\n")
    report_id = ingest.file_id(path)

    pending, _warnings = ingest.scan_inbox(inbox, learned_ids={report_id})
    assert pending == []


def test_scan_inbox_ignores_unrelated_file_types(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write(inbox / "a.md", "---\nbroker: A증권\ntitle: t\ndate: 2026-07-01\n---\n\n본문\n")
    _write(inbox / "notes.docx", "irrelevant")
    pending, _warnings = ingest.scan_inbox(inbox, learned_ids=set())
    assert len(pending) == 1


def test_next_batch_takes_the_first_n():
    reports = [ingest.PendingReport(id=str(i), path=Path(f"{i}.md"), text="", broker="",
                                     title="", date="", date_source="mtime") for i in range(5)]
    assert [r.id for r in ingest.next_batch(reports, 2)] == ["0", "1"]


# --- pdf skip warning ------------------------------------------------------------------------


def test_pdf_without_docling_or_pymupdf_is_skipped_with_korean_warning(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "r.pdf").write_bytes(b"%PDF-1.4 fake")

    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in ("docling", "docling.document_converter", "docling.datamodel.base_models",
                    "docling.datamodel.pipeline_options", "fitz"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    pending, warnings = ingest.scan_inbox(inbox, learned_ids=set())
    assert pending == []
    assert len(warnings) == 1
    assert "PDF" in warnings[0] and "r.pdf" in warnings[0]


def test_pdf_uses_pymupdf_when_available(tmp_path):
    # pymupdf (fitz) is installed in this venv -- exercise the real path against a tiny PDF.
    import fitz

    pdf_path = tmp_path / "r.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "안녕하세요 PDF 테스트")
    doc.save(str(pdf_path))
    doc.close()

    text, warn = ingest.load_text(pdf_path)
    assert warn is None
    assert "PDF" in text or "안녕" in text


# --- sentence numbering ----------------------------------------------------------------------


def test_split_sentences_drops_short_fragments_and_splits_on_enders():
    text = "아이폰 생산량이 큰 폭으로 감소했다. 이는 수요 둔화를 의미한다.\n\n짧다.\n"
    sentences = ingest.split_sentences(text)
    assert any("아이폰" in s for s in sentences)
    assert all(len(s) >= 15 for s in sentences)
    assert not any(s == "짧다." for s in sentences)


def test_number_sentences_caps_at_max_chars_and_ids_from_s1():
    text = "문장 하나입니다 충분히 길게. " * 50
    numbered, sid_map = ingest.number_sentences(text, max_chars=100)
    assert "S1" in sid_map
    assert numbered.startswith("[S1]")
    total_chars = sum(len(s) for s in sid_map.values())
    assert total_chars < len(text)  # capped, didn't keep every sentence
