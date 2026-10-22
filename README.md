# mutator

Mutation testing for Clojure, Java, Go, TypeScript, Rust, Python, and Lua. One run detects the language of each source file, applies that language's mutation rules, and writes the snapshot [uml-viewer](https://github.com/unclebob/uml-viewer) already reads.

Clojure follows [clj-mutate](https://github.com/unclebob/clj-mutate). Go follows [mutate4go](https://github.com/unclebob/mutate4go). Java follows [mutate4java](https://github.com/unclebob/mutate4java). TypeScript, Rust, Python, and Lua start with the Java decisions, spelled in that language, with the additions listed below. Function names and namespaces come from [crapper](https://github.com/unclebob/crapper), which is what uml-viewer joins to `.metrics/crap.edn`.

A mutant is killed when the tests fail or time out. It survives when the tests still pass. A site on a line the coverage report does not hit is uncovered and is not run. The score uml-viewer paints is killed / (killed + survived).

## Run

```bash
./mutator
```

The first run creates `.venv` and installs the tool. crapper has to be checked out next to this directory (`../crapper`), because that is where the namespace and function names come from. From a project root the tool walks the tree, skips `test`, `spec`, `vendor`, `node_modules`, and `target`, and writes `.metrics/mutate/<namespace>.edn`.

The first run of a tree executes every covered mutant. Start with the file you are working on.

```bash
./mutator src/demo/core.clj          # one file, or a directory
./mutator --scan src/demo           # list sites, do not run tests
./mutator --changed                 # git additions and edits
./mutator --mutate-all              # rerun killed mutants too
./mutator --no-coverage             # do not skip uncovered lines
./mutator --use-existing-coverage   # read reports already on disk
./mutator --test-command "pytest -q tests/test_demo.py"
./mutator --max-workers 3 src/demo/core.clj
./mutator --paranoid-workers        # pristine-check every worker, not once per command
```

Once a snapshot exists, the next run is differential. It reruns survivors and every site in a function whose text changed. Killed mutants in an unchanged function stay killed. `--since-last-run` is that same selection. `--mutate-all` ignores it.

Exit `0` when every executed mutant was killed. Exit `2` when the baseline tests fail. Exit `3` when a mutant survives; this is a valid result with published snapshots. A baseline failure takes precedence over survivors from other sources and does not rewrite the failed source's snapshot.

## Workers

Mutants of one file run at the same time. The default is one worker per core. `--max-workers` sets the cap. The run uses the smaller of that cap, the number of cores, and the number of selected sites. Files are taken one at a time, except full-suite Lua: its files share one worker queue, so a worker that finishes a mutant takes the next one from any file instead of waiting for the current file's slowest mutant. Each file's snapshot is still written as soon as its own last mutant finishes. `--test-command` and `--select-specs` keep the file-by-file order.

Each worker is a directory under `target/mutation-workers`. Lua schedules
previous survivors first within each file to reduce a long final tail; history
changes execution order only. The file being mutated is a private copy. A module that reaches that file through a relative import is copied too, so Node resolves the import to the private copy. The rest of the tree is linked. The original source stays unchanged. Workers are removed after success, setup failure, or runtime failure.

For Lua's shared worker pool, each distinct pristine command is checked once
per run, not once per worker; every worker's overlay is still compared
structurally before and after use. `--paranoid-workers` restores a full
pristine run in every worker.

The baseline still runs once, in the real tree, before any worker starts. The mutant command then runs inside the worker, in the same directory it would have used in the real tree, so a relative path such as `src` or `./pkg` refers to the overlay. A copy left under `target/mutator-backup/` by an interrupted older run is restored before a non-scan run.

## Snapshot

uml-viewer loads every `*.edn` file under `.metrics/mutate`. Each file is one namespace. A Java file with an inner class, or a Go file with a function and a method, therefore writes more than one file. Two Go files in the same package share one snapshot, and rerunning one of them keeps the other's forms.

Publication starts only after all selected mutants of a source finish. Each affected namespace artifact is written to a temporary file beside its destination, then replaced atomically. Readers see a complete old or new artifact; a write or replacement failure leaves the old artifact intact. Unrelated namespaces are not rewritten. A baseline or worker execution failure leaves that source's previously published results intact, and a later source's failure preserves results already published for earlier sources.

The atomicity boundary is one namespace artifact, not the whole source or run. If a source updates several artifacts and a later replacement fails, the earlier replacements remain published. Legacy snapshots are folded into the canonical namespace artifacts, preserving other sources' records, and removed only after the affected artifacts publish successfully. Stale records for the completed source are removed without deleting records owned by other sources.

```clojure
{:version 1
 :source "src/demo/board.py"
 :namespace "demo.board.Board"
 :outcomes {"[\"src/demo/board.py\",\"demo.board.Board\",\"defn/place\",10,11,\">\",\">=\"]" :survived}
 :forms [{:id "defn/place"
          :kind "defn"
          :file "src/demo/board.py"
          :line 2
          :end-line 4
          :hash "…"
          :killed 1
          :survived 1
          :uncovered 0
          :sites 2}
         {:id "defn-/_hide"
          :kind "defn-"
          :killed 0
          :survived 0
          :uncovered 1
          :sites 1}]}
```

`:namespace` is the class key crapper uses. The form id is what the viewer splits:

| Form id | Operation | Private |
| --- | --- | --- |
| `defn/place` | `place` | no |
| `defn-/hide` | `hide` | yes |

`:sites` is every mutation in the function, including uncovered ones. The viewer prints `---no mutation sites---` when that count is zero.

| Language | `:namespace` | `:name` |
| --- | --- | --- |
| Clojure | the `ns` | the `defn` / `defn-` name |
| Java | `package.Class`, or `package.Outer.Inner` | the method name |
| Go | the package import path, or `import/path.Receiver` | the function or method name |
| TypeScript | the dotted module path, or `module.Class` | the function or method name |
| Rust | `crate::module`, or `crate::module::Type` | the function or method name |
| Python | the dotted module path, or `module.Class` | the function or method name |
| Lua | the `require` path | the name as written: `helper`, `M.foo`, `Account:deposit` |

`defn-` is Clojure's private form. Elsewhere a private method, an unexported Go function, a Python name that starts with `_`, a Rust function that is not `pub`, a TypeScript `private` / `#` name, or a Lua `local function` / `local f = function` uses `defn-/`.

## What each language mutates

Tokens inside strings, comments, and quoted Clojure forms are left alone. A replacement that does not compile fails the test run and counts as killed.

| Language | Mutations |
| --- | --- |
| Clojure | clj-mutate's head symbols: `+`/`-`, `*` to `/`, `inc`/`dec`, comparisons, `=`/`not=`, `if`/`if-not`, `when`/`when-not`, `and`/`or`, and the seq, predicate, and coercion pairs. `true`/`false` and `0`/`1` anywhere. |
| Java | `true`/`false`, `==`/`!=`, `<`/`<=`, `>`/`>=`, `+`/`-`, `*`/`/`, `&&`/`||`, delete `!` and unary `-`, `0`/`1`. Constructors are not entries, matching crapper. |
| Go | mutate4go: `+`/`-`, `*` to `/` only, comparisons, `==`/`!=`, `true`/`false`, `&&`/`||`, `0`/`1`. |
| TypeScript | the Java set, plus `===`/`!==`. |
| Rust | the Java set (`&&`/`||`, `!`, unary `-`). |
| Python | the Java set, spelled `and`/`or`, `not`, and `True`/`False`. |
| Lua | the Java set, spelled `~=`, `and`/`or`, and `not`, plus `//` to `/` only. `..` and `#` are never mutated. |

Rust, TypeScript, and Python also swap binary `^` to `|`, `&`/`|`, `<<`/`>>`, and `%` to `*`; TypeScript additionally swaps `>>>` to `<<`. Compound assignments and type annotations are outside these operator rules.

Return literals already use the boolean and `0`/`1` rules, including Rust tail literals. Arbitrary return expressions and whole function bodies are not replaced: syntax alone cannot establish a valid replacement type, a Rust `Default` implementation, or whether removing side effects is appropriate. A function with no supported sites retains `:sites 0` and an `n/a` score; it has no mutants to test and is not classified as untested.

## Tests and coverage

The baseline command has to pass before any mutant runs. A mutant's timeout is ten times that baseline, and at least two seconds. `--timeout-factor` changes the multiple. `--test-command` replaces the default. Defaults:

| Language | Command | Where |
| --- | --- | --- |
| Clojure | `clj -M:spec --tag ~no-mutate` when Speclj is on the classpath, otherwise `clj -M:test` or `bb spec` | nearest `deps.edn` or `bb.edn` |
| Java | `mvn -q test -DexcludeTags=no-mutate` | nearest `pom.xml` |
| Go | `go test -count=1` of the file's package | nearest `go.mod` |
| TypeScript | `npm test` | nearest `package.json` |
| Rust | `cargo test` | nearest `Cargo.toml` |
| Python | the project's `.venv` or `venv` Python running `pytest`, or `unittest discover` | nearest project file |
| Lua | native Lua 5.4 loading `busted.runner` with `--ignore-lua`, with cluacov 1.0.0 required | nearest `.busted` or rockspec |

Built-in plans run as argument vectors without a shell, so an interpreter
path with spaces stays one argument. Lua uses crapper's interpreter discovery
and LuaRocks wrapper module-path setup, then invokes Busted in that executable
without Busted's shell-based `--lua` re-execution. Busted configuration and
failure exit codes are preserved; baseline tests do not enable coverage.

The generated Lua command confines module search paths to the LuaRocks trees
reported by `luarocks config rocks_trees`, passed through the environment so
nested test subprocesses inherit them. Inherited `LUA_PATH` entries survive
when they are relative or inside a tree. Nothing is pinned unless busted
itself resolves inside a tree; `MUTATOR_LUA_NO_PIN=1` opts out. The command's
bootstrap also caches compiled chunks (`string.dump`, keyed by chunk name and
content) and, on Lua 5.4 with Busted 2.3.0, replaces the per-element
`debug.traceback` with a Lua-side version that formats only the frames busted
keeps. `MUTATOR_LUA_NO_CACHE=1` and `MUTATOR_LUA_NO_FASTTRACE=1` disable each
part; verbose failure output is unchanged outside one enumerable class
(`docs/adr/0002-fasttrace-verbose-text.md`).

Without `--select-specs`, each mutant still runs every spec of the suite, but
the specs that cover the file's mutants come first, so a killing spec fails
early under `--no-keep-going` (`docs/adr/0003-covering-specs-first.md`). Each
Lua test run is capped at 2 GiB of address space so a runaway mutant fails
instead of exhausting memory; `MUTATOR_LUA_MEMORY_MB` sets the cap and `0`
removes it.

A fresh Lua coverage run doubles as the test baseline: the suite already
passed, so it does not run again, and the mutant timeout derives from the
coverage-inclusive duration (`docs/adr/0001-coverage-run-as-baseline.md`).
Use the matching sibling crapper checkout, which supplies this bootstrap helper.
A `--test-command` you supply still runs through the shell as typed, with its
quoted arguments intact. The runner returns the subprocess's original status;
the mutation CLI maps a failed baseline to exit `2` and surviving mutants to
exit `3` with published results.

For a named task already defined in your project's `.busted`, select it with:

```bash
./mutator --busted-task fast --no-coverage src/calc.lua
```

The generated Lua command adds `--run fast` to both the baseline and every
mutant while keeping the native interpreter and LuaRocks module-path setup.
Task definitions belong to the consuming project. `--busted-task` affects Lua
tests only. Lua spec coverage is collected for that same task; coverage commands
and tests in other languages keep their existing plans. Custom coverage commands
and `--full-suite` retain crapper's ordinary coverage path. Use `--no-coverage`
to mutate every site without selecting tests by coverage.

Combining `--busted-task` with `--test-command` is a usage error (exit `1`).
For a custom command, include task selection yourself, for example
`--test-command "busted --run fast spec"`; mutator runs that command unchanged.
When changing test tasks, use `--mutate-all` to recompute outcomes previously
killed under a different test set.

Lua uses the full suite by default, with Busted's `--no-keep-going` to skip
later tests after a failure. `--select-specs` explicitly enables spec-file
selection for suites whose spec files behave independently. Shared state across
files can change mutation outcomes in either direction, even when all pristine
subset checks pass. Use the default full suite for such projects.

With `--select-specs`, Lua collects line coverage and spec-file membership in a
single Busted + LuaCov run. Each mutant runs the spec files that covered its
line. Busted's auto-insulation remains enabled. Lines executed outside a spec
file, such as module initialization in a helper, and unknown membership fall
back to the full suite. `--full-suite` explicitly retains the default and cannot
be combined with `--select-specs`; custom `--test-command` strings are never
rewritten. A mutant that survives its selected tests is confirmed against the
full suite before it is recorded as survived. This catches missed downstream
assertions, but cannot establish independent spec behavior or rule out false
kills caused by omitted shared-state setup. When switching between selected
and full-suite runs, use `--mutate-all`: existing killed outcomes were measured
under the previous scope and are otherwise retained by differential selection.

Each distinct selected command must also pass on pristine source; a failing
subset falls back to the full suite. These checks are cached for the run and
provide a measured lower bound for its timeout. Coverage timing includes shared
suite startup as well as spec time, so helper and interpreter startup are not
lost from a short plan's timeout.

Every pristine worker is checked before any mutant runs. For selected Lua tests,
each worker reserves its first mutant and checks that mutant's exact test command.
All workers must pass before mutations begin. Each worker checks any new command
on pristine source before first using it; successful checks are cached within that
worker. A failing worker check stops publication for that source.
Selected mutants use the larger of `max(2 seconds, --timeout-factor × (shared
suite overhead + selected spec wall times))` and the measured pristine selected
command's timeout. Coverage times are measured during collection and include
coverage overhead. Full-suite mutants retain the ordinary baseline timeout.

The spec index is stored next to the managed LuaCov stats and LCOV report under
`target/coverage/lua`. Runs with `--select-specs` normally rebuild it. `--reuse-coverage` (also
`--use-existing-coverage`) reuses a valid index for the same Busted task and
command; a missing or invalid index triggers fresh collection. Explicit reuse
can use stale source coverage. Use fresh coverage after changing source or tests.

`--timings` prints coverage, baseline, worker preparation, worker checks, mutant
and survivor-confirmation totals, followed by the mutation run's wall time.
Concurrent worker phase totals sum thread time and can exceed elapsed wall time.
An unchanged selection containing only previously killed mutants skips coverage and keeps
the existing snapshots. Survivors continue to run on each invocation.

For other languages, coverage is generated with crapper's commands unless
`--use-existing-coverage` or `--no-coverage` is set. A file missing from the report
is treated as uncovered, and the run says so.

## WSL2

Native Windows is not supported: mutator exits with status 1 and points at
this section. Run it inside WSL2 with Ubuntu.

1. In an administrator PowerShell, run `wsl --install` and reboot.
2. Open Ubuntu and clone into the Linux home directory, not `/mnt/c`.
   Windows drives are slow under WSL2 and break symlinks:

   ```bash
   cd ~
   git clone -b lua https://github.com/qinyuanj/crapper.git
   git clone -b lua https://github.com/qinyuanj/mutator.git
   ```

3. Run `mutator/scripts/setup-ubuntu.sh`. It installs Python, Lua 5.4,
   busted, luacov, cluacov 1.0.0, and luacov-reporter-lcov, then creates `.venv`.
4. Edit in VS Code with the WSL extension (`code .` from Ubuntu).

mutator warns when a WSL project is under `/mnt/`.

Custom `--test-command` strings run through `/bin/sh`. The runner returns the
test process's status, while the mutation CLI returns its aggregate statuses
(`0`, `2`, or `3`) described above. A timed-out test command is killed by
process group, which cleans up its descendants.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
