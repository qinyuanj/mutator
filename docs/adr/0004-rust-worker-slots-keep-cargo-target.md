# Rust worker slots keep their Cargo target across files and runs

A Rust worker used to build the whole dependency tree from scratch for every
file: `target` is never linked into an overlay, and each file got a fresh
`run-<uuid>` overlay. On a heavy workspace (egui/wgpu/vello) that meant a
6.5 GB cold build per worker per file. Rust workers now run in numbered
worker slots at fixed paths, each with its own persistent slot target under
`target/mutation-workers/cargo-slot-<n>`. A slot is seeded from the project's
`target/debug` when it exists, and is held by a file lock so a concurrent run
takes the next free number instead of sharing it.

**Consequences**: build output is now shared across mutants of one slot, so a
stale build of the mutated crate could turn a kill into a survivor. mutator
sets the mtime of every mutated or restored source to now before building,
so Cargo's fingerprint always sees the change. The overlay path must stay
fixed because workspace-crate fingerprints include it, so an interrupted run
removes the overlay contents and keeps the slot target. Slot targets are
removed only by `cargo clean`.

The mtime rule alone does not cover a source that goes back to being a link.
When the next file (or run) is prepared, the previous file's private copy is
replaced by a link to the original, whose mtime is older than the slot's last
build of that file's mutant; Cargo would call that build fresh. Each slot
therefore records the source it is about to mutate in `cargo-slot-<n>/mutated`
before any mutant runs, and the next prepare removes that package's
fingerprints from the slot target. The record is written first so an
interrupted or killed run is undone the same way. When the package or its fingerprints cannot
be recognised, the whole slot target is discarded instead: a cold build is
slower, a reused mutant build is wrong. The mtime is set from the
fine-grained clock rather than left to the kernel's write stamp, which can
fall in the same tick as Cargo's own build stamp; Cargo treats an equal mtime
as unchanged.

## Considered Options

- Overlays stay in `run-*`: only registry crates are reused across runs, and
  workspace crates rebuild every run.
- `cargo clean -p <crate>` before each mutant: correct without mtime handling,
  but rebuilds too much and gives back most of the saving.
