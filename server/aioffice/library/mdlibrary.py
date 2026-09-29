"""The weekly report markdown library ("stacking").

Turns clean report Markdown -- produced upstream by the company pipeline (zip -> unzip ->
docling -> in-house LLM disclaimer removal) -- into a folder tree an in-house OpenCode agent
can use with small context and no LLM calls: one INDEX.md per ISO week, one markdown file
per report (our front matter + the original body, unchanged), a catalog.jsonl machine
index, and a search.sqlite FTS5 + vector index for the `library_mcp` search tool. Nothing
here calls an LLM; the only network calls are to an embedding/reranker endpoint, and both
are optional.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import httpx
import numpy as np
import yaml

from .metadata import BROKERS_PATH, normalize_broker, regex_date

COMPANIES_PATH = Path(__file__).resolve().parents[2] / "companies.yaml"
CONVERTER_VERSION = "md-v1"
_RRF_K = 60
_TOP_N = 30
_MAX_PER_REPORT = 2
_TEXT_PREVIEW_CHARS = 600


# --- library layout ------------------------------------------------------------------------


def readme_path(library: Path) -> Path:
    return Path(library) / "README.md"


def catalog_path(library: Path) -> Path:
    return Path(library) / "catalog.jsonl"


def originals_dir(library: Path) -> Path:
    return Path(library) / "originals"


def search_db_path(library: Path) -> Path:
    return Path(library) / "search.sqlite"


def week_dir(library: Path, week: str) -> Path:
    return Path(library) / week


def reports_dir(library: Path, week: str) -> Path:
    return week_dir(library, week) / "reports"


def index_path(library: Path, week: str) -> Path:
    return week_dir(library, week) / "INDEX.md"


# --- catalog.jsonl ---------------------------------------------------------------------------


def read_catalog(library: Path) -> list[dict]:
    path = catalog_path(library)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def catalog_ids(library: Path) -> set[str]:
    return {rec["id"] for rec in read_catalog(library)}


def upsert_catalog(library: Path, record: dict) -> None:
    """Replaces the line with the same id (if any), else appends.

    ponytail: rewrites the whole file each call -- fine for a weekly batch of a few
    dozen reports; switch to append + periodic compaction if catalogs grow large.
    """
    records = [r for r in read_catalog(library) if r["id"] != record["id"]]
    records.append(record)
    catalog_path(library).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )


# --- ids / broker slug / companies ------------------------------------------------------------


def doc_ids(sha256_hex: str) -> tuple[str, str, str]:
    """(full id, id12 for originals/, id8 for the report filename)."""
    return sha256_hex, sha256_hex[:12], sha256_hex[:8]


@lru_cache(maxsize=4)
def _brokers_raw(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def broker_slug(canonical: str, path: str | Path = BROKERS_PATH) -> str:
    """First ASCII alias of `canonical` in brokers.yaml, lower-kebab-case; else "unknown"."""
    if not canonical:
        return "unknown"
    records = _brokers_raw(str(path))
    for name in [canonical, *(records.get(canonical) or [])]:
        name = str(name)
        if name and name.isascii():
            slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
            if slug:
                return slug
    return "unknown"


def _guess_broker(text: str, path: str | Path = BROKERS_PATH) -> str:
    """The longest brokers.yaml alias that appears in `text`, canonicalized."""
    records = _brokers_raw(str(path))
    aliases: set[str] = set()
    for canonical, names in records.items():
        aliases.add(str(canonical))
        aliases.update(str(n) for n in (names or []) if n)
    for alias in sorted(aliases, key=len, reverse=True):
        if alias in text:
            return normalize_broker(alias, path)
    return ""


def load_companies(path: str | Path = COMPANIES_PATH) -> dict[str, list[str]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {str(k): [str(a) for a in (v or [])] for k, v in data.items()}


@lru_cache(maxsize=256)
def _pattern(pattern: str) -> re.Pattern[str]:
    """ASCII patterns match on word boundaries, Korean ones as substrings.

    `Mac` must not match `Macau` and `LG` must not match inside `LGD`, `XLG`; Korean names
    take suffixes with no space (`애플코리아`), so a boundary there would lose real matches.
    The boundary is spelled out in ASCII rather than `\\b`, because `\\w` is unicode-aware
    and `애플Mac` would otherwise have no boundary before `Mac`.

    The trailing boundary allows a digit, because a model number runs straight onto the
    name in broker copy (`iPhone17`, `Xiaomi17`). The leading one does not, so `XLG` still
    fails to match `LG`.
    """
    body = re.escape(pattern)
    if pattern.isascii():
        body = rf"(?<![A-Za-z0-9_]){body}(?![A-Za-z_])"
    return re.compile(body, re.IGNORECASE)


def guess_companies(text: str, companies: dict[str, list[str]] | None = None) -> list[str]:
    """Canonical company names mentioned in `text`, ordered by hit count desc.

    ASCII aliases match on word boundaries, Korean aliases as plain substrings.
    """
    companies = load_companies() if companies is None else companies
    counts: list[tuple[str, int]] = []
    for canonical, aliases in companies.items():
        hits = sum(len(_pattern(alias).findall(text)) for alias in [canonical, *aliases] if alias)
        if hits:
            counts.append((canonical, hits))
    counts.sort(key=lambda pair: pair[1], reverse=True)
    return [name for name, _hits in counts]


_INDUSTRY_RE = re.compile(r"산업|업종|Industry|Sector|시장|Market", re.IGNORECASE)


def classify_type(title: str, companies_in_title: list[str]) -> str:
    if _INDUSTRY_RE.search(title or ""):
        return "industry"
    if len(companies_in_title) == 1:
        return "company"
    return "미상"


def iso_week(day: date) -> str:
    year, week, _weekday = day.isocalendar()
    return f"{year}-W{week:02d}"


def first_sentence(text: str, limit: int) -> str:
    """First sentence (roughly) of `text`, capped at `limit` chars.

    ponytail: a cheap regex split, not linguistic-grade sentence segmentation -- this is
    only used for a one-line preview in the catalog/INDEX, not for anything read as prose.
    """
    text = " ".join(text.split())
    if not text:
        return ""
    match = re.search(r"[.!?](?:\s|$)", text)
    sentence = text[: match.end()].strip() if match else text
    if len(sentence) > limit:
        sentence = sentence[: limit - 1].rstrip() + "…"
    return sentence


# --- front matter / title / summary -------------------------------------------------------------


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
_BULLET_RE = re.compile(r"^\s*[-*•]\s+\S")

_OUR_FRONT_MATTER_KEYS = {
    "id", "broker", "title", "pub_date", "date", "date_source", "week",
    "companies", "type", "source", "converter", "date_unknown",
}


def _split_front_matter(content: str) -> tuple[dict, str]:
    """Strips a leading YAML front-matter block, if present. (fields, body-without-it)."""
    match = _FRONT_MATTER_RE.match(content)
    if not match:
        return {}, content
    try:
        fm = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}, content
    if not isinstance(fm, dict):
        return {}, content
    return fm, content[match.end():]


def _first_heading_or_line(body: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        heading = _HEADING_RE.match(line)
        return heading.group(2).strip() if heading else line
    return ""


def _summary_text(body: str) -> str:
    """The first bullet list block, else the first paragraph after the title heading."""
    lines = body.splitlines()
    start = 1 if lines and _HEADING_RE.match(lines[0]) else 0
    paragraphs = [p for p in re.split(r"\n\s*\n", "\n".join(lines[start:])) if p.strip()]
    for para in paragraphs:
        if _BULLET_RE.search(para):
            return para.strip()
    return paragraphs[0].strip() if paragraphs else ""


# --- per-report metadata -----------------------------------------------------------------------


@dataclass
class ReportMeta:
    id: str
    id12: str
    id8: str
    broker: str
    title: str
    pub_date: str | None
    date_source: str
    date_unknown: bool
    week: str
    companies: list[str] = field(default_factory=list)
    type: str = "미상"
    source: str = ""
    extra: dict = field(default_factory=dict)


def build_meta(content: str, filename: str, sha256_hex: str,
               companies: dict[str, list[str]] | None = None) -> tuple[ReportMeta, str]:
    id_, id12, id8 = doc_ids(sha256_hex)
    fm, body = _split_front_matter(content)

    title = str(fm.get("title") or "").strip() or _first_heading_or_line(body) or filename
    broker_raw = fm.get("broker")
    broker = normalize_broker(str(broker_raw)) if broker_raw else _guess_broker(body[:1500])
    pub_date_raw = fm.get("pub_date") or fm.get("date")
    pub_date = str(pub_date_raw) if pub_date_raw else regex_date(body[:2000])
    date_source = "front_matter" if pub_date_raw else ("regex" if pub_date else "none")
    week = iso_week(date.fromisoformat(pub_date)) if pub_date else iso_week(date.today())

    fm_companies = fm.get("companies")
    companies_out = ([str(c) for c in fm_companies] if fm_companies
                      else guess_companies(f"{title}\n{body}", companies))
    title_companies = guess_companies(title, companies)

    extra = {k: v for k, v in fm.items() if k not in _OUR_FRONT_MATTER_KEYS}

    meta = ReportMeta(
        id=id_, id12=id12, id8=id8, broker=broker, title=title, pub_date=pub_date,
        date_source=date_source, date_unknown=pub_date is None, week=week,
        companies=companies_out, type=classify_type(title, title_companies),
        source=str(fm.get("source") or filename), extra=extra,
    )
    return meta, body


def report_filename(meta: ReportMeta) -> str:
    return f"{meta.pub_date or 'nodate'}_{broker_slug(meta.broker)}_{meta.id8}.md"


# --- markdown rendering ------------------------------------------------------------------------


def _front_matter(meta: ReportMeta) -> str:
    data: dict = {
        "id": meta.id, "broker": meta.broker, "title": meta.title, "pub_date": meta.pub_date,
        "date_source": meta.date_source, "week": meta.week, "companies": meta.companies,
        "type": meta.type, "source": meta.source, "converter": CONVERTER_VERSION,
    }
    if meta.date_unknown:
        data["date_unknown"] = True
    data.update(meta.extra)  # merges any other fields the original front matter carried
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def render_markdown(body: str, meta: ReportMeta) -> str:
    """Our front matter + the original body, unchanged."""
    return "---\n" + _front_matter(meta).rstrip("\n") + "\n---\n\n" + body.strip() + "\n"


# --- catalog record / INDEX.md ------------------------------------------------------------------


def catalog_record(meta: ReportMeta, body: str, *, md_rel: str, original_rel: str) -> dict:
    return {
        "id": meta.id, "id12": meta.id12, "id8": meta.id8, "broker": meta.broker,
        "title": meta.title, "pub_date": meta.pub_date, "date_source": meta.date_source,
        "date_unknown": meta.date_unknown, "week": meta.week, "companies": meta.companies,
        "type": meta.type, "source": meta.source, "converter": CONVERTER_VERSION,
        "md_path": md_rel, "original_path": original_rel,
        "summary": first_sentence(_summary_text(body), 120),
    }


def _esc_pipe(text: str) -> str:
    return str(text).replace("|", "\\|")


INDEX_NOTE = (
    "이 표는 해당 주차 리포트의 한 줄 색인입니다. 필요한 리포트를 찾으면 파일을 열어 `##` 섹션 "
    "제목으로 원하는 부분만 읽고, 여러 리포트를 넘나드는 질문은 search_reports 도구를 쓰세요."
)


def rebuild_index(library: Path, week: str) -> None:
    records = [r for r in read_catalog(library) if r.get("week") == week]
    records.sort(key=lambda r: r.get("broker") or "")
    records.sort(key=lambda r: r.get("pub_date") or "", reverse=True)

    brokers = {r["broker"] for r in records if r.get("broker")}
    lines = [
        f"# {week} 주간 리포트 색인", "",
        f"- 주차: {week}",
        f"- 문서 수: {len(records)}",
        f"- 증권사 수: {len(brokers)}",
        f"- 갱신 시각: {datetime.now().isoformat(timespec='seconds')}",
        "", INDEX_NOTE, "",
        "| 날짜 | 증권사 | 제목 | 기업 | 유형 | 요약 | 파일 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in records:
        filename = Path(r["md_path"]).name
        lines.append(
            "| {date} | {broker} | {title} | {companies} | {type} | {summary} | [{file}]({link}) |".format(
                date=_esc_pipe(r.get("pub_date") or "미상"),
                broker=_esc_pipe(r.get("broker") or "미상"),
                title=_esc_pipe(r.get("title") or ""),
                companies=_esc_pipe(", ".join(r.get("companies") or [])),
                type=_esc_pipe(r.get("type") or ""),
                summary=_esc_pipe(first_sentence(r.get("summary") or "", 80)),
                file=_esc_pipe(filename), link=f"reports/{filename}",
            )
        )
    week_dir(library, week).mkdir(parents=True, exist_ok=True)
    index_path(library, week).write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- library README.md (written once) -------------------------------------------------------


LIBRARY_README = """# 리포트 라이브러리 사용법 (AI 에이전트용)

