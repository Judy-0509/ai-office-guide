"""CLI: `python -m aioffice.tools.stack_reports <md file|folder|zip of .md> --library <path> [...]`.

Turns a batch of clean report Markdown (produced upstream by the company pipeline) into the
markdown report library that in-house OpenCode agents read with small context (see
aioffice.library.mdlibrary): one INDEX.md per ISO week, one markdown file per report,
catalog.jsonl, and a search.sqlite FTS5/vector index. No LLM calls; all logic lives in
mdlibrary.stack_batch().
"""
from __future__ import annotations

import argparse
import ntpath
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from ..config import read_env_file
from ..library import mdlibrary


class UnsafeZipError(Exception):
    """A zip member tried to escape the destination directory."""


def _safe_member_path(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    if ntpath.splitdrive(name)[0] or normalized.startswith("/"):
        raise UnsafeZipError(f"absolute path in zip member: {name!r}")
    rel = PurePosixPath(normalized)
    if any(part == ".." for part in rel.parts):
        raise UnsafeZipError(f"parent traversal in zip member: {name!r}")
    return rel


def safe_unzip(zip_path: Path, dest: Path) -> list[Path]:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    written: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        targets = [(m, dest.joinpath(*_safe_member_path(m.filename).parts)) for m in members]
        for _, target in targets:
            if root not in target.resolve().parents:
                raise UnsafeZipError(f"resolved outside destination: {target}")
        for member, target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as out:
                out.write(src.read())
            written.append(target)
    return written


def _collect_mds(target: Path, tmp_root: Path) -> list[Path]:
    if target.is_dir():
        return sorted(p for p in target.rglob("*.md"))
    if target.suffix.lower() == ".zip":
        extracted = safe_unzip(target, tmp_root / "unzipped")
        return sorted(p for p in extracted if p.suffix.lower() == ".md")
    return [target]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="주간 리포트 마크다운을 라이브러리로 정리합니다")
    parser.add_argument("target", help="마크다운 파일, zip, 또는 폴더 경로")
    parser.add_argument("--library", required=True, help="라이브러리 폴더 경로")
    parser.add_argument("--no-embed", action="store_true", help="벡터 인덱스(임베딩)를 건너뜁니다")
    parser.add_argument("--env", default=None, help=".env 파일 경로 (EMBED_*/RERANK_* 설정)")
    parser.add_argument("--force", action="store_true", help="이미 있는 리포트도 다시 스택합니다")
    args = parser.parse_args(argv)

    env = read_env_file(Path(args.env)) if args.env else {}

    with tempfile.TemporaryDirectory(prefix="stack_reports_") as tmp:
        mds = _collect_mds(Path(args.target), Path(tmp))
        if not mds:
            print("처리할 마크다운 파일이 없습니다.")
            return 1

        results = mdlibrary.stack_batch(
            mds, Path(args.library), env=env, embed=not args.no_embed, force=args.force,
        )

    counts = {"ok": 0, "중복": 0, "오류": 0}
    total_seconds = 0.0
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        name = Path(r.path).name
        if r.status == "ok":
            total_seconds += r.seconds or 0.0
            print(f"{name}: {r.seconds}초 -> {r.md_path}")
        elif r.status == "중복":
            print(f"{name}: 중복 (건너뜀)")
        else:
            print(f"{name}: 오류 - {r.error}")

    print(f"총 {len(results)}개 중 신규 {counts.get('ok', 0)}개, 중복 {counts.get('중복', 0)}개, "
          f"오류 {counts.get('오류', 0)}개, 소요시간 {round(total_seconds, 2)}초")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
