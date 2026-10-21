# Mutation testing

The terms used to describe mutation outcomes and the test scope behind them.

## Language

**mutant**:
A source variant with one deliberate change at a mutation site.

**killed mutant**:
A mutant whose selected test command fails or times out.

**surviving mutant**:
A mutant whose selected test command passes. Survival is relative to that command's test scope.

**uncovered mutant**:
A mutation site whose source line has no recorded execution coverage. It has no executed mutation outcome.

**spec-file selection**:
A test scope containing the spec files recorded as executing a mutant's source line.
_Avoid_: equivalent suite

**independent spec files**:
Spec files whose test behavior is unaffected by the presence, absence, or execution order of other spec files.

**pristine worker check**:
A run of a mutant's exact test command inside an unmutated worker before that command executes against mutated source. With the shared Lua worker pool, each distinct command is checked once per run and the remaining workers are validated structurally; `--paranoid-workers` restores one check per worker.

**full-suite confirmation**:
A second execution of a tentatively surviving mutant against the complete test scope. It establishes survival in that scope and does not establish independence between spec files.