이 폴더는 증권사 주간 리포트를 에이전트가 작은 컨텍스트로도 바로 읽을 수 있도록 마크다운으로
정리한 서고입니다. 리포트 전체를 프롬프트에 붙여넣지 말고, 아래 순서로 필요한 부분만 읽으세요.

1. 이번 주 리포트를 찾으려면 `<주차>/INDEX.md`를 먼저 읽습니다. 리포트 한 개당 한 줄로
   날짜·증권사·제목·기업·유형·요약·파일 경로가 있습니다.
2. 필요한 리포트 하나만 `<주차>/reports/*.md`에서 엽니다. 전체를 읽지 말고 `##` 섹션 제목을
   먼저 훑어 필요한 절만 읽으세요.
3. 인용할 때는 `[파일명 p.N]` 형식으로 파일명과 페이지 번호를 함께 남기세요. 페이지는 본문 중
   `<!-- p.N -->` 표시로 확인합니다 (원문에 표시가 없으면 페이지 없이 인용하세요).
4. 여러 리포트를 넘나드는 질문(예: "이번 주 특정 기업 언급 리포트 다 찾아줘")에는
   `search_reports` MCP 도구를 쓰세요. `list_reports`로 주차/증권사/기업별 목록을 볼 수 있습니다.

## MCP 도구 등록 (OpenCode)

