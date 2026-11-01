from pathlib import Path

import pytest

from mutator.crapper_link import ensure_crapper


def test_an_older_crapper_names_what_it_lacks(monkeypatch):
    crapper = ensure_crapper()
    monkeypatch.delattr(crapper.runners, "stale_files")
    with pytest.raises(ImportError) as raised:
        ensure_crapper()
    assert str(raised.value) == (
        f"crapper at {Path(crapper.__file__).parent} is too old for mutator: it has no "
        "crapper.runners.stale_files. Update that checkout."
    )


def test_a_crapper_without_source_root_scoping_is_too_old(monkeypatch):
    crapper = ensure_crapper()
    monkeypatch.delattr(crapper.discover, "under_roots", raising=False)
    with pytest.raises(ImportError) as raised:
        ensure_crapper()
    assert "it has no crapper.discover.under_roots." in str(raised.value)
