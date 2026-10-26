# Run decisions come from the selection itself

The snapshot's killed/survived totals mix outcomes measured this run with
outcomes carried forward from the last one, so a run could not say which
mutants it actually executed or why. Each run now reports a run decision per
site: an action (`executed`, `reused`, `uncovered`, `excluded`), a reason, and
the site's previous outcome. The report follows the results table: a count
line per function and one for the run, with one line per site under
`--verbose`. It covers file-by-file runs and the shared Lua queue alike.

The reason vocabulary is a contract with differential selection, so it is not
derived after the fact. `_decide()` walks the selection branches once and
returns every site's decision; the selection is exactly the `executed` sites,
and the check that lets a run skip coverage and tests uses the same function. A new selection branch has to
name its reason there or it does not exist.

Reasons, in the order they are tested:

| action | reason | when |
| --- | --- | --- |
| uncovered | `uncovered` | no recorded coverage for the line |
| excluded | `line-filter` | outside `--lines` |
| executed | `explicit-all` | `--mutate-all` |
| executed | `no-history` | the function has no previous snapshot |
| executed | `digest-changed` | the function's text changed |
| reused | `unchanged-killed` | unchanged function, previously killed |
| executed | `previous-survived` | unchanged function, previously survived |
| executed | `id-changed` | unchanged function, no previous outcome for this id |

**Consequences**: `reused` means only a carried-forward kill; uncovered and
line-filtered sites are never counted as reuse. Files whose baseline failed or
whose source changed during the run publish nothing and report no decisions.
A run that skips coverage and tests because nothing needs executing still
reports its decisions; there every site counts as covered, which is exact,
because a site uncovered last time has no killed outcome to reuse.
