# PR #11 independent qualification — 2026-10-21

Scope: merge f57e89616399f287bb5fc401edb9f16079f2b978. No later commits or open PRs were returned at inspection. All execution used Linux temporary repositories; no owner's working trees or remote forks were synchronized.

## Findings and corrections

1. **Recursive submodule update moved an active checkout.** A two-level real Git fixture had a changed parent gitlink and a child at the old parent's recorded commit, on `active-work`. The old blocker checked only `+` entries, so recursive update detached the child and moved it to the new commit. The original code failed the regression; the correction checks all initialized descendants before a recursive update. Dirty checks that fail now block updates, as do in-progress Git operations. Local branches and unpublished commits remain protected. Blocking is deliberately conservative: even an unrelated matching child on a branch can hold back initialization.
2. **Overlapping discovery roots ran parent and child concurrently.** A clone explicitly listed as both a root and a descendant was deduplicated but processed in the top-level thread pool alongside its parent. An execution probe reproduced overlapping calls. Top-level work now runs in ancestry batches, preserving parallelism between unrelated clones.
3. **Incomplete filesystem discovery allowed cloning.** Injecting PermissionError during real local discovery left `complete=True`; failed submodule status was also silently ignored. Those errors now enter the report and suppress automatic cloning. Unparseable submodule-status output is treated as failure. Missing default roots remain permissible for first use.
4. **Origin matching accepted unrelated hosts and local paths.** `https://notgithub.com/owner/repo.git` and `/local/github.com/owner/repo.git` matched GitHub identities. Matching now requires an anchored GitHub transport/host. Existing tests cover HTTPS, SSH, scp-style URLs, and case-insensitive identity.

## Verified GitHub metadata versus simulations

The authenticated connector returned 22 owned repositories; a second page was empty. One was archived (`scholarlypdftools`). Aggregate GitHub `size` metadata was 185,558 KiB (about 181 MiB); this is **not** a local disk-space estimate. Worktrees, LFS, submodules, pack layout, and future growth can add substantially more. Connector visibility may differ from the CLI's credentials.

A plan-only call using these repository identities/archive flags and an empty temporary destination returned 22 `would-clone` records, including the archive. Fork flags were not supplied by this inventory endpoint and were set false for this narrow clone-planning exercise; they do not affect clone selection. This is a metadata-backed simulation, not a dry-run inventory of the owner's Macs.

## Behavior retained and limits

- Archives intentionally remain clone candidates and are labeled. Archived clones cannot auto-push through the application's permission gate; ordinary local fetch/fast-forward and submodule initialization can still run.
- Existing files, directories and dangling symlinks at destinations are skipped. New regression cases exercise each; existing tests check repeat-run idempotence and preservation of occupied content. The pre-clone existence check is not an atomic cross-process lock.
- Dirty nested feature branches intentionally use the existing auto-save/push workflow. Existing real-Git tests verify this and verify that an untracked nested clone holds back its parent instead of being committed as a gitlink. This is synchronization, not read-only inspection.
- Normal discovery is bounded: root depth 3, nested depth 4; hidden/build/vendor directories and symlinks are excluded. Clones outside that scope, SSH aliases not recognizable as GitHub, and uninitialized submodules do not reliably suppress standalone cloning. A standalone clone plus later submodule initialization can therefore duplicate storage. No claim of global clone uniqueness is justified.
- Plain `--dry-run` executes no cloning, fetching, updating, or pushing. Existing API tests verify `--api --dry-run` does not synchronize forks. `--api` alone may mutate remote forks.
- No global storage cap, disk-space reservation, or cross-process synchronization lock was added. Eight unrelated clone jobs may run concurrently. The deliberate one-command and archive policies were preserved.
- No macOS execution, actual machine clone inventory, real `gh repo clone`, real GitHub fork mutation, or production concurrency qualification was performed.

## Validation

Original suite: 123 passed, 1 skipped. Revised suite: 134 passed, 1 skipped. Eleven new cases include a real recursive Git reproduction, overlapping-root scheduling, injected discovery/status failures, host matching, and occupied paths. Git/Git-sync run against temporary local remotes; GitHub/ghq are synthetic. The skipped test checks a separately installed system git-sync; tests using the pinned fixture execute normally.

Ruff formatting/lint and uv dependency audit passed. Additional repository gates and remote CI are reported in the PR rather than assumed here.
