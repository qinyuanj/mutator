# Lua collector memory

Issue: https://github.com/qinyuanj/mutator/issues/11

The collector previously retained a Lua hash table of integer spec IDs for each
covered line. At shutdown it expanded every membership into an array, then
encoded the complete publication into one string. Serialized schema-2 size did
not measure these allocations or the project's own retained heap.

The collector now stores immutable byte strings with one bit per spec ID.
A segment-local transition cache shares each updated membership between lines
with the same previous membership. Discarding this cache after each segment
allows obsolete histories to be collected. Repeated saves within a spec and
repeated execution of a spec preserve IDs and do not duplicate memberships.

Publication streams the line map and caches encoded membership arrays within
each source file. It preserves schema 2, sorted integer memberships, the spec-ID
table, and separate outside-spec fallback entries. Unknown lines remain absent.
The aggregate LuaCov counters and hook handling are unchanged.

## Synthetic isolation

A standalone Lua fixture replaces Busted and LuaCov counters with deterministic
segments, isolating membership storage and publication from the real project's
heap. On Windows Lua 5.4, 4,096 source lines and 160 spec IDs produced:

| Collector | Heap before publication (KiB) | Sampled heap peak (KiB) |
| --- | ---: | ---: |
| Baseline `adc4d34` | 11,245.0 | 25,017.8 |
| Packed and streamed | 729.8 | 1,163.5 |

The baseline and candidate publications matched exactly for version, completion,
unsafe status, spec IDs, line memberships and outside-spec fallback. Spec elapsed
times were excluded. These are sampled Lua heap measurements without forced GC,
not process RSS or proof of real-project peak memory reduction.

The fixture also tests IDs through 79, sparse memberships with gaps larger than
a byte, repeated spec IDs and saves, and unknown versus outside-spec attribution.
Collector integration tests require native cluacov 1.0.0, because the pure LuaCov configuration is no longer supported.

## Real-project harness

`scripts/lua-perf/collector_memory.py` runs the baseline collector from a fixed
Git commit followed by the current collector. It records process peak working
set on Windows (sampled every 20 ms and queried again after process exit before
closing its handle), and Lua heap at segment boundaries, JSON
encode returns and publication writes. On Linux the RSS field is explicitly a
sampled lower bound; unsupported platforms fail before launching a child, and
zero RSS measurements fail rather than being reported as success. The heap sampling does not force GC and
may miss allocations between sampling points; OS working set includes the
project, Lua runtime and native modules.

Both runs use private helper, stats, configuration, JSON and log files under
`target/lua-perf/collector-memory/`, leaving the project's coverage cache alone.
The harness verifies source bytes before and after, probes native cluacov when
requested, and compares all raw memberships, fallback entries and spec IDs.
Run it only after other scheduled memory-heavy benchmarks have finished.

## Real Monopoly comparison

The sequential native-cluacov comparison completed successfully on a detached
Monopoly worktree pinned to `cdb605398d0ce73862959bc1376503ab01e14925`, using
baseline collector `adc4d34` and the packed/streamed candidate from `777908e`.
Raw measurements and exact environment paths are recorded in
[`lua-performance/collector-memory-result.json`](lua-performance/collector-memory-result.json).

| Measurement | Baseline | Candidate | Reduction |
| --- | ---: | ---: | ---: |
| Process lifetime peak working set (MiB) | 1,115.10 | 872.85 | 21.72% |
| Sampled Lua heap peak (KiB) | 831,537.33 | 733,785.67 | 11.76% |
| Instrumented collection elapsed (s) | 119.625 | 112.781 | 5.72% |

All 550 spec IDs, exact line memberships and fallback entries matched; both
publications were complete and safe. Source bytes, project HEAD, tracked file
content and Git diff hashes matched before, between and after both runs. The
full-state equality check was validated against the captured report after the
run, then added as an explicit harness assertion. Spec durations vary normally;
this also explains the small difference in JSON byte counts.

The isolated checkout used a copied experimental snapshot of the main project's
`.toolcache`, stored as real directories. Both runs used that same dependency
snapshot, including cluacov 1.0.0, without installing or bootstrapping dependencies
during measurement. This is experimental provenance, not a recommendation for
ordinary Monopoly worktree setup. No Monopoly instructions or main-checkout
source/snapshots were changed. The measurement did not overlap the scheduled
#12 benchmark; unrelated machine activity was not controlled.

The remaining 873 MiB process peak includes the project and test environment;
this change reduces collector overhead but does not establish a bound on
whole-project memory. The elapsed comparison is one instrumented sequential
pair, not an end-to-end mutation performance claim. Heap peaks are sampled;
Windows RSS is the lifetime high-water mark queried after process exit.

Private raw JSON, stats, configuration, heap measurements and logs remain under
`target/lua-perf/collector-memory/` in the profiling checkout. The harness does
not generate LCOV or a normal reusable published index; consumers requiring
coverage reuse must perform the normal collection/publication step first.
