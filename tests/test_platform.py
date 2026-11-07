from pathlib import Path

from mutator import isolation
from mutator.cli import MINIMUM_WINDOWS_BUILD, platform_warning, windows_refusal


def test_windows_10_22h2_and_later_run():
    assert windows_refusal("win32", 19045) is None
    assert windows_refusal("win32", 26100) is None


def test_an_older_windows_build_is_refused_naming_the_minimum():
    message = windows_refusal("win32", 19044)
    assert "19045" in message and "19044" in message
    assert MINIMUM_WINDOWS_BUILD == 19045


def test_linux_has_no_build_gate():
    assert windows_refusal("linux", None) is None


def test_native_windows_runs_in_copies_and_linux_in_namespace_views():
    assert isolation.for_platform("win32").level == "copy"
    assert isolation.for_platform("linux").level == "namespace-view"


def test_native_windows_paths_are_quiet():
    assert platform_warning("win32", Path("C:/proj")) is None


def test_windows_drive_under_wsl_suggests_the_linux_filesystem():
    assert "under ~/" in platform_warning("linux", Path("/mnt/c/proj"))


def test_linux_filesystem_paths_are_quiet():
    assert platform_warning("linux", Path("/home/me/proj")) is None
