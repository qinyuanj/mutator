import subprocess
from pathlib import Path

import pytest

from mutator.cli import HELP, parse_args, run, select_files

SOURCE = "def place(x):\n    return x > 0\n"


def _write(path: Path, text: str = SOURCE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _names(files: list[Path], root: Path) -> list[str]:
    return [path.relative_to(root.resolve()).as_posix() for path in files]


def _select(root: Path, *argv: str) -> list[str]:
    return _names(select_files(parse_args(["--root", str(root), *argv])), root)


def test_exclude_is_repeatable():
    assert parse_args(["--exclude", "a", "--exclude", "b/**"]).excludes == ["a", "b/**"]
    assert parse_args([]).excludes == []


@pytest.mark.parametrize("pattern", ["/abs", "../up", "src/../x", "\\abs"])
def test_an_exclusion_outside_the_root_is_a_usage_error(pattern):
    options = parse_args(["--exclude", pattern])
    assert options.exit_code == 1
    message = f"--exclude must be a path or glob inside the project root: {pattern}"
    assert options.message == f"{message}\n\n{HELP}"


def test_an_exclusion_waits_for_crapper_when_it_is_missing(monkeypatch, capsys):
    def missing():
        raise ImportError("crapper not found")

    monkeypatch.setattr("mutator.cli.ensure_crapper", missing)
    assert parse_args(["--exclude", "/abs"]).exit_code == 0
    assert run(["--exclude", "/abs", "--scan"]) == 2
    assert capsys.readouterr().err == "crapper not found\n"


def test_exclusions_filter_the_default_walk(tmp_path):
    _write(tmp_path / "src" / "app.py")
    _write(tmp_path / "src" / "gen" / "made.py")
    _write(tmp_path / "lib" / "deep" / "gen" / "other.py")
    assert _select(tmp_path, "--exclude", "src/gen") == ["lib/deep/gen/other.py", "src/app.py"]
    assert _select(tmp_path, "--exclude", "**/gen/") == ["src/app.py"]
    assert _select(tmp_path, "--exclude", "./src/*.py", "--exclude", "lib") == ["src/gen/made.py"]


def test_exclusions_filter_source_roots_named_directories_and_filters(tmp_path):
    _write(tmp_path / "src" / "app.py")
    _write(tmp_path / "src" / "gen" / "made.py")
    assert _select(tmp_path, "-s", "src", "--exclude", "src/gen") == ["src/app.py"]
    assert _select(tmp_path, "src", "--exclude", "src/gen") == ["src/app.py"]
    assert _select(tmp_path, "made", "--exclude", "src/gen") == []


def test_a_named_excluded_file_is_skipped_with_a_message(tmp_path, capsys):
    _write(tmp_path / "src" / "app.py")
    made = _write(tmp_path / "src" / "gen" / "made.py")
    chosen = _select(tmp_path, str(made), "src/app.py", "--exclude", "src/gen")
    assert chosen == ["src/app.py"]
    assert capsys.readouterr().err == "Skipping excluded src/gen/made.py\n"


def test_scan_honors_exclusions(tmp_path, capsys):
    _write(tmp_path / "src" / "app.py")
    _write(tmp_path / "src" / "gen" / "made.py")
    code = run(["--scan", "--no-coverage", "--root", str(tmp_path), "--exclude", "src/gen"])
    out = capsys.readouterr().out
    assert code == 0
    assert "app.py:2" in out
    assert "made.py" not in out


def test_excluding_everything_leaves_nothing_to_mutate(tmp_path, capsys):
    _write(tmp_path / "src" / "app.py")
    assert run(["--no-coverage", "--root", str(tmp_path), "--exclude", "**"]) == 0
    assert capsys.readouterr().out == "No source files to mutate.\n"


def test_changed_expands_new_untracked_directories(tmp_path):
    _git(tmp_path, "init")
    _write(tmp_path / "pkg" / "fresh" / "core.py")
    _write(tmp_path / "pkg" / "fresh" / "test_core.py")
    _write(tmp_path / "pkg" / "fresh" / "notes.txt", "x")
    assert _select(tmp_path, "--changed") == ["pkg/fresh/core.py"]


def test_changed_works_from_a_subdirectory_root(tmp_path):
    _write(tmp_path / "top.py")
    edited = _write(tmp_path / "sub" / "src" / "app.py")
    gone = _write(tmp_path / "sub" / "src" / "gone.py")
    _git(tmp_path, "init")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init")
    edited.write_text(SOURCE + "\n", encoding="utf-8")
    gone.unlink()
    _write(tmp_path / "top.py", SOURCE + "\n")
    _write(tmp_path / "sub" / "src" / "odd name é.py")
    assert _select(tmp_path / "sub", "--changed") == ["src/app.py", "src/odd name é.py"]


def test_changed_honors_exclusions(tmp_path):
    _git(tmp_path, "init")
    _write(tmp_path / "src" / "app.py")
    _write(tmp_path / "src" / "gen" / "made.py")
    assert _select(tmp_path, "--changed", "--exclude", "src/gen") == ["src/app.py"]


def test_help_documents_exclusions_and_coverage_reuse():
    assert "--exclude <glob>" in HELP
    assert "Skipping excluded" in HELP
    assert "crapper-stamp.json" in HELP
    assert "untracked" in HELP


def _scope_tree(root: Path) -> None:
    for name in ("src/a.py", "src/x/c.py", "lib/b.py"):
        _write(root / name)


def test_source_roots_narrow_changed_files(tmp_path, capsys):
    _git(tmp_path, "init")
    _scope_tree(tmp_path)
    assert _select(tmp_path, "-s", "src", "--changed") == ["src/a.py", "src/x/c.py"]
    assert _select(tmp_path, "--changed", "-s", "src/x", "-s", "lib") == ["lib/b.py", "src/x/c.py"]
    assert capsys.readouterr().err == ""


def test_a_named_file_outside_the_source_roots_is_skipped_with_a_message(tmp_path, capsys):
    _scope_tree(tmp_path)
    outside = _write(tmp_path.parent / f"{tmp_path.name}-elsewhere.py")
    assert _select(tmp_path, "-s", "src", "lib/b.py", str(outside)) == []
    assert capsys.readouterr().err == (
        "Skipping lib/b.py; it is outside the source roots\n"
        f"Skipping {outside.resolve().as_posix()}; it is outside the source roots\n"
    )


def test_a_named_directory_keeps_only_files_under_the_source_roots(tmp_path, capsys):
    _scope_tree(tmp_path)
    assert _select(tmp_path, "-s", "src", ".") == ["src/a.py", "src/x/c.py"]
    assert _select(tmp_path, "-s", "src/x", "-s", "lib", "src") == ["src/x/c.py"]
    assert capsys.readouterr().err == ""


def test_a_named_file_inside_a_source_root_is_all_that_is_chosen(tmp_path, capsys):
    _scope_tree(tmp_path)
    assert _select(tmp_path, "-s", "src", "src/a.py") == ["src/a.py"]
    assert _select(tmp_path, "-s", "src/a.py", "src") == ["src/a.py"]
    assert capsys.readouterr().err == ""


def test_source_roots_alone_still_walk_the_roots_then_filter_and_exclude(tmp_path):
    _scope_tree(tmp_path)
    assert _select(tmp_path, "-s", "src") == ["src/a.py", "src/x/c.py"]
    assert _select(tmp_path, "-s", "src", "c.py") == ["src/x/c.py"]
    assert _select(tmp_path, "-s", "src", "--exclude", "src/x") == ["src/a.py"]
    assert _select(tmp_path, "-s", "src", "src", "--exclude", "src/x") == ["src/a.py"]


def test_help_says_source_roots_narrow_paths_and_changed():
    assert "outside the source roots" in HELP
