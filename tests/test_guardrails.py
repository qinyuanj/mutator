"""Repository rules that review kept catching by hand.

Fast and tool-free, so the pre-commit hook runs them on every commit.
"""

import ast
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Modules allowed to start processes, relative to src/, each with its reason.
# Everything else goes through crapper.platform.run. sandbox.py enters Linux
# user and mount namespaces in-process with unshare(2) and starts nothing, so
# it needs no entry; a namespace helper that launches would get its own.
PROCESS_BOUNDARY = {
    "mutator/runner.py": "the test-command boundary: process groups and memory limits on Linux, "
                         "winjob Job Objects on Windows, interrupt reaping",
}

# Calls that start a process, by module and attribute; a trailing * is a prefix.
_LAUNCHERS = {
    "subprocess": ("Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"),
    "os": ("system", "popen", "spawn*", "exec*", "posix_spawn*"),
    "asyncio": ("create_subprocess_*",),
}


def _launcher(module: str, name: str) -> bool:
    return any(name.startswith(pattern[:-1]) if pattern.endswith("*") else name == pattern
               for pattern in _LAUNCHERS.get(module, ()))


def _imported_names(tree: ast.Module) -> dict[str, tuple[str, str | None]]:
    """Local name -> (module, attribute) for every import of a launcher module or launcher."""

    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _LAUNCHERS:
                    names[alias.asname or alias.name] = (alias.name, None)
        elif isinstance(node, ast.ImportFrom) and node.module in _LAUNCHERS:
            for alias in node.names:
                names[alias.asname or alias.name] = (node.module, alias.name)
    return names


def _launches(path: Path) -> list[int]:
    """Lines of `path` that call a process-launching API."""

    tree = ast.parse(path.read_bytes(), filename=str(path))
    names = _imported_names(tree)
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            module, attribute = names.get(func.value.id, (None, None))
            target = (module, func.attr) if attribute is None else (None, None)
        elif isinstance(func, ast.Name):
            target = names.get(func.id, (None, None))
        else:
            continue
        if target[0] is not None and target[1] is not None and _launcher(*target):
            lines.append(node.lineno)
    return lines


def test_processes_start_only_inside_the_platform_boundary():
    source = ROOT / "src"
    violations = [
        f"src/{path.relative_to(source).as_posix()}:{line}"
        for path in sorted(source.rglob("*.py"))
        if path.relative_to(source).as_posix() not in PROCESS_BOUNDARY
        for line in _launches(path)
    ]
    assert not violations, (
        "These lines start a process outside the platform boundary; run tests through mutator.runner, "
        "anything else through crapper.platform.run (Job Objects, argv resolution, batch quoting):\n  " + "\n  ".join(violations)
    )


def test_the_boundary_names_only_existing_modules():
    assert all((ROOT / "src" / module).is_file() for module in PROCESS_BOUNDARY)


@pytest.mark.parametrize("source, expected", [
    ("import subprocess\nsubprocess.run(['x'])\n", [2]),
    ("import subprocess as sp\nsp.check_output(['x'])\n", [2]),
    ("from subprocess import Popen as P\nP(['x'])\n", [2]),
    ("import os\nos.system('x')\nos.execvp('x', ['x'])\nos.spawnl(0, 'x')\n", [2, 3, 4]),
    ("import asyncio\nasyncio.create_subprocess_exec('x')\n", [2]),
    ("import subprocess\nsubprocess.CompletedProcess(['x'], 0)\nx: subprocess.Popen\n", []),
    ("import os\nos.replace('a', 'b')\nos.environ.get('x')\n", []),
])
def test_launches_are_found_through_any_import_spelling(tmp_path, source, expected):
    path = tmp_path / "module.py"
    path.write_text(source, encoding="utf-8")
    assert sorted(_launches(path)) == expected


def _tracked(*pathspecs: str) -> list[str]:
    listed = subprocess.run(["git", "ls-files", "-z", "--", *pathspecs], cwd=ROOT, capture_output=True)
    if listed.returncode != 0:
        pytest.skip("not a git checkout")
    return [name for name in listed.stdout.decode("utf-8").split("\0") if name]


def test_golden_files_are_checked_out_with_lf():
    crlf = [name for name in _tracked("tests/fixtures", "*.edn") if b"\r" in (ROOT / name).read_bytes()]
    assert not crlf, (
        "These tracked golden files contain CR bytes, so byte-for-byte comparisons will fail:\n  "
        + "\n  ".join(crlf)
        + "\nFix: keep `*.edn` and `tests/fixtures/**` as `text eol=lf` in .gitattributes, then delete "
        "those working copies and re-checkout them: git checkout -- <files>"
    )
