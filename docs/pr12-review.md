# PR #12 independent review — 2026-10-21

Reviewed head: `c93da2d700310c2da04f49d72ec47fb6ea7fcc57`.

## Outcome

Changes requested on the reviewed head: two reproducible gaps in the documented protections. This follow-up fixes both. Keep PR #12 draft pending successful CI on the corrected head and review of these changes. No merge or release was performed.

## Demonstrated findings

1. **Untracked work can be hidden from the submodule guard (medium).** `nested.blockers` used `git status --porcelain`, which honors `status.showUntrackedFiles=no`. In a real temporary submodule with unsaved `notes.txt`, a changed parent gitlink caused `nested.update` to advance the child's HEAD despite that work. The file survived; this demonstrates checkout movement, not data loss. The new regression failed on the reviewed head. The guard now explicitly requests all untracked files and disables submodule-ignore filtering for this safety inspection.
2. **A disappearing discovered directory is silently treated as complete discovery (medium).** `find_clones` ignored every FileNotFoundError, not just an absent default root. An injected enumeration failure below an existing root returned no error, permitting the completeness gate to treat a partial inventory as complete. The new regression failed on the reviewed head. Only missing roots at depth zero are now exempt; descendant failures reach the existing automatic-clone suppression gate.

## Evidence and coverage

- Existing suite independently rerun at the reviewed head: **134 passed, 1 skipped**. The skipped test requires a separate system git-sync installation; pinned real git-sync fixture tests ran.
- GitHub CI [39562003795](https://github.com/benpshore/syncghrepo/actions/runs/39562003795): all five jobs successful. Python job logs explicitly report 134 passed / 1 skipped; lint, audit and package build succeeded.
- CodeQL [39562003798](https://github.com/benpshore/syncghrepo/actions/runs/39562003798): Python and Actions analyses successful.
- GitHub metadata confirmed the reviewed head open, draft and unmerged, with no submitted reviews.
- Read all six changed files, plus surrounding synchronization, runner, discovery and fixtures. The existing two-level active-child regression passes, as do overlapping-root, malformed/error discovery, host-matching and occupied-destination coverage.
- Four additional real-Git probes exercise parent synchronization followed by the explicit update guard with `submodule.recurse=true`: active child branch, staged edits, unstaged edits and untracked files. Each preserves child HEAD and porcelain status; the branch case also verifies its name. Dirty children can hold back the parent before an update is attempted, which is valid protection.
- Two new negative regressions demonstrate the findings above before the fixes. All six new cases use isolated temporary repositories; discovery failure is injected. No production synchronization, real GitHub clone or remote fork mutation was used.

## Remaining limits

The protections adequately address the reproduced cases after these corrections; this is not a blanket safety certification. Bounded discovery still misses clones outside its roots/depth/exclusions; uninitialized submodules can cause duplicate storage; no disk reservation/cap or cross-process lock exists. External concurrent writers can race inspection and update. These constraints remain explicitly documented rather than solved by this patch. No macOS or owner-working-tree execution was performed. The dirty feature-branch auto-save/push behavior is intentional, so preservation does not mean all nested work remains uncommitted after a full synchronization.

## Corrected-tree validation

140 passed, 1 skipped; Ruff format/check and uv dependency audit passed. ShellCheck passed via `uv tool run --from shellcheck-py shellcheck scripts/*.sh`. The SQLite schema applied successfully using Python's sqlite3 module (the sqlite3 CLI is unavailable). Remote CI on this follow-up must be checked separately from the earlier successful run.
