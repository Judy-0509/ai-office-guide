"""Tests for aioffice.dataplat.schedule: schtasks command construction and the .cmd wrapper,
all via an injected fake subprocess runner -- no real schtasks call."""
from __future__ import annotations

from types import SimpleNamespace

from aioffice.dataplat import schedule


def _fake_runner(calls, returncode=0, stdout="ok", stderr=""):
    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    return runner


def test_install_builds_weekly_schtasks_command(tmp_path):
    calls = []
    result = schedule.install(tmp_path / "source.yaml", tmp_path / "db" / "dataplat.sqlite",
                               day="TUE", time_="09:30", runner=_fake_runner(calls))
    argv = calls[0]
    assert argv[:2] == ["schtasks", "/Create"]
    assert "/D" in argv and argv[argv.index("/D") + 1] == "TUE"
    assert "/ST" in argv and argv[argv.index("/ST") + 1] == "09:30"
    assert "/TN" in argv and argv[argv.index("/TN") + 1] == schedule.TASK_NAME
    assert "/RU" not in argv  # deliberate: "run only when logged on" default


def test_install_writes_wrapper_without_pre_command(tmp_path):
    calls = []
    result = schedule.install(tmp_path / "source.yaml", tmp_path / "db" / "dataplat.sqlite",
                               runner=_fake_runner(calls))
    content = open(result["wrapper"], encoding="utf-8").read()
    assert "aioffice.dataplat.snapshot" in content
    assert "--source" in content and "--db" in content
    assert "call " not in content  # no pre-command block


def test_install_writes_wrapper_with_pre_command_gated_on_success(tmp_path):
    calls = []
    result = schedule.install(tmp_path / "source.yaml", tmp_path / "db" / "dataplat.sqlite",
                               pre_command="aggregate.exe --run", runner=_fake_runner(calls))
    content = open(result["wrapper"], encoding="utf-8").read()
    assert "call aggregate.exe --run" in content
    assert "if errorlevel 1" in content
    assert "exit /b 1" in content
    # the snapshot call must appear AFTER the errorlevel gate
    assert content.index("if errorlevel 1") < content.index("aioffice.dataplat.snapshot")


def test_install_log_path_next_to_db(tmp_path):
    calls = []
    db_path = tmp_path / "db" / "dataplat.sqlite"
    result = schedule.install(tmp_path / "source.yaml", db_path, runner=_fake_runner(calls))
    assert result["log"] == str(db_path.parent / schedule.LOG_NAME)
    assert result["wrapper"] == str(db_path.parent / schedule.WRAPPER_NAME)


def test_remove_builds_delete_command():
    calls = []
    schedule.remove(runner=_fake_runner(calls))
    assert calls[0] == ["schtasks", "/Delete", "/TN", schedule.TASK_NAME, "/F"]


def test_show_builds_query_command():
    calls = []
    schedule.show(runner=_fake_runner(calls))
    assert calls[0][:4] == ["schtasks", "/Query", "/TN", schedule.TASK_NAME]


def test_main_install_exits_nonzero_on_schtasks_failure(tmp_path, monkeypatch, capsys):
    def failing_install(*args, **kwargs):
        return {"argv": [], "wrapper": str(tmp_path / "w.cmd"), "log": str(tmp_path / "l.log"),
                "returncode": 1, "stdout": "", "stderr": "액세스가 거부되었습니다"}

    monkeypatch.setattr(schedule, "install", failing_install)
    import pytest
    with pytest.raises(SystemExit) as exc:
        schedule.main(["install", "--source", str(tmp_path / "s.yaml"),
                       "--db", str(tmp_path / "d.sqlite")])
    assert exc.value.code == 1


def test_main_show_prints_stdout(capsys, monkeypatch):
    monkeypatch.setattr(schedule, "show", lambda **kw: {"argv": [], "returncode": 0,
                                                          "stdout": "작업 있음", "stderr": ""})
    import pytest
    with pytest.raises(SystemExit) as exc:
        schedule.main(["show"])
    assert exc.value.code == 0
    assert "작업 있음" in capsys.readouterr().out
