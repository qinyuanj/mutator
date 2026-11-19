# A worker copy past 260 characters cannot be torn down

Deleting a Rust worker's project copy fails on native Windows with
OSError [WinError 145] (the directory is not empty), deterministically, in
three tests: twice in tests/test_rust_slots_cargo.py and once in
tests/test_worker_slots.py. Windows copy isolation is the only isolation mutator
delivers, so its teardown failing means mutate is not trustworthy there.

## What happened

    E   OSError: [WinError 145] 目录不是空的。:
        ...\run-c3a5dda7-.../worker-0/target/debug/.fingerprint/util-1b53a86fb319fd84
    src\mutator\isolation.py:104: OSError      (_retrying -> os.rmdir)

Reported from #9's reproduction: the three children of .fingerprint/util-* were
listed but never removed; re-listing before each rmdir did not help; the same
directory deleted fine by hand after the test.

## Cause

Not a handle, not a read-only bit, not a race with a process exit. It is
Win32's MAX_PATH.

- The host reports HKLM\...\FileSystem LongPathsEnabled = 0, so a Win32 path
  longer than 260 characters is refused with ERROR_PATH_NOT_FOUND (3) unless it
  carries the \\?\ prefix.
- The failing path is one character past the limit:

      C:\Users\...\Temp\pytest-of-Administrator\pytest-NNN\test_...0\
      project\target\mutation-workers\run-<uuid>\worker-0\target\debug\
      .fingerprint\util-1b53a86fb319fd84\test-integration-test-below.json   (265)

  The directory itself is 234 characters, so it can still be listed and its
  rmdir attempted; only the three longest names inside it are out of reach
  (invoked.timestamp, at 252, deletes normally).
- Cargo and rustc prefix every path they touch, so they created those entries
  without trouble. Mutator's own deletion does not: os.scandir lists the names,
  os.unlink on one raises FileNotFoundError (WinError 3), and _retrying treats
  FileNotFoundError as "already removed" and returns. The entry is never
  deleted.
- os.rmdir on the parent then finds it non-empty. ERROR_DIR_NOT_EMPTY (145)
  arrives as a plain OSError, which _retrying does not retry (it only retries
  PermissionError), so the first attempt raises and teardown fails.

Evidence, taken at the failure point:

- os.listdir(dir) shows the three names; os.stat(name) and open(name) raise
  WinError 3 / ENOENT: the names exist but are unreachable.
- No process holds them: a full tasklist diff against a pre-run snapshot shows
  no new process, and no cargo, rustc, link or test binary is alive.
- A fresh process cannot remove them either, and rmdir keeps failing for 60 s:
  waiting never helps, because the limit is not transient.
- DeleteFileW("\\?\" + name) returns 1 and the rmdir then succeeds; the same
  call without the prefix returns 0 with error 3. This is the whole difference.
- By hand after the test the tree deletes because Explorer and MSYS rm pass
  long paths (or prefix them), not because time passed.
- Standalone: creating a file at 262 characters fails with FileNotFoundError on
  this host, at 256 it succeeds.

## Fix

delete_tree walks the tree under the \\?\ prefix on Windows (_long_path in
src/mutator/isolation.py). Nothing retries and nothing sleeps; a genuinely held
file still raises (tests/test_copy_isolation.py holds one open and expects
PermissionError), and a tree with an entry past the limit now deletes.

tests/test_copy_isolation.py:
- test_a_worker_copy_entry_past_the_path_limit_does_not_block_teardown fails
  with WinError 145 without the fix.
- test_teardown_still_raises_when_a_file_is_really_held pins the other side.

Still bounded by the same limit, deliberately untouched here: copying a project
whose own paths are past MAX_PATH (the copy would fail while writing, before
any teardown), and shutil.rmtree of a Cargo fingerprint directory in
workers._invalidate. Both are the same class of bug and neither is this one.