`opencode.json`에 다음을 추가하세요 (키 이름은 설치된 OpenCode 버전에 맞게 확인하세요):

```json
{
  "mcp": {
    "ai-office-library": {
      "type": "local",
      "command": ["python", "-m", "aioffice.tools.library_mcp", "--library", "<library 경로>"],
      "enabled": true
    }
  }
}
```
"""


def write_readme_if_missing(library: Path) -> None:
    path = readme_path(library)
    if not path.exists():
        path.write_text(LIBRARY_README, encoding="utf-8")


# --- env helpers (mirrors config.read_env_file: env file, then process env override) ---------


def _env_get(env: dict[str, str], key: str, default: str = "") -> str:
    return os.environ.get(key) or (env or {}).get(key) or default


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _post_with_retries(client: httpx.Client, url: str, payload: dict, headers: dict,
                        retries: int = 3) -> dict:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 -- retry on anything, surface the last one
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"요청 실패 ({retries}회 재시도): {last_exc}")


def embed_texts(texts: list[str], env: dict[str, str]) -> list[list[float]]:
    """L2-normalized embeddings in input order. Raises if EMBED_BASE_URL fails."""
    base = _env_get(env, "EMBED_BASE_URL")
    if not base or not texts:
        return []
    model = _env_get(env, "EMBED_MODEL", "BAAI-bge-m3")
    api_key = _env_get(env, "EMBED_API_KEY") or _env_get(env, "LLM_API_KEY")
    batch_size = int(_env_get(env, "EMBED_BATCH_SIZE", "32") or "32")
    out: list[list[float] | None] = [None] * len(texts)
    with httpx.Client(timeout=120.0) as client:
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            data = _post_with_retries(
                client, f"{base.rstrip('/')}/embeddings",
                {"model": model, "input": batch}, _auth_headers(api_key),
            )
            for record in data["data"]:
                index = int(record["index"])
                vector = np.asarray(record["embedding"], dtype=np.float32)
                norm = float(np.linalg.norm(vector)) or 1.0
                out[start + index] = (vector / norm).tolist()
    if any(v is None for v in out):
        raise RuntimeError("임베딩 응답에 일부 항목이 빠졌습니다")
    return out  # type: ignore[return-value]


# --- search chunking (heading split + ~350-token paragraph merge) -----------------------------


# Matches <!-- p.N -->, <!-- page N --> (also covers docling's page-break comments),
# [p.N], and "Page N".
_PAGE_MARKER_RE = re.compile(
    r"<!--\s*p(?:age)?\.?\s*(\d+)\s*-->|\[p\.?\s*(\d+)\]|(?<![A-Za-z])Page\s+(\d+)\b",
    re.IGNORECASE,
)
_EST_TOKENS_PER_CHUNK = 350


@dataclass
class SearchChunk:
    text: str
    page: int | None
    section: str | None


def _chunk_body(body: str) -> list[SearchChunk]:
    """Splits `body` by heading, then merges paragraphs to ~350 estimated tokens.

    page = the most recent recognizable page marker seen so far in the document, else null.
    section = the nearest preceding heading, else null.
    ponytail: a chars/4 token estimate, not a real tokenizer -- good enough to size chunks.
    """
    section: str | None = None
    page: int | None = None
    paragraphs: list[tuple[str | None, int | None, str]] = []
    para_lines: list[str] = []
    para_page: int | None = None

    def flush_para() -> None:
        nonlocal para_lines, para_page
        text = "\n".join(para_lines).strip()
        if text:
            paragraphs.append((section, para_page if para_page is not None else page, text))
        para_lines, para_page = [], None

    for line in body.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            flush_para()
            section = heading.group(2).strip()
            continue
        marker = _PAGE_MARKER_RE.search(line)
        if marker:
            page = int(next(g for g in marker.groups() if g))
            line = _PAGE_MARKER_RE.sub("", line).strip()
            if not line:
                continue  # a marker-only line carries no content of its own
        if not line.strip():
            flush_para()
            continue
        if para_page is None:
            para_page = page
        para_lines.append(line)
    flush_para()

    chunks: list[SearchChunk] = []
    buf: list[str] = []
    buf_chars = 0
    buf_section: str | None = None
    buf_page: int | None = None
    for para_section, para_page_val, text in paragraphs:
        if buf and (para_section != buf_section or buf_chars >= _EST_TOKENS_PER_CHUNK * 4):
            chunks.append(SearchChunk(text="\n\n".join(buf), page=buf_page, section=buf_section))
            buf, buf_chars = [], 0
        if not buf:
            buf_section, buf_page = para_section, para_page_val
        buf.append(text)
        buf_chars += len(text)
    if buf:
        chunks.append(SearchChunk(text="\n\n".join(buf), page=buf_page, section=buf_section))
    return chunks


# --- search.sqlite: schema + indexing -----------------------------------------------------------


_fts_warned = False


def ensure_search_schema(conn: sqlite3.Connection) -> str:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chunks ("
        " id INTEGER PRIMARY KEY, doc_id TEXT, week TEXT, file TEXT, broker TEXT,"
        " title TEXT, pub_date TEXT, page INTEGER, section TEXT, text TEXT)"
    )
    tokenizer = "trigram"
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, tokenize='trigram')")
    except sqlite3.OperationalError:
        tokenizer = "unicode61"
        global _fts_warned
        if not _fts_warned:
            print("경고: sqlite에 trigram 토크나이저가 없어 unicode61로 대체합니다 "
                  "(한글 부분 문자열 검색 품질이 떨어질 수 있습니다)")
            _fts_warned = True
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, tokenize='unicode61')")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vectors (chunk_id INTEGER, model TEXT, dim INTEGER, vector BLOB)"
    )
    conn.commit()
    return tokenizer


def open_search_db(library: Path) -> sqlite3.Connection:
    Path(library).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(search_db_path(library)))
    conn.row_factory = sqlite3.Row
    ensure_search_schema(conn)
    return conn


_WS_RE = re.compile(r"\s+")


def _tokenizer_of(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='chunks_fts'").fetchone()
    return "trigram" if row and "trigram" in row[0] else "unicode61"


def _fts_text(text: str, tokenizer: str) -> str:
    """Strips whitespace before indexing/querying under trigram.

    Korean text has no reliable spaces between words and particles, so a query typed with
    natural spacing must still match source text without it, and vice versa -- trigram
    windows are exact character sequences, so both sides need the same normalization.
    unicode61 tokenizes on whitespace already, so it's left alone there.
    """
    return _WS_RE.sub("", text) if tokenizer == "trigram" else text


def index_document(conn: sqlite3.Connection, meta: ReportMeta, body: str, md_rel: str,
                    env: dict[str, str], *, embed: bool = True) -> None:
    """Replaces this doc's rows (chunks/FTS/vectors), then re-embeds if enabled."""
    old_ids = [row["id"] for row in conn.execute("SELECT id FROM chunks WHERE doc_id=?", (meta.id,))]
    if old_ids:
        marks = ",".join("?" * len(old_ids))
        conn.execute(f"DELETE FROM chunks WHERE id IN ({marks})", old_ids)
        conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({marks})", old_ids)
        conn.execute(f"DELETE FROM vectors WHERE chunk_id IN ({marks})", old_ids)

    rows = _chunk_body(body)
    tokenizer = _tokenizer_of(conn)
    chunk_ids: list[int] = []
    for c in rows:
        cur = conn.execute(
            "INSERT INTO chunks (doc_id, week, file, broker, title, pub_date, page, section, text)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (meta.id, meta.week, md_rel, meta.broker, meta.title, meta.pub_date,
             c.page, c.section, c.text),
        )
        chunk_ids.append(cur.lastrowid)
        fts_text = _fts_text(c.text, tokenizer)
        conn.execute("INSERT INTO chunks_fts (rowid, text) VALUES (?, ?)", (cur.lastrowid, fts_text))
    conn.commit()

    if not embed or not _env_get(env, "EMBED_BASE_URL") or not rows:
        return
    texts = [f"[{meta.broker} | {meta.title} | {c.section or '본문'}] {c.text}" for c in rows]
    try:
        vectors = embed_texts(texts, env)
    except Exception as exc:  # embedding is best-effort -- the md/FTS index must survive this
        print(f"경고: 임베딩 실패로 벡터 인덱스를 건너뜁니다 ({exc})")
        return
    model = _env_get(env, "EMBED_MODEL", "BAAI-bge-m3")
    for chunk_id, vector in zip(chunk_ids, vectors):
        arr = np.asarray(vector, dtype=np.float32)
        conn.execute("INSERT INTO vectors (chunk_id, model, dim, vector) VALUES (?,?,?,?)",
                     (chunk_id, model, len(arr), arr.tobytes()))
    conn.commit()


