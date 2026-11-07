"""Import crapper from the environment or the sibling checkout.

Function names and namespaces come from crapper so `.metrics/mutate` joins
the same operations uml-viewer joins to `.metrics/crap.edn`.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _sibling_src() -> Path:
    project = Path(__file__).resolve().parents[2]
    return project.parent / "crapper" / "src"


# Added to crapper with --changed, --exclude, coverage reuse and source-root scoping.
_NEEDS = {
    "discover": ("changed_files", "exclusion_error", "excluded", "without_excluded", "under_roots"),
    "runners": ("stale_files",),
    # Added with native Windows (crapper#10).
    "atomic": ("write_bytes",),
}


def _missing(crapper) -> list[str]:
    return [
        f"crapper.{module}.{name}"
        for module, names in _NEEDS.items()
        for name in names
        if not hasattr(getattr(crapper, module, None), name)
    ]


def ensure_crapper():
    try:
        import crapper
    except ImportError:
        sibling = _sibling_src()
        if not (sibling / "crapper").is_dir():
            raise ImportError(
                "crapper is not installed and was not found at "
                f"{sibling}. Clone github.com/unclebob/crapper next to mutator."
            ) from None
        sys.path.insert(0, str(sibling))
        import crapper
    try:
        import crapper.atomic  # noqa: F401
    except ImportError:
        pass  # Reported as missing below.
    import crapper.coverage
    import crapper.discover
    import crapper.languages
    import crapper.languages.treesitter
    import crapper.runners

    missing = _missing(crapper)
    if missing:
        raise ImportError(
            f"crapper at {Path(crapper.__file__).parent} is too old for mutator: "
            f"it has no {', '.join(missing)}. Update that checkout."
        )
    return crapper
