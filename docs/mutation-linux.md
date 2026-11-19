# Linux isolation is kept and not delivered

Native Windows x64 is the only supported host, and Linux x64 left the list with the
same decision that removed macOS (ADR 0002 in `qinyuanj/home-uml-viewer`,
`docs/adr/0002-macos-is-not-a-supported-host.md`). Nothing in the delivery surface
reaches Linux: no supported-host list names it, CI runs on Windows, and no release
artifact is built there. Its isolation stays in the code, because it is the only way
back to a Linux host. Deleting it is a separate decision, not a tidy-up that rides
along with this one.

## What remains

`src/mutator/sandbox.py` enters a private user and mount namespace once, before any
thread starts, and mounts an overlayfs view per command. `isolation.NamespaceViews`
builds each worker's base layer, hides build output, writes the source under test into
the view and unmounts it afterwards; `isolation.for_platform` returns it for every
platform that is not `win32`. `scripts/setup-ubuntu.sh` remains its toolchain setup:
Python, Lua 5.4, LuaRocks, Busted, LuaCov, cluacov 1.0.0 and luacov-reporter-lcov, then
`.venv`. ADR 0006 records why the views replaced symlink overlays.

The view is not a weaker copy. It holds the whole project tree rather than a duplicate
of it, imports and paths resolve inside it, so Node and Cargo see the mutant without
copying anything, and its writable layer is discarded with the view.

## Why it is kept

Because deleting it would close the only door back. Restoring Linux support means
running the namespace path where a row can be taken — on a host with unprivileged user
namespaces and overlayfs — and that is what this code does today. Removing it would turn
a documentation decision into a rewrite, and later Linux work would start by
reconstructing a mechanism that already has tests.

Keeping it has a price: nothing in CI runs it, so a change to `isolation.py` can break
it without a red build. That is the cost of naming one host, paid deliberately, and it
is written down here so the next reader does not take the code for an oversight.

## Linux is not refused

`mutate` keeps working on Linux; the decision is not to refuse it. A refusal belongs
where the isolation itself cannot be delivered: macOS has neither user namespaces nor
overlayfs, so `mutate` exits 1 there before running anything, and ADR 0002 makes that
refusal final. Linux has a working mechanism that the host list merely stopped naming.
Refusing it would turn a documentation boundary into a code change, make the kept path
harder to exercise, since every later run would first have to lift the refusal, and
would not make any delivered host more honest: the delivery surface never reaches Linux
either way.

The answer changes if Linux is offered again without that isolation. Then refusing is
right, because the refusal is about what cannot be guaranteed.

## macOS

macOS is refused, not kept: `isolation.for_platform` hands it the namespace views, no
user namespace can be entered, and mutator exits 1 with the reason. mutator#26 is closed
as not planned, so there is no path to restore, which is ADR 0002's decision and not one
this document reopens.

## The way back

A host with unprivileged user namespaces and overlayfs is the first requirement; on
Ubuntu 23.10 and later that means `kernel.apparmor_restrict_unprivileged_userns=0`.
Then `scripts/setup-ubuntu.sh` for the toolchain, with crapper checked out beside
mutator, and the ordinary test suite. Adding Linux x64 back to the host list — in ADR
0002, the README and CI — belongs in the same change that runs the suite there: the
namespace path needs a row, not a promise.