# --- search ----------------------------------------------------------------------------------


def _fts_search(conn: sqlite3.Connection, query: str, week: str | None, broker: str | None,
                 limit: int) -> list[sqlite3.Row]:
    sql = ("SELECT c.*, bm25(chunks_fts) AS score FROM chunks_fts "
           "JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ?")
    params: list = [_fts_text(query, _tokenizer_of(conn))]
    if week:
        sql += " AND c.week=?"
        params.append(week)
    if broker:
        sql += " AND c.broker=?"
        params.append(broker)
    sql += " ORDER BY score LIMIT ?"
    params.append(limit)
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []  # malformed FTS5 query syntax -- degrade to vector-only results


def _vector_search(conn: sqlite3.Connection, query: str, week: str | None, broker: str | None,
                    limit: int, env: dict[str, str]) -> list[sqlite3.Row]:
    if not _env_get(env, "EMBED_BASE_URL"):
        return []
    try:
        query_vector = np.asarray(embed_texts([query], env)[0], dtype=np.float32)
    except Exception:
        return []  # embedding endpoint unavailable -- FTS results alone still work

    model = _env_get(env, "EMBED_MODEL", "BAAI-bge-m3")
    sql = "SELECT c.*, v.vector AS vector, v.dim AS dim FROM vectors v JOIN chunks c ON c.id=v.chunk_id WHERE v.model=?"
    params: list = [model]
    if week:
        sql += " AND c.week=?"
        params.append(week)
    if broker:
        sql += " AND c.broker=?"
        params.append(broker)
    scored: list[tuple[float, sqlite3.Row]] = []
    for row in conn.execute(sql, params).fetchall():
        vector = np.frombuffer(row["vector"], dtype=np.float32)
        if len(vector) != row["dim"]:
            continue
        scored.append((float(np.dot(query_vector, vector)), row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _score, row in scored[:limit]]


def _rrf_merge(fts_rows: list[sqlite3.Row], vec_rows: list[sqlite3.Row]) -> list[tuple[sqlite3.Row, float]]:
    scores: dict[int, float] = {}
    rows_by_id: dict[int, sqlite3.Row] = {}
    for rank, row in enumerate(fts_rows, start=1):
        scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (_RRF_K + rank)
        rows_by_id[row["id"]] = row
    for rank, row in enumerate(vec_rows, start=1):
        scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (_RRF_K + rank)
        rows_by_id.setdefault(row["id"], row)
    ordered = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    return [(rows_by_id[chunk_id], score) for chunk_id, score in ordered]


def _rerank(query: str, merged: list[tuple[sqlite3.Row, float]],
            env: dict[str, str]) -> list[tuple[sqlite3.Row, float]]:
    base = _env_get(env, "RERANK_BASE_URL")
    if not base or not merged:
        return merged
    model = _env_get(env, "RERANK_MODEL", "BAAI-bge-reranker-v2-m3")
    api_key = _env_get(env, "RERANK_API_KEY") or _env_get(env, "LLM_API_KEY")
    documents = [row["text"] for row, _score in merged]
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                f"{base.rstrip('/')}/rerank",
                json={"model": model, "query": query, "documents": documents},
                headers=_auth_headers(api_key),
            )
            response.raise_for_status()
            results = response.json()["results"]
        return [(merged[r["index"]][0], float(r["relevance_score"])) for r in results]
    except Exception:
        return merged  # reranker failure -- keep RRF order


