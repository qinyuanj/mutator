# Faster `--select-specs` runs

mutator does not optimize the `--select-specs` path (subset runs per mutant
plus full-suite confirmation of survivors) any further. Lua performance work
goes into the default full-suite path instead.

## Why this is out of scope

`--select-specs` was attractive because a killed mutant only ran its covering
specs instead of the alphabetical prefix of the whole suite. Since
`docs/adr/0003-covering-specs-first.md`, the default command already passes
the covering specs first, so a killed mutant stops at about the same point a
subset run would. The remaining cost in both modes is survivors, and there
`--select-specs` is strictly worse: a survivor runs the subset, then the full
suite again for full-suite confirmation.

The other advantage, a shorter subset timeout for runaway mutants, is now
covered by the per-mutant Lua memory cap from the same change (`phase_wait`
went from 620–760 s to about a minute).

`--select-specs` also carries an extra correctness premise: a subset kill is
only meaningful for independent spec files (see GLOSSARY). The default path
keeps full-suite meaning for every outcome.

Parallelizing survivor confirmation, deduplicating the full pristine check
per run, and dropping the serial subset baselines (the three fixes from
`docs/research/non-ui-mutant-cost.md` §2.1) would make `--select-specs` about
as fast as the default path, not faster, while keeping the extra premise.

## Prior requests

- #18: "perf(lua): parallelize --select-specs survivor confirmation"
