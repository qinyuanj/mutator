# AGENTS.md

## Host

Native Windows x64 is the only supported host
([ADR 0002](https://github.com/qinyuanj/home-uml-viewer/blob/main/docs/adr/0002-macos-is-not-a-supported-host.md)).
macOS is refused and stays refused (mutator#26, closed as not planned). The Linux
namespace view is kept in the code and no delivery surface reaches it; do not delete
it, and read [docs/mutation-linux.md](docs/mutation-linux.md) before changing it.

Run the tests with `.venv\Scripts\python -m pytest` from a native Windows shell.

The Lua cases need a Lua toolchain, and on this host that is
`scripts/setup-windows.ps1`: it builds Lua 5.4.8 with a winlibs MinGW-w64 GCC it
unpacks into `%USERPROFILE%\.local`, installs LuaRocks 3.12.0, Busted, LuaCov,
cluacov 1.0.0 and `luacov-reporter-lcov` (user tree only), and persists `PATH`,
`LUA_PATH` and `LUA_CPATH`. No administrator prompt and no WSL:
`powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1`, once per
machine, then a new shell. CI runs the same script, so a local row and the job
install one toolchain; the versions are the ones crapper's and dsh-uml-viewer's
installers pin, and the three must be re-pinned together. Without it the Lua
cases skip rather than fail, and a skip reads as a pass.
`scripts/setup-ubuntu.sh` remains the kept Linux development entry point
([docs/mutation-linux.md](docs/mutation-linux.md)) and is not an installation
path for the supported host.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### Parallel worktrees

Working in a `git worktree` (parallel subagents): per-worktree venv, crapper sibling, PYTHONPATH pinning. See `docs/agents/worktrees.md`.

CI runs `tests/test_guardrails.py` (process boundary, no CR in golden files) as part of the full suite on every push to main and every pull request; run `.venv/Scripts/python.exe -m pytest tests/test_guardrails.py` before a commit that changes those rules.
