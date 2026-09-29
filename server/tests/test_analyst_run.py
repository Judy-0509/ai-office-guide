from __future__ import annotations

from aioffice.analyst import run, viewer


def _write_report(inbox, name="r1.md"):
    (inbox / name).write_text(
        "---\nbroker: A증권\ntitle: t\ndate: 2026-07-01\n---\n\n"
        "충분히 길게 작성한 본문 문장입니다 숫자 검사 통과용입니다.\n",
        encoding="utf-8",
    )


def _raise_keyboard_interrupt(_seconds):
    raise KeyboardInterrupt


# --- port fallback (find_open_port lives in viewer.py -- see test_analyst_viewer.py; this
# just confirms `run` re-exports the same function, since both need it) --------------------


def test_run_reexports_viewers_find_open_port():
    assert run.find_open_port is viewer.find_open_port


# --- msedge discovery / browser launch (mocked -- never launches a real browser) -----------


def test_find_msedge_prefers_a_candidate_path_that_exists(monkeypatch):
    monkeypatch.setattr(run.Path, "exists", lambda self: str(self) == run._EDGE_CANDIDATES[0])
    monkeypatch.setattr(run.shutil, "which", lambda name: None)
    assert run.find_msedge() == run._EDGE_CANDIDATES[0]


def test_find_msedge_falls_back_to_path_lookup(monkeypatch):
    monkeypatch.setattr(run.Path, "exists", lambda self: False)
    monkeypatch.setattr(run.shutil, "which", lambda name: "/usr/bin/msedge")
    assert run.find_msedge() == "/usr/bin/msedge"


def test_find_msedge_returns_none_when_not_found_anywhere(monkeypatch):
    monkeypatch.setattr(run.Path, "exists", lambda self: False)
    monkeypatch.setattr(run.shutil, "which", lambda name: None)
    assert run.find_msedge() is None


def test_open_browser_launches_edge_as_an_app_window(monkeypatch):
    calls = []
    monkeypatch.setattr(run, "find_msedge", lambda: r"C:\edge.exe")
    monkeypatch.setattr(run.subprocess, "Popen", lambda args: calls.append(("popen", args)))
    monkeypatch.setattr(run.webbrowser, "open", lambda url: calls.append(("webbrowser", url)))
    run.open_browser("http://127.0.0.1:8780/")
    assert calls == [("popen", [r"C:\edge.exe", "--app=http://127.0.0.1:8780/"])]


def test_open_browser_falls_back_to_webbrowser_when_no_edge(monkeypatch):
    calls = []
    monkeypatch.setattr(run, "find_msedge", lambda: None)
    monkeypatch.setattr(run.webbrowser, "open", lambda url: calls.append(url))
    run.open_browser("http://127.0.0.1:8780/")
    assert calls == ["http://127.0.0.1:8780/"]


def test_open_browser_falls_back_when_launching_edge_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(run, "find_msedge", lambda: r"C:\edge.exe")

    def boom(args):
        raise OSError("no such file")

    monkeypatch.setattr(run.subprocess, "Popen", boom)
    monkeypatch.setattr(run.webbrowser, "open", lambda url: calls.append(url))
    run.open_browser("http://x/")
    assert calls == ["http://x/"]


# --- main(): arg parsing + orchestration (server is real but idle -- no LLM call is ever
# triggered, since either the inbox is empty or start_background_learn is mocked out) --------


def test_run_main_skips_learn_when_nothing_pending(tmp_path, monkeypatch, capsys):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(run.time, "sleep", _raise_keyboard_interrupt)
    started = []
    monkeypatch.setattr(viewer, "start_background_learn", lambda server, batch: started.append(batch) or True)

    run.main(["--inbox", str(inbox), "--vault", str(vault), "--port", "0", "--no-browser"])

    assert started == []
    assert "학습할 리포트가 없습니다" in capsys.readouterr().out


def test_run_main_starts_background_learn_when_reports_are_pending(tmp_path, monkeypatch, capsys):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    _write_report(inbox)
    monkeypatch.setattr(run.time, "sleep", _raise_keyboard_interrupt)
    started = []
    monkeypatch.setattr(viewer, "start_background_learn", lambda server, batch: started.append(batch) or True)

    run.main(["--inbox", str(inbox), "--vault", str(vault), "--port", "0", "--no-browser", "--batch", "5"])

    assert started == [5]
    assert "백그라운드에서 시작" in capsys.readouterr().out


def test_run_main_no_learn_flag_skips_even_with_pending_reports(tmp_path, monkeypatch):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    _write_report(inbox)
    monkeypatch.setattr(run.time, "sleep", _raise_keyboard_interrupt)
    started = []
    monkeypatch.setattr(viewer, "start_background_learn", lambda server, batch: started.append(batch) or True)

    run.main(["--inbox", str(inbox), "--vault", str(vault), "--port", "0", "--no-browser", "--no-learn"])

    assert started == []


def test_run_main_opens_the_browser_by_default(tmp_path, monkeypatch):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(run.time, "sleep", _raise_keyboard_interrupt)
    opened = []
    monkeypatch.setattr(run, "open_browser", lambda url: opened.append(url))

    run.main(["--inbox", str(inbox), "--vault", str(vault), "--port", "0"])

    assert len(opened) == 1 and opened[0].startswith("http://127.0.0.1:")


def test_run_main_no_browser_flag_skips_opening_it(tmp_path, monkeypatch):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(run.time, "sleep", _raise_keyboard_interrupt)
    opened = []
    monkeypatch.setattr(run, "open_browser", lambda url: opened.append(url))

    run.main(["--inbox", str(inbox), "--vault", str(vault), "--port", "0", "--no-browser"])

    assert opened == []


def test_run_main_releases_the_lock_file_on_ctrl_c(tmp_path, monkeypatch):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(run.time, "sleep", _raise_keyboard_interrupt)

    run.main(["--inbox", str(inbox), "--vault", str(vault), "--port", "0", "--no-browser"])

    assert not (vault / ".analyst" / "learn.lock").exists()


def test_run_main_prints_the_actual_url_and_model(tmp_path, monkeypatch, capsys):
    vault, inbox = tmp_path / "vault", tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(run.time, "sleep", _raise_keyboard_interrupt)

    run.main(["--inbox", str(inbox), "--vault", str(vault), "--port", "0", "--no-browser"])

    out = capsys.readouterr().out
    assert "분석가 뷰어 실행 중" in out
    assert "http://127.0.0.1:" in out
