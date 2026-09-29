"""`python -m aioffice.dataplat.schedule install|remove|show [--day MON] [--time 08:00]
[--pre "<their existing aggregation command>"] --source source.yaml --db dataplat.sqlite`

Wraps Windows Task Scheduler (`schtasks`). `install` writes a small `.cmd` wrapper next to
`dataplat.sqlite` that runs `--pre` (if given) and only runs the snapshot if it succeeded,
logging both to a file beside the wrapper, then registers that wrapper as a weekly task.

The task is created WITHOUT `/RU` (run-as-user), which is schtasks' default "run only when the
user is logged on" mode -- deliberate, since the team's own aggregation step may itself need an
interactive session (Excel, a mapped drive, DRM tooling, ...). A task that must run unattended
needs `/RU`/`/RP` added by hand in Task Scheduler; this CLI doesn't offer it.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

TASK_NAME = "AIOffice-Dataplat-Snapshot"
WRAPPER_NAME = "dataplat_run.cmd"
LOG_NAME = "dataplat_run.log"
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def build_wrapper_script(python_exe: str, source_path: Path, db_path: Path,
                          pre_command: str | None, log_path: Path,
                          env_path: Path | None = None) -> str:
    """Snapshot, then -- only if it succeeded -- a digest run (dataplat.digest). A digest
    failure is logged but never changes the wrapper's own exit code (%SNAPSHOT_ERRORLEVEL%,
    captured right after the snapshot step): the snapshot is the part a missed run actually
    matters for."""
    env_arg = f' --env "{env_path}"' if env_path else ""
    lines = ["@echo off", "setlocal"]
    if pre_command:
        lines += [
            f'call {pre_command} >> "{log_path}" 2>&1',
            "if errorlevel 1 (",
            f'  echo [%date% %time%] pre-command failed, skipping snapshot >> "{log_path}"',
            "  exit /b 1",
            ")",
        ]
    lines += [
        f'"{python_exe}" -m aioffice.dataplat.snapshot --source "{source_path}" '
        f'--db "{db_path}" >> "{log_path}" 2>&1',
        "set SNAPSHOT_ERRORLEVEL=%ERRORLEVEL%",
        "if %SNAPSHOT_ERRORLEVEL% EQU 0 (",
        f'  "{python_exe}" -m aioffice.dataplat.digest --source "{source_path}" '
        f'--db "{db_path}"{env_arg} >> "{log_path}" 2>&1',
        ")",
        "exit /b %SNAPSHOT_ERRORLEVEL%",
    ]
    return "\r\n".join(lines) + "\r\n"


def install_argv(wrapper_path: Path, day: str, time_: str) -> list[str]:
    return ["schtasks", "/Create", "/SC", "WEEKLY", "/D", day, "/ST", time_,
            "/TN", TASK_NAME, "/TR", str(wrapper_path), "/F"]


def remove_argv() -> list[str]:
    return ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]


def show_argv() -> list[str]:
    return ["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"]


def install(source: Path, db: Path, *, day: str = "MON", time_: str = "08:00",
            pre_command: str | None = None, python_exe: str = sys.executable,
            runner: Callable[..., Any] = subprocess.run, env_path: Path | None = None) -> dict:
    if day not in DAYS:
        raise ValueError(f"--day는 {DAYS} 중 하나여야 합니다: {day!r}")
    db = Path(db).resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    wrapper_path = db.parent / WRAPPER_NAME
    log_path = db.parent / LOG_NAME
    wrapper_path.write_text(
        build_wrapper_script(python_exe, Path(source).resolve(), db, pre_command, log_path,
                              env_path=Path(env_path).resolve() if env_path else None),
        encoding="utf-8",
    )
    result = runner(install_argv(wrapper_path, day, time_), capture_output=True, text=True)
    return {"argv": install_argv(wrapper_path, day, time_), "wrapper": str(wrapper_path),
            "log": str(log_path), "returncode": getattr(result, "returncode", 0),
            "stdout": getattr(result, "stdout", ""), "stderr": getattr(result, "stderr", "")}


def remove(*, runner: Callable[..., Any] = subprocess.run) -> dict:
    result = runner(remove_argv(), capture_output=True, text=True)
    return {"argv": remove_argv(), "returncode": getattr(result, "returncode", 0),
            "stdout": getattr(result, "stdout", ""), "stderr": getattr(result, "stderr", "")}


def show(*, runner: Callable[..., Any] = subprocess.run) -> dict:
    result = runner(show_argv(), capture_output=True, text=True)
    return {"argv": show_argv(), "returncode": getattr(result, "returncode", 0),
            "stdout": getattr(result, "stdout", ""), "stderr": getattr(result, "stderr", "")}


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="AI Office 데이터 플랫폼 주간 스냅샷 예약 (Windows 작업 스케줄러). "
                     "로그온한 사용자 세션에서만 실행됩니다 (팀의 집계 단계가 엑셀/매핑 드라이브를 "
                     "쓸 수 있어서 무인 실행으로 등록하지 않습니다)."
    )
    parser.add_argument("action", choices=["install", "remove", "show"])
    parser.add_argument("--day", default="MON", choices=DAYS)
    parser.add_argument("--time", default="08:00", dest="time_")
    parser.add_argument("--pre", default=None, dest="pre_command", help="스냅샷 전에 실행할 기존 집계 명령")
    parser.add_argument("--source", default=None, help="source.yaml 경로 (install 시 필수)")
    parser.add_argument("--db", default=None, help="dataplat.sqlite 경로 (install 시 필수)")
    parser.add_argument("--env", default=None,
                         help=".env 경로 (다이제스트의 LLM 설정용 -- 스냅샷 성공 후에만 실행됨)")
    args = parser.parse_args(argv)

    if args.action == "install":
        if not args.source or not args.db:
            parser.error("install에는 --source와 --db가 필요합니다")
        result = install(Path(args.source), Path(args.db), day=args.day, time_=args.time_,
                          pre_command=args.pre_command,
                          env_path=Path(args.env) if args.env else None)
        ok = result["returncode"] == 0
        print(f"래퍼 스크립트: {result['wrapper']}", flush=True)
        print(f"로그 파일: {result['log']}", flush=True)
        print(("[OK] " if ok else "[오류] ") + f"작업 등록: {TASK_NAME}", flush=True)
        if not ok:
            print(result["stderr"] or result["stdout"], flush=True)
        sys.exit(0 if ok else 1)
    elif args.action == "remove":
        result = remove()
        ok = result["returncode"] == 0
        print(("[OK] " if ok else "[오류] ") + f"작업 삭제: {TASK_NAME}", flush=True)
        sys.exit(0 if ok else 1)
    else:
        result = show()
        print(result["stdout"] or result["stderr"], flush=True)
        sys.exit(result["returncode"])


if __name__ == "__main__":
    main()
