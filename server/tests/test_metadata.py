from __future__ import annotations

from aioffice.library import metadata


def test_regex_date_handles_report_formats():
    assert metadata.regex_date("Published: 2026-09-19") == "2026-09-19"
    assert metadata.regex_date("발행일: 2026.09.12") == "2026-09-12"
    assert metadata.regex_date("2026/08/20 published") == "2026-08-20"
    assert metadata.regex_date("2026년 9월 3일") == "2026-09-03"
    assert metadata.regex_date("September 19, 2026") == "2026-09-19"
    assert metadata.regex_date("19 September 2026") == "2026-09-19"
    assert metadata.regex_date("no date") is None


def test_regex_date_rejects_an_impossible_calendar_date():
    assert metadata.regex_date("2026-13-45") is None


def test_language_detection_and_broker_aliases():
    assert metadata.detect_language("애플 iPhone 생산 전망입니다") == "ko"
    assert metadata.detect_language("We raise our iPhone build plan for 2026") == "en"
    assert metadata.normalize_broker("  삼성증권  ") == "삼성증권"
    assert metadata.normalize_broker("MORGAN STANLEY GROUP") == "Morgan Stanley"
    assert metadata.normalize_broker("BofA Securities, Inc.") == "BofA Securities"
    assert metadata.normalize_broker("Korea Investment & Securities") == "한국투자증권"
