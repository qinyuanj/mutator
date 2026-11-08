# AGENTS.md

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### Parallel worktrees

Working in a `git worktree` (parallel subagents): per-worktree venv, crapper sibling, PYTHONPATH pinning. See `docs/agents/worktrees.md`.

Enable the versioned pre-commit hook once per clone with `git config core.hooksPath .githooks`; it runs `tests/test_guardrails.py` (process boundary, no CR in golden files).
