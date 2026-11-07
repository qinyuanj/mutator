"""A CLI fixture publishes identical LF bytes apart from its declared isolation."""

from datetime import datetime, timezone
import sys

from host import WINDOWS, shell_command
from mutator.cli import run


class FixedTime:
    @staticmethod
    def now():
        # Same run time on both hosts; astimezone must not depend on host zone.
        return FixedTime()

    def astimezone(self):
        return datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)


EXPECTED = b'{:version 1 :tested-at "2026-11-07T12:00:00+00:00" :isolation :ISOLATION :source "calc.py" :namespace "calc" :outcomes {"[\\"calc.py\\",\\"calc\\",\\"defn/add\\",28,29,\\"+\\",\\"-\\"]" :killed} :forms [{:id "defn/add" :kind "defn" :file "calc.py" :line 1 :end-line 2 :hash "7f5d06820e37639e83a860f5e4c06bc64504526affd8a2c0203143ecf47713a4" :killed 1 :survived 0 :uncovered 0 :sites 1}]}\n'


def test_cli_snapshot_bytes_are_the_same_on_every_host(tmp_path, monkeypatch):
    monkeypatch.setattr("mutator.metrics.datetime", FixedTime)
    source = tmp_path / "calc.py"
    source.write_bytes(b"def add(a, b):\n    return a + b\n")
    command = shell_command([sys.executable, "-c", "from calc import add; assert add(2, 1) == 3"])
    assert run(["--root", str(tmp_path), "--no-coverage", "--mutate-all", "--max-workers", "1",
                "--test-command", command, str(source)]) == 0
    actual = (tmp_path / ".metrics" / "mutate" / "calc.edn").read_bytes()
    assert b"\r" not in actual
    level = b"copy" if WINDOWS else b"namespace-view"
    assert actual == EXPECTED.replace(b"ISOLATION", level)
