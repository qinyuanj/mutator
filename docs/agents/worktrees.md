# Testing in parallel worktrees

The main checkout may have no `.venv`. Each agent working in a `git worktree` must set up its own environment; sharing one causes editable installs to leak across worktrees and produces spurious test failures.

## Per-worktree setup

1. Create a venv inside the worktree: `python -m venv .venv`, then `pip install -e ".[dev]"`.
2. `crapper` must be a sibling of the worktree root (`ensure_crapper` looks for `../crapper/src`). In a worktrees directory, a symlink to the real crapper checkout works.
3. When running tests *against* a checkout without installing it, pin `PYTHONPATH` to that checkout's `src` — otherwise an installed editable mutator from another worktree wins the import.

## Reporting test results

The baseline is expected to be green. If a test fails on the unmodified base commit, fix it or quarantine it in the same change — do not leave "pre-existing failure" caveats for the next agent to re-verify. CI runs the suite on Ubuntu, so a red baseline blocks every PR.
