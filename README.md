# syncghrepo

Keep all work in your local clones saved and in sync, and list what is still
open, for you or for an agent to triage. One plain CLI with exit codes and
`--json`, so a Makefile can call it. Standard library plus the `git` and `gh`
CLIs; no other dependencies, and no coupling to makeghrepo or reconcile-tasks.

**Dry run by default.** Nothing is written until you pass `--apply`.

## What it does, per clone

1. **Auto-save.** Uncommitted changes become a WIP commit on the current branch.
   Never on the default branch (or `main`/`master`): there the work moves to a new
   `wip/<date>` branch first. Ignored files are never added. Secret-looking files
   (`.env`, `.envrc`, keys, `credentials*`, databases, ...), files containing
   secret-looking strings (GitHub/AWS/Slack/OpenAI/Google tokens, private keys)
   and files over 50 MB are **held back** and reported. Over 2,000 changed files
   (a missing `.gitignore`?) skips the save. Commit hooks are skipped (`--no-verify`).
2. **Sync.** `gh repo sync owner/name` for forks you can push to (never `--force`),
   `git fetch --prune`, then `git pull --rebase --autostash`. If the default
   branch is not checked out, it is fast-forwarded with `git fetch . origin/main:main`
   (git refuses non-fast-forwards and checked-out branches).
3. **Auto-clean conflicts.** Before any rebase that rewrites local commits, a backup
   ref is made: `refs/syncghrepo/backup/<branch>/<time>`. Trivial conflicts are
   resolved:
   - *identical*: both sides made the same change;
   - *whitespace*: the sides differ only in trailing whitespace, line endings or
     trailing blank lines; mine is kept. Indentation is never ignored;
   - *lockfiles*: take upstream's, then regenerate (`uv lock`, `cargo update
     --workspace`, `npm install --package-lock-only --ignore-scripts`, `pnpm install
     --lockfile-only --ignore-scripts`, `bundle lock`); `go.sum` takes the union.

   Anything else aborts the rebase (`git rebase --abort`), leaving the branch
   exactly as it was, with the backup ref, and is reported. Nothing is pushed
   after a failed pull.
4. **Push** the current branch to its upstream (`-u origin` if it has none). Never
   the default branch; never a branch whose upstream was deleted (merged PRs are
   not recreated); only when GitHub says you have write access. The only forced
   push is `--force-with-lease` on `wip/` branches, after a normal push is rejected.
5. **Report** open PRs (yours, drafts, needing review, review requested), failing
   CI on the default branch, issues assigned to you, unpushed, stale and deleted-upstream
   branches, stashes and linked worktrees. Repos come from `gh`: yours, your
   organisations', collaborations, and repos you opened PRs in.

Clones in the middle of a merge or rebase, or with a detached HEAD, are left alone
and reported. Linked worktrees are listed but not auto-saved. Nothing is ever
reset, cleaned, stashed away by hand or discarded.

## Use

```sh
uv tool install git+https://github.com/benpshore/syncghrepo
syncghrepo                     # dry run: exactly what would be committed, pushed, resolved
syncghrepo --fetch             # dry run against what is on the remotes now
syncghrepo --apply             # do it
syncghrepo --json              # full report for agents (works with --apply too)
syncghrepo --root ~/code/GitHub/proj --apply   # one clone; --root is repeatable
syncghrepo --no-github         # no gh calls: local work only, nothing pushed
```

From a checkout: `uv run syncghrepo`. In a Makefile:

```make
sync:      ; syncghrepo --apply
sync-plan: ; syncghrepo --fetch --json > sync-plan.json
```

A dry run writes nothing: no commits, refs, index or working-tree changes, and
no fetch unless `--fetch` (which only updates remote-tracking refs). Conflicts are
predicted with `git merge-tree` in a scratch object store outside the clone; a
rebase replays commit by commit, so the prediction is close but not a guarantee.

Options: `--depth 3` (how deep to search roots, default `~/code` or
`$SYNCGHREPO_ROOTS`, colon-separated), `--stale-days 30`, `--timeout 120` (seconds
per command), `--jobs 8` (clones in parallel).

**Exit status:** 0 when every clone is saved and in sync (open PRs and failing CI
are reported, not failures); 1 when something needs a person (a conflict, an
error, a held-back file, or GitHub unreachable); 2 for bad arguments.

**JSON:** `summary` counts, then one entry per repo with `sources`, `remote` (CI,
PRs, assigned issues, `can_push`), `attention` (short strings) and `clones`. Each
clone has `status` (`up-to-date`, `updated`, `would-update`, `held-back`,
`conflict`, `error`, `skipped`), `actions` (`cmd`, `ok`: true/false, or null in a
dry run, and `note`), `saved` (`branch`, `files`, `commit`), `held_back`,
`resolved`, `conflicts`, `backup_refs`, plus branch, ahead/behind, branches,
stashes and worktrees.

To recover from a backup ref: `git switch -c rescue refs/syncghrepo/backup/<branch>/<time>`.

## Checks

Run these before opening a PR. CI runs the same ones.

```sh
uv run ruff format && uv run ruff check && uv run pytest && uv audit --preview-features audit-command
shellcheck scripts/*.sh
sqlite3 -bail :memory: ".read sqlite/schema.sql"   # the app opens ${APP_DATA:-.}/app.db
```

The owner (`benpshore`) may push their own work directly to `main` without a PR or passing CI. CI still runs after a push; releases retain their CI gates. Force-push, deletion, and merge commits remain blocked for everyone.

Other accounts must use a squash PR with passing, up-to-date `ci`. Agents must use PRs and CI even when authenticated as the owner (see `AGENTS.md`). GitHub checks the pushing account, so enforcing a human/agent boundary requires separate credentials.