def _row_to_result(row: sqlite3.Row, score: float) -> dict:
    return {
        "file": row["file"], "week": row["week"], "broker": row["broker"], "title": row["title"],
        "pub_date": row["pub_date"], "page": row["page"], "section": row["section"],
        "text": row["text"][:_TEXT_PREVIEW_CHARS], "score": round(float(score), 4),
    }


def search_chunks(library: Path, query: str, *, week: str | None = None, broker: str | None = None,
                   k: int = 8, env: dict[str, str] | None = None) -> list[dict]:
    env = env or {}
    conn = open_search_db(library)
    try:
        fts_rows = _fts_search(conn, query, week, broker, _TOP_N)
        vec_rows = _vector_search(conn, query, week, broker, _TOP_N, env)
        merged = _rerank(query, _rrf_merge(fts_rows, vec_rows), env)
    finally:
        conn.close()

    out: list[dict] = []
    per_report: dict[str, int] = {}
    for row, score in merged:
        doc_id = row["doc_id"]
        if per_report.get(doc_id, 0) >= _MAX_PER_REPORT:
            continue
        out.append(_row_to_result(row, score))
        per_report[doc_id] = per_report.get(doc_id, 0) + 1
        if len(out) >= k:
            break
    return out


def list_reports(library: Path, *, week: str | None = None, broker: str | None = None,
                  company: str | None = None) -> list[dict]:
    out = []
    for r in read_catalog(library):
        if week and r.get("week") != week:
            continue
        if broker and r.get("broker") != broker:
            continue
        if company and company not in (r.get("companies") or []):
            continue
        out.append({
            "date": r.get("pub_date"), "broker": r.get("broker"), "title": r.get("title"),
            "companies": r.get("companies"), "type": r.get("type"), "file": r.get("md_path"),
            "summary": r.get("summary"),
        })
    return out


