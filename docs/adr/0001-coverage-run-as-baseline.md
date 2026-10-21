# Coverage run doubles as the Lua test baseline

Standard Lua coverage is collected by mutator's own collector, which runs the
full Busted suite once. A fresh, passing collection already proves the
pristine suite passes, so mutator no longer runs the suite a second time as a
separate baseline; the baseline is seeded from the coverage run (monopoly's
src/computer: ~48s saved per run).

**Consequences**: the mutant timeout base is the coverage-inclusive suite
duration (~60s) instead of the bare suite duration (~48s). A mutant that
would have finished between 10× the old base and 10× the new base now
survives where it previously died by timeout. This is the one accepted
exception to the rule that every mutant's killed/survived/uncovered outcome
stays identical; in practice the window almost never contains a mutant. A
reused coverage index (`--use-existing-coverage`) does not seed: the suite
runs as the baseline again, because nothing fresh proved it passes.
