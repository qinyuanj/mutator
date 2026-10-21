# fasttrace: trimmed Lua-side tracebacks for Busted

Suite loading on large projects is dominated by one `debug.traceback` call
per `describe`/`it` registration: Lua 5.4's `luaL_traceback` scans all of
`package.loaded` per stack frame to find global function names, and Busted
discards all but the first 2–3 frames anyway (~22s of a 33s `--list` on
monopoly; see `docs/research/busted-internal-overhead.md`). mutator's
bootstrap replaces Busted 2.3.0's `getTrace` with a version whose traceback
is produced by `fasttrace.trimmed`, a Lua-side formatter that emits only the
frames `trimTrace` keeps, byte-replicating `luaL_traceback`'s format.

**Consequences**: killed/survived outcomes and TAP output are unchanged.
Verbose failure output differs in one enumerable class: when a kept frame's
function is itself a value in `package.loaded`, the stock formatter prints
the global name (`in function 'mod.name'`) where fasttrace prints the
namewhat form (`in local 'register'`). On monopoly's 7229 registrations this
class occurred once; an exact mode exists for reconciliation tests. The patch
engages only on Lua 5.4 with Busted 2.3.0 and otherwise falls back to
`debug.traceback`; `MUTATOR_LUA_NO_FASTTRACE=1` disables it. An upstream PR
to lunarmodules/busted is the long-term home for this fix.
