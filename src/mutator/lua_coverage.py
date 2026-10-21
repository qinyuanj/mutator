"""One Busted suite's aggregate coverage and conservative spec-file membership."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from mutator.crapper_link import ensure_crapper
from mutator.runner import CommandRunner, test_plan


_VERSION = 2


def _canonical(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


@dataclass
class LuaCoverageIndex:
    directory: Path
    command: list[str]
    specs: dict[str, float]
    lines: dict[str, dict[int, set[str]]]
    fallback: dict[str, set[int]]
    overhead: float = 0.0

    def select(self, source: Path, line: int) -> list[str] | None:
        key = _canonical(source)
        if line in self.fallback.get(key, set()):
            return None
        found = self.lines.get(key, {}).get(line)
        return sorted(found) if found else None

    def seconds_for(self, specs: list[str]) -> float:
        return self.overhead + sum(self.specs[name] for name in set(specs))


def _plan(root: Path, source: Path, busted_task: str | None):
    command, directory = test_plan(root, source, "lua", None, busted_task=busted_task)
    if not isinstance(command, list):
        raise ValueError("Lua coverage collection requires the generated Busted command")
    report = ensure_crapper().runners._coverage_report(root, directory, "lua")
    return command, directory.resolve(), report


def _relative_spec(name: str, directory: Path) -> str:
    path = Path(name.replace("\\", "/"))
    path = path if path.is_absolute() else directory / path
    relative = path.resolve().relative_to(directory.resolve()).as_posix()
    parts = PurePosixPath(relative).parts
    if not parts or any(part in (".", "..") for part in parts) or not path.is_file():
        raise ValueError("Invalid spec path")
    return relative


def _seconds(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid spec duration")
    return float(value)


def _line(value) -> int:
    if not isinstance(value, str) or not value.isdecimal() or int(value) <= 0:
        raise ValueError("Invalid coverage line")
    return int(value)


def _mapping(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Invalid coverage mapping")
    return value


def _decode(raw: dict, directory: Path, command: list[str], *, published: bool) -> LuaCoverageIndex:
    if type(raw.get("version")) is not int or raw["version"] != _VERSION or raw.get("complete") is not True:
        raise ValueError("Incomplete spec coverage publication")
    specs = {}
    names = {}
    for name, elapsed in _mapping(raw.get("specs")).items():
        if not isinstance(name, str):
            raise ValueError("Invalid spec name")
        relative = _relative_spec(name, directory)
        if published and relative != name:
            raise ValueError("Cached spec path must be relative and canonical")
        names[name] = relative
        specs[relative] = specs.get(relative, 0.0) + _seconds(elapsed)
    if not specs:
        raise ValueError("Busted did not complete any spec files")
    ids = raw.get("specIds")
    if (not isinstance(ids, list) or not all(isinstance(name, str) and name in names for name in ids)
            or len(ids) != len(names) or len(set(ids)) != len(ids)):
        raise ValueError("Invalid spec ID table")
    spec_ids = [names[name] for name in ids]

    def source_key(name):
        if not isinstance(name, str) or not name:
            raise ValueError("Invalid source path")
        path = Path(name.replace("\\", "/"))
        if published and (not path.is_absolute() or _canonical(path) != name):
            raise ValueError("Cached source path must be canonical and absolute")
        return _canonical(path if path.is_absolute() else directory / path)

    lines: dict[str, dict[int, set[str]]] = {}
    for name, covered in _mapping(raw.get("lines")).items():
        target = lines.setdefault(source_key(name), {})
        for number, members in _mapping(covered).items():
            if (not isinstance(members, list) or not members
                    or any(type(member) is not int or not 0 <= member < len(spec_ids) for member in members)
                    or len(set(members)) != len(members)):
                raise ValueError("Invalid spec membership")
            selected = {spec_ids[member] for member in members}
            target.setdefault(_line(number), set()).update(selected)
    fallback: dict[str, set[int]] = {}
    for name, covered in _mapping(raw.get("fallback")).items():
        target = fallback.setdefault(source_key(name), set())
        if published:
            if not isinstance(covered, list):
                raise ValueError("Invalid cached fallback")
            for number in covered:
                if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                    raise ValueError("Invalid fallback line")
                target.add(number)
        else:
            for number, hit in _mapping(covered).items():
                if hit is not True:
                    raise ValueError("Invalid fallback membership")
                target.add(_line(number))
    if not published:
        if not isinstance(raw.get("unsafe"), bool):
            raise ValueError("Missing attribution safety state")
        if raw["unsafe"]:
            for name, covered in lines.items():
                fallback.setdefault(name, set()).update(covered)
    overhead = _seconds(raw.get("overhead")) if published else 0.0
    return LuaCoverageIndex(directory, command, specs, lines, fallback, overhead)


def _identity(root: Path, directory: Path, command: list[str], busted_task: str | None,
              source_files: list[Path]) -> dict:
    return {"root": _canonical(root), "cwd": _canonical(directory), "command": command,
            "busted_task": busted_task, "source_files": sorted({_canonical(path) for path in source_files})}


def _artifact_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(root: Path, source: Path, busted_task: str | None = None, *,
         source_files: list[Path] | None = None) -> LuaCoverageIndex | None:
    """Load only a complete index for this exact project, cwd, task and command."""
    root = root.resolve()
    command, directory, report = _plan(root, source, busted_task)
    path = report.parent / "spec-coverage.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("identity") != _identity(root, directory, command, busted_task, [source] if source_files is None else source_files):
            return None
        artifacts = {"lcov": _artifact_hash(report), "stats": _artifact_hash(report.parent / "luacov.stats.out")}
        if raw.get("artifacts") != artifacts:
            return None
        return _decode(raw, directory, command, published=True)
    except (OSError, ValueError, TypeError):
        return None


def _literal(text: str) -> str:
    # Long strings preserve backslashes, non-ASCII and spaces on every platform.
    equals = "="
    while "]" + equals + "]" in text:
        equals += "="
    return "[" + equals + "[" + text + "]" + equals + "]"


def collect(
    root: Path, source: Path, *, busted_task: str | None = None,
    reuse: bool = False, verbose: bool = False, source_files: list[Path] | None = None,
) -> LuaCoverageIndex:
    """Collect once, rejecting failed suites before publishing metrics or cache."""
    root = root.resolve()
    if reuse:
        cached = load(root, source, busted_task, source_files=source_files)
        if cached is not None:
            return cached
    runners = ensure_crapper().runners
    command, directory, report = _plan(root, source, busted_task)
    stats = report.parent / "luacov.stats.out"
    config = report.parent / "luacov.cfg.lua"
    publication = report.parent / "spec-coverage.json"
    pending = report.parent / "spec-coverage.pending.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    for artifact in (report, stats, publication, pending):
        artifact.unlink(missing_ok=True)
    # LuaCov's C hook changes which lines count, so a missing or other cluacov
    # version cannot produce the baseline this index was measured against.
    runners.require_lua_cluacov(command[0])
    selected = [source] if source_files is None else source_files
    config.write_text(runners.lua_coverage_config(stats, report, selected, directory), encoding="utf-8")
    run = list(command)
    try:
        entry = run.index("-e") + 1
        marker = "require('busted.runner')"
        if marker not in run[entry]:
            raise ValueError("Unsupported Busted bootstrap")
        helper = Path(__file__).with_suffix(".lua").resolve()
        bootstrap = f"local mutator_coverage_finish = dofile({_literal(helper.as_posix())})({_literal(pending.as_posix())}); "
        run[entry] = run[entry].replace(marker, bootstrap + marker, 1)
        ending = "; os.exit(0)"
        if not run[entry].endswith(ending):
            raise ValueError("Unsupported Busted completion")
        run[entry] = run[entry][:-len(ending)] + "; mutator_coverage_finish(); os.exit(0)"
    except (ValueError, IndexError) as exc:
        raise runners.CoverageError(f"Cannot instrument generated Busted command: {exc}") from exc
    run.extend(["-c", f"--coverage-config-file={config}"])
    result = CommandRunner(verbose).run(run, directory, None)
    if result.code != 0:
        pending.unlink(missing_ok=True)
        raise runners.CoverageError(f"Lua tests exited {result.code} in {directory}.\n{result.output}")
    try:
        raw = json.loads(pending.read_text(encoding="utf-8"))
        index = _decode(_mapping(raw), directory, command, published=False)
        index.overhead = max(0.0, result.seconds - sum(index.specs.values()))
        if not stats.is_file() or stats.stat().st_size == 0:
            raise ValueError("LuaCov stats missing or empty after suite")
    except (OSError, ValueError, TypeError) as exc:
        pending.unlink(missing_ok=True)
        raise runners.CoverageError(f"Invalid Lua spec coverage in {directory}: {exc}") from exc
    _, lcov_command = runners.lua_coverage_commands(command[0], config)
    try:
        runners._record_lua_lcov(directory, result.code, stats, report, lcov_command)
        fresh = runners._fresh_lcov(report, directory)
        # Any aggregate hit without a known owner must force the full suite.
        for filename, covered in fresh.items():
            key = _canonical(Path(filename))
            for number, (hits, _total) in covered.items():
                if hits > 0 and not index.lines.get(key, {}).get(number):
                    index.fallback.setdefault(key, set()).add(number)
        spec_ids = sorted(index.specs)
        spec_numbers = {name: number for number, name in enumerate(spec_ids)}
        encoded = {
            "version": _VERSION, "complete": True,
            "identity": _identity(root, directory, command, busted_task, [source] if source_files is None else source_files),
            "specs": index.specs,
            "specIds": spec_ids,
            "overhead": index.overhead,
            "artifacts": {"lcov": _artifact_hash(report), "stats": _artifact_hash(stats)},
            "lines": {name: {str(n): sorted(spec_numbers[member] for member in members) for n, members in covered.items()}
                      for name, covered in index.lines.items()},
            "fallback": {name: sorted(covered) for name, covered in index.fallback.items()},
        }
        pending.write_text(json.dumps(encoded, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        pending.replace(publication)
    except Exception:
        pending.unlink(missing_ok=True)
        report.unlink(missing_ok=True)
        raise
    return index
