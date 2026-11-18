# Windows has no build-number gate

mutator decides on the operating system alone. Every Windows build runs; there is
no minimum build number, as for uml-viewer itself (dsh-uml-viewer ADR 0018). This
removes the floor ADR 0007 inherited from uml-viewer ADR 0010 and drops
`MINIMUM_WINDOWS_BUILD` with the build-number plumbing and boundary tests behind
it (mutator#30). Nothing else about the platform decision changes: Linux and
native Windows are delivered, macOS keeps the rule its isolation already applies,
the exit codes and their position before any work are as before, and the WSL2
`/mnt` warning stays.

No capability was gated by the number. The Windows surface is kernel32 and ntdll
(Job Objects, `LockFileEx`, `MoveFileExW`, `CreateProcessW`, `GetProcessTimes`),
all of which predate Windows 10, and Rust's standard library already requires
Windows 10 or later, so a floor below that adds nothing at all. A first run on
Windows 10 20H2 (build 19042) refused before doing any work and left the downstream
overlay stale.

The declared copy-isolation level is untouched: the volume and build reported in
the isolation summary describe which copy method a volume supports (ReFS block
cloning from Windows 11 24H2), not whether a host may run. Other volumes and
builds take full copies with the same results.

## Considered options

- Lower the floor to build 19042: the number is a date, so it recurs every time an
  older machine appears; this issue is the second round of exactly that.
- Gate on a capability probe instead of a build number: the APIs used need no
  probe, and a probe would add a failure mode where none exists.
- Keep the floor and tell 20H2 users to use WSL2: refuses a host that works, and
  hides the tool from the machine that renders the diagram.
