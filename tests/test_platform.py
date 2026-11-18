from pathlib import Path

from mutator import isolation
from mutator.cli import platform_warning


def test_native_windows_runs_in_copies_and_linux_in_namespace_views():
    assert isolation.for_platform("win32").level == "copy"
    assert isolation.for_platform("linux").level == "namespace-view"


def test_native_windows_paths_are_quiet():
    assert platform_warning("win32", Path("C:/proj")) is None


def test_windows_drive_under_wsl_suggests_the_linux_filesystem():
    assert "under ~/" in platform_warning("linux", Path("/mnt/c/proj"))


def test_linux_filesystem_paths_are_quiet():
    assert platform_warning("linux", Path("/home/me/proj")) is None
