"""Inbox scanning: file discovery, text extraction, metadata guessing, sentence numbering."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import yaml

from ..library.metadata import BROKERS_PATH, normalize_broker, regex_date

MAX_SENTENCE_CHARS = 12000
_TEXT_SUFFIXES = (".md", ".txt")
_ALL_SUFFIXES = (".md", ".txt", ".pdf")
_FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_ENDER_RE = re.compile(r"(?<=[.!?。])\s+")


@dataclass
class PendingReport:
    """One inbox file not yet learned, with its metadata already guessed."""

    id: str
    path: Path
    text: str  # body, front matter stripped
    broker: str
    title: str
    date: str
    date_source: str  # "front_matter" | "regex" | "mtime"
    fictional: bool = False


# --- text extraction ------------------------------------------------------------------------


def _load_pdf(path: Path) -> tuple[str | None, str | None]:
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError:
        pass
    else:
        options = PdfPipelineOptions(do_ocr=False, do_table_structure=False)
        converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )
        return converter.convert(str(path)).document.export_to_markdown(), None

    try:
        import fitz  # pymupdf
    except ImportError:
        return None, f"PDF 변환 라이브러리(docling/pymupdf)가 없어 건너뜁니다: {path.name}"
    with fitz.open(str(path)) as doc:
        return "\n".join(page.get_text("text", sort=True) for page in doc), None


def load_text(path: Path) -> tuple[str | None, str | None]:
    """(text, warning). `warning` is a Korean message when the file was skipped."""
    suffix = path.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        return path.read_text(encoding="utf-8-sig"), None
    if suffix == ".pdf":
        return _load_pdf(path)
    return None, f"지원하지 않는 파일 형식이라 건너뜁니다: {path.name}"


# --- metadata ------------------------------------------------------------------------------


def _split_front_matter(content: str) -> tuple[dict, str]:
    match = _FRONT_MATTER_RE.match(content)
    if not match:
        return {}, content
    try:
        fm = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}, content
    return (fm if isinstance(fm, dict) else {}), content[match.end():]


def _first_heading_or_line(body: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        heading = _HEADING_RE.match(line)
        return heading.group(2).strip() if heading else line
    return ""


@lru_cache(maxsize=1)
def _broker_aliases(path: str = str(BROKERS_PATH)) -> tuple[str, ...]:
    records = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    aliases: set[str] = set()
    for canonical, names in records.items():
        aliases.add(str(canonical))
        aliases.update(str(n) for n in (names or []) if n)
    return tuple(sorted(aliases, key=len, reverse=True))


def guess_broker(text: str) -> str:
    """The longest known broker alias appearing in `text`, canonicalized.

    ponytail: a substring scan over brokers.yaml aliases, not NER -- good enough for a
    known, small set of institutions; front matter is the primary path in practice.
    """
    for alias in _broker_aliases():
        if alias in text:
            return normalize_broker(alias)
    return ""


def extract_metadata(content: str, path: Path) -> tuple[dict, str]:
    """(meta, body). meta: broker, title, date, date_source, fictional."""
    fm, body = _split_front_matter(content)

    title = str(fm.get("title") or "").strip() or _first_heading_or_line(body) or path.name
    broker = str(fm.get("broker") or "").strip() or guess_broker(body[:1500])

    date_raw = fm.get("date") or fm.get("pub_date")
    if date_raw:
        date_val, date_source = str(date_raw), "front_matter"
    else:
        regex_hit = regex_date(body[:2000])
        if regex_hit:
            date_val, date_source = regex_hit, "regex"
        else:
            date_val = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
            date_source = "mtime"

    fictional = bool(fm.get("fictional", False))
    return {"broker": broker, "title": title, "date": date_val, "date_source": date_source,
            "fictional": fictional}, body


# --- sentence numbering ---------------------------------------------------------------------


def split_sentences(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        for piece in _ENDER_RE.split(para):
            piece = piece.strip()
            if len(piece) >= 15:
                out.append(piece)
    return out


def number_sentences(text: str, max_chars: int = MAX_SENTENCE_CHARS) -> tuple[str, dict[str, str]]:
    """(numbered "[S1] ... [S2] ..." text, {sid: sentence text}), capped at `max_chars`."""
    kept: list[str] = []
    total = 0
    for sentence in split_sentences(text):
        kept.append(sentence)
        total += len(sentence)
        if total >= max_chars:
            break
    sid_map = {f"S{i}": s for i, s in enumerate(kept, start=1)}
    numbered = "\n".join(f"[S{i}] {s}" for i, s in enumerate(kept, start=1))
    return numbered, sid_map


# --- inbox scan ------------------------------------------------------------------------------


def file_id(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scan_inbox(inbox: Path, learned_ids: set[str]) -> tuple[list[PendingReport], list[str]]:
    """(pending reports sorted by date/broker/title, Korean skip warnings)."""
    inbox = Path(inbox)
    warnings: list[str] = []
    pending: list[PendingReport] = []
    if not inbox.exists():
        return pending, warnings
    for path in sorted(inbox.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _ALL_SUFFIXES:
            continue
        report_id = file_id(path)
        if report_id in learned_ids:
            continue
        text, warning = load_text(path)
        if warning:
            warnings.append(warning)
            continue
        meta, body = extract_metadata(text, path)
        pending.append(PendingReport(
            id=report_id, path=path, text=body, broker=meta["broker"], title=meta["title"],
            date=meta["date"], date_source=meta["date_source"], fictional=meta["fictional"],
        ))
    pending.sort(key=lambda r: (r.date, r.broker, r.title))
    return pending, warnings


def next_batch(pending: list[PendingReport], n: int) -> list[PendingReport]:
    return pending[:n]
