import shlex
from pathlib import Path

from mutator.cli import _prepare_coverage, parse_args
from mutator.crapper_link import ensure_crapper


def _project(tmp_path: Path, *names: str) -> tuple[Path, list[Path]]:
    root = tmp_path / "proj"
    files = []
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")
        files.append(path.resolve())
    return root.resolve(), files


def _command(tmp_path: Path) -> str:
    return f"echo run >> {shlex.quote(str(tmp_path / 'log'))}"


def _runs(tmp_path: Path) -> int:
    log = tmp_path / "log"
    return len(log.read_text().splitlines()) if log.exists() else 0


def _spy(monkeypatch) -> list[list[str]]:
    runners = ensure_crapper().runners
    real = runners.run_coverage
    calls = []

    def spy(root, files, command):
        calls.append(sorted(Path(file).name for file in files))
        return real(root, files, command)

    monkeypatch.setattr(runners, "run_coverage", spy)
    return calls


def _prepare(root: Path, command: str, files: list[Path]) -> None:
    _prepare_coverage(parse_args(["--root", str(root), "--coverage-command", command]), root, files)


def test_fresh_coverage_from_crapper_is_reused(tmp_path, capsys):
    root, files = _project(tmp_path, "src/a.py", "src/b.lua")
    command = _command(tmp_path)
    ensure_crapper().runners.run_coverage(root, files, command)
    capsys.readouterr()
    _prepare(root, command, files)
    assert _runs(tmp_path) == 1
    assert capsys.readouterr().err == "Reusing lua, python coverage from crapper's last run.\n"


def test_only_stale_languages_are_regenerated(tmp_path, capsys, monkeypatch):
    root, files = _project(tmp_path, "src/a.py", "src/b.lua")
    command = _command(tmp_path)
    ensure_crapper().runners.run_coverage(root, files[:1], command)
    capsys.readouterr()
    calls = _spy(monkeypatch)
    _prepare(root, command, files)
    assert calls == [["b.lua"]]
    assert _runs(tmp_path) == 2
    assert capsys.readouterr().err.startswith("Reusing python coverage from crapper's last run.\n+ ")


def test_a_stale_file_regenerates_its_whole_language(tmp_path, capsys, monkeypatch):
    root, files = _project(tmp_path, "src/a.py", "lib/c.py")
    command = _command(tmp_path)
    ensure_crapper().runners.run_coverage(root, files[:1], command)
    capsys.readouterr()
    calls = _spy(monkeypatch)
    _prepare(root, command, files)
    assert calls == [["a.py", "c.py"]]
    assert "Reusing" not in capsys.readouterr().err


def test_coverage_is_regenerated_without_a_matching_stamp(tmp_path, capsys, monkeypatch):
    root, files = _project(tmp_path, "src/a.py")
    command = _command(tmp_path)
    calls = _spy(monkeypatch)
    _prepare(root, command, files)
    _prepare(root, command + " ", files)
    (root / "src" / "a.py").write_text("x = 22\n", encoding="utf-8")
    _prepare(root, command + " ", files)
    assert calls == [["a.py"], ["a.py"], ["a.py"]]
    _prepare(root, command + " ", files)
    assert len(calls) == 3
    err = capsys.readouterr().err
    assert err.count("Reusing") == 1
    assert err.endswith("Reusing python coverage from crapper's last run.\n")


def test_reuse_is_skipped_when_coverage_is_not_generated(tmp_path, monkeypatch):
    root, files = _project(tmp_path, "src/a.py")
    asked = []
    runners = ensure_crapper().runners
    monkeypatch.setattr(runners, "stale_files", lambda *args: asked.append(args) or [])
    for flags in (["--no-coverage"], ["--use-existing-coverage"], ["--scan"]):
        _prepare_coverage(parse_args(["--root", str(root), *flags]), root, files)
    assert asked == []
    _prepare_coverage(parse_args(["--root", str(root)]), root, files)
    assert asked == [(root, files, None)]
