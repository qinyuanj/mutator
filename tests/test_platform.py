from pathlib import Path

from mutator.cli import UNSUPPORTED_PLATFORM, platform_warning


def test_native_windows_is_refused():
    assert platform_warning("win32", Path("C:/proj")) == UNSUPPORTED_PLATFORM


def test_windows_drive_under_wsl_suggests_the_linux_filesystem():
    assert "under ~/" in platform_warning("linux", Path("/mnt/c/proj"))


def test_linux_filesystem_paths_are_quiet():
    assert platform_warning("linux", Path("/home/me/proj")) is None