# --- stacking a batch --------------------------------------------------------------------------


@dataclass
class StackResult:
    path: str
    status: str  # "ok" | "중복" | "오류"
    id: str | None = None
    md_path: str | None = None
    seconds: float | None = None
    error: str | None = None


def stack_batch(md_paths: list[Path], library: Path, *,
                 env: dict[str, str] | None = None, embed: bool = True,
                 force: bool = False) -> list[StackResult]:
    """One bad file never stops the batch; returns one StackResult per input path."""
    env = env or {}
    library = Path(library)
    library.mkdir(parents=True, exist_ok=True)
    existing_ids = catalog_ids(library)
    companies = load_companies()

    touched_weeks: set[str] = set()
    results: list[StackResult] = []
    conn = open_search_db(library)
    try:
        for path in md_paths:
            started = time.perf_counter()
            try:
                # sha is over the raw file bytes (unchanged behavior) -- decode with
                # utf-8-sig separately so a leading BOM from the upstream pipeline
                # doesn't break front-matter (`---`) / title (`#`) detection below.
                raw = Path(path).read_bytes()
                sha = hashlib.sha256(raw).hexdigest()
                content = raw.decode("utf-8-sig")
            except Exception as exc:
                results.append(StackResult(path=str(path), status="오류", error=str(exc)))
                continue
            if sha in existing_ids and not force:
                results.append(StackResult(path=str(path), status="중복", id=sha))
                continue

            try:
                meta, body = build_meta(content, Path(path).name, sha, companies)
                filename = report_filename(meta)

                reports_dir(library, meta.week).mkdir(parents=True, exist_ok=True)
                (reports_dir(library, meta.week) / filename).write_text(
                    render_markdown(body, meta), encoding="utf-8"
                )

                originals_dir(library).mkdir(parents=True, exist_ok=True)
                (originals_dir(library) / f"{meta.id12}.md").write_text(content, encoding="utf-8")

                md_rel = f"{meta.week}/reports/{filename}"
                original_rel = f"originals/{meta.id12}.md"
                upsert_catalog(library, catalog_record(meta, body, md_rel=md_rel, original_rel=original_rel))
                index_document(conn, meta, body, md_rel, env, embed=embed)
                touched_weeks.add(meta.week)
                existing_ids.add(sha)

                results.append(StackResult(
                    path=str(path), status="ok", id=meta.id, md_path=md_rel,
                    seconds=round(time.perf_counter() - started, 2),
                ))
            except Exception as exc:
                results.append(StackResult(path=str(path), status="오류", error=str(exc)))
    finally:
        conn.close()

    write_readme_if_missing(library)
    for week in sorted(touched_weeks):
        rebuild_index(library, week)

    return results
