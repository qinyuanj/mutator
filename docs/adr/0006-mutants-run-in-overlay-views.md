# Mutants run in overlay views

A worker used to be a symlink overlay: real directories on the path to the
source under test, every sibling a link into the project. A test that wrote a
file left it behind (issue #25). In a real directory of the overlay it failed
the structural check after the mutant, so one already killed mutant cost the
whole file its snapshot. Through a link it reached the user's checkout and no
fingerprint of the overlay could see it. Every worker shares the same link
targets and mutants run concurrently, so a write through a link cannot even be
attributed to a mutant.

Every pristine check and every mutant now runs in its own overlay view: an
overlayfs mount at the worker's path with the project tree as the lowest
layer, a base layer above it, and a fresh empty writable layer on top. The
source under test is written into the view; the view is unmounted and its
writable layer deleted afterwards. The base layer hides build output (the
names that used to be skipped when linking) with opaque directories, and for
the shared Lua pool holds the bound sources. Layers live in a per-process
temporary directory: overlayfs refuses a layer inside another layer, and the
project is one.

mutator enters a private user and mount namespace at startup, before any
thread, mapping its own uid and gid to themselves. The kernel then lets it
mount overlayfs without privileges, the views are visible to mutator and its
children only, and they disappear with the process.

**Consequences**: a mutant's writes can no longer fail a file or change the
project, so the post-mutant structural check, restoration, and pool poisoning
on a worker change are gone. Relative imports resolve inside the view, so
importer copying for Node and config copying are gone too. The source under
test is stamped with the current time in every view, the original included,
because a slot target may hold a newer build of a mutant (ADR 0004). The
worker path stays fixed, so ADR 0004's slot targets are unchanged. mutator
needs unprivileged user namespaces; where the system refuses them it exits
with status 1 instead of falling back, so a weaker isolation never runs
silently. Tests cannot mount inside a view.

## Considered Options

- Keep symlink overlays, rebuild a worker after a mutant changes it, and
  fingerprint the project after each file: fixes the aborted file, but a write
  through a link still reaches the checkout and cannot be attributed.
- Make linked targets read-only: they are the user's own files.
- A per-command `unshare` wrapper: mutator itself could not see the view to
  write the mutant into it, and every command line would change.
- Fall back to symlink overlays without user namespaces: two worker
  implementations, and isolation that silently weakens.
