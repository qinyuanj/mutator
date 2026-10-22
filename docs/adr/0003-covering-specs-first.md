# Covering specs run first in full-suite Lua mutation

A Lua mutant runs the whole Busted suite with `--no-keep-going`, and Busted
orders spec files by path. A mutant dies at the first failing spec, so its
cost was set by where its covering specs sit in that order: `test/behavior/ui`
starts about 70% into the suite, and `turn` specs around 40%
(`docs/research/ui-mutant-cost.md`, `docs/research/non-ui-mutant-cost.md`).
mutator now passes every spec file explicitly, with the specs that cover the
file's selected mutants (from the coverage index) first, then the rest in
path order.

**Consequences**: every spec still runs for a survivor, so killed/survived
outcomes keep their full-suite meaning; only the order changes. An
order-dependent spec could flip an outcome. Busted's auto-insulation reloads
modules per spec file, which makes this unlikely, and sampled runs matched the
path-order snapshots per mutation id. The pristine worker check still runs
the suite in path order once per command, not once per reordered file: a
check per file would cost a full suite run per file and give back most of the
saving. `--select-specs` keeps its own subset plans and is unaffected.
