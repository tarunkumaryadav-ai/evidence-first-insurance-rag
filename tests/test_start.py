import sys

import start


def test_launcher_reuses_existing_server_without_spawning(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["start.py"])
    monkeypatch.setattr(start, "server_state", lambda: "running")
    monkeypatch.setattr(start.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Must not spawn a duplicate")))
    assert start.main() == 0
    assert "already running" in capsys.readouterr().out


def test_launcher_does_not_start_over_unhealthy_or_other_server(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["start.py"])
    monkeypatch.setattr(start, "server_state", lambda: "occupied")
    monkeypatch.setattr(start.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Must not spawn on occupied port")))
    assert start.main() == 1
    assert "occupied" in capsys.readouterr().err


def test_launcher_starts_project_python_in_project_directory(monkeypatch, tmp_path):
    from types import SimpleNamespace
    relative_python = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    project_python = tmp_path / ".venv" / relative_python
    project_python.parent.mkdir(parents=True)
    project_python.touch()
    monkeypatch.setattr(sys, "argv", ["start.py", "--reload"])
    monkeypatch.setattr(start, "ROOT", tmp_path)
    monkeypatch.setattr(start, "server_state", lambda: "available")
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(start.subprocess, "run", run)
    assert start.main() == 0
    command, kwargs = calls[0]
    assert command[0] == str(project_python)
    assert command[1:4] == ["-m", "uvicorn", "app.api:app"]
    assert "--reload-dir" in command and kwargs["cwd"] == start.ROOT


def test_embedded_index_lock_has_actionable_error(settings):
    import pytest
    from conftest import FakeEmbeddings
    from app.store import Store, StoreInUseError
    first = Store(settings, embedding=FakeEmbeddings())
    try:
        with pytest.raises(StoreInUseError, match="python start.py"):
            Store(settings, embedding=FakeEmbeddings())
    finally:
        first.close()
