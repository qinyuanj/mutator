# Windows workers run in declared copies

This partially supersedes ADR 0006 **for native Windows only**, as required by
mutator#27 and uml-viewer ADR 0010. Windows 10 22H2 (build 19045) and later run
with copy isolation. Linux still uses private user/mount namespaces and
fresh overlayfs views; if namespaces are refused it exits 1. There is no
Linux fallback to copies. macOS isolation (#26) is a separate decision.

Every pristine check and mutant gets a fresh project copy at the same worker
path. The isolation seam owns prepare/view/remove/cleanup and environment
redirection; the engine and scheduling retain their existing worker API.
Both implementations compute hidden build-output paths the same way: at the
project root and beside directories on the path to a source or bound source.
Bound Lua sources override the copy, then the source under test is written
with the current fine-grained mtime. Rust slot paths and persistent targets
stay as ADR 0004 requires. Installed node_modules is copied too: a junction
to it would let a mutant write into the checkout. This can be expensive on
NTFS, but does not introduce a new hole in project isolation.

The file copier is a parameter of copy isolation, leaving a place for a third
implementation using clonefile (#26). Windows uses shutil.copy2 (CopyFile2
on CPython 3.12+), keeping mtimes and automatically block-cloning on ReFS with
block refcounting on Windows 11 24H2 (build 26100) and later. Other volumes or
builds make full copies with the same results. The summary names the volume
and method; the declared isolation remains `copy` in either case. Detection
uses the project's volume, not the system drive. This host has NTFS only;
acceptance exercises actual full copies and declared ReFS/flags/build
fixtures for the block-clone decision, not actual ReFS cloning.

Every command starts suspended, enters a kill-on-close Job Object, then
resumes, so even a venv redirector's early child belongs to the job. crapper's
platform module owns adoption and job handle lifecycle; mutator adds timeout
control, output streaming, Lua per-process commit limits and Rust job-wide
resident-memory accounting. crapper.platform.run handles ordinary tool
probes, resolve handles PATHEXT, and crapper.atomic writes snapshots through
its replace retry. The command's descendants end when the leader exits,
times out or is interrupted; killing mutator closes its job handles too.
Windows Ctrl+C/Ctrl+Break are handled while the main thread polls an explicit
run-completion event. It waits for worker cleanup before deleting leftovers.

Home, profile, temp and application-data variables (`HOME USERPROFILE TEMP
TMP APPDATA LOCALAPPDATA`) point at fresh directories beside each copy, so a
test walking its project does not discover its redirected home. Existing
Cargo/rustup, Go, Python-user-site and npm locations are pinned before those
variables change; LuaRocks module paths are pinned before launch too. These
are toolchain inputs, outside the redirected locations. The initial baseline
command also gets a private home under a locked target/mutation-workers run;
only its working directory remains the real tree, as before. The copy and home
are deleted after the command. Read-only files are made writable and Windows
sharing failures are retried. A killed mutator leaves copies on disk: run
locks distinguish live runs from abandoned ones, which the next mutation
run sweeps. Slot workers are cleaned only after their slot lock is acquired.
Locks are released by the OS at process death, so concurrent runs take
different slots and keep their own targets.

`:isolation :copy` on Windows and `:isolation :namespace-view` on Linux are
written on **both** hosts. A symmetric declaration makes the meaning of the
field explicit, including Linux's stronger mechanism, and permits byte
comparison with just that field changed. Snapshot version and other fields
are unchanged; publication encodes UTF-8 LF bytes on every host. Byte parity
means the same source, results and tested-at time; the existing timestamp is
still a real run timestamp. Reused outcomes are not rerun merely to change a
declaration, and an untouched snapshot retains the declaration of its last
publication. When a namespace retains another file's records, isolation names
the run publishing that artifact, not historical provenance per outcome.

Before adding the field, uml-viewer's Windows integration branch was checked:
`crates/scene/src/overlay.rs::load` parses generic EDN maps and reads only
namespace/source; `paint` reads forms by name. Unknown keys are tolerated.
No viewer change is needed. Version-1 readers in mutator also ignore unknown
keys, including old snapshots without isolation. The summary declares the
mechanism used for the current run.

Copy isolation protects relative writes into the copied tree and the
redirected locations; it is not an OS security boundary. Absolute paths,
external directory links and toolchain inputs can still reach outside it;
registry, network and other OS resources are not contained. Project links are
recreated without traversal; directory junctions substitute where Windows
refuses symlink creation, and file links are copied if that privilege is
unavailable. Internal directory links remain rejected by worker reuse. Copies also refuse
internal directory links and absolute links back into the project before any
command starts; recreating such a binding could expose the checkout.
Windows shell overrides use cmd.exe; generated argument vectors resolve
through PATHEXT. A .cmd/.bat program still parses its own arguments under
cmd.exe rules. Native Lua may be unable to open non-ANSI source paths.

## Considered options

- Silently use copies when namespaces fail: violates ADR 0006 on Linux and
  conceals the Windows isolation difference.
- Junction project siblings or node_modules into every worker: cheaper but
  sends relative writes back into the checkout, the defect fixed by #25.
- AppContainer, Windows Sandbox or containers: stronger enforcement but
  outside this ticket and not available as an ordinary developer process.
- Explicit FSCTL_DUPLICATE_EXTENTS_TO_FILE: unnecessary complexity for the
  supported modern ReFS automatic-clone path; full copying works elsewhere.
