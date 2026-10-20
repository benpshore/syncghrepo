# syncghrepo

Keep every local clone of your GitHub repos up to date, safely, and list the
work still open in each one, for you or for an agent to triage.

It is the stage before `reconcile-tasks`; `makeghrepo` creates the repos.
Standard library plus the `git` and `gh` CLIs; no other dependencies.

## What it does

1. **Finds your repos** with `gh`: repos you own, your organisations' repos,
   repos you collaborate on, and repos you opened pull requests in.
2. **Finds your clones** under `~/code` (or `--root`, or `$SYNCGHREPO_ROOTS`,
   colon-separated), and matches them to GitHub by their `origin` URL.
3. **Updates each clone safely** (skipped with `--dry-run`):
   - forks: `gh repo sync owner/name` first (fast-forward only, never `--force`);
   - `git fetch`, then `git pull --ff-only` only if the clone is on a branch with an
     upstream, has no uncommitted changes to tracked files, has no merge or rebase
     in progress, and has not diverged.
   It never forces, stashes, resets or discards. Dirty, diverged, detached and
   branch-without-upstream clones are reported and left alone. Untracked files
   do not block a pull (git refuses to overwrite them) but are reported.
4. **Reports unresolved work** per repo: open PRs (yours, drafts, ones needing
   review), failing CI on the default branch, issues assigned to you, unpushed
   and stale local branches, branches whose upstream was deleted, stashes, and
   linked worktrees. For repos you only contributed to or merely cloned, only
   your PRs and PRs requesting your review are listed.

## Use

```sh
uv tool install git+https://github.com/benpshore/syncghrepo
syncghrepo --dry-run           # change nothing; local state as of the last fetch
syncghrepo                     # sync, then report
syncghrepo --json              # the full report as JSON, for agents
syncghrepo --root ~/src --root ~/code --depth 3 --stale-days 30
syncghrepo --no-github         # local clones only, no gh calls
```

From a checkout: `uv run syncghrepo --dry-run`.

`--dry-run` makes no network writes and no local writes: no `gh repo sync`, no
fetch, no pull. It still reads from GitHub. Each command times out after
`--timeout` seconds (60); `--jobs` repos run in parallel (8).

Exit status is 0 when the report is complete, 1 when GitHub could not be listed
(the report then covers what it could), and 2 for bad arguments.

The JSON has `summary` counts and one entry per repo with `sources`, `clones`
(status, branch, ahead/behind, changes, branches, stashes, worktrees, actions),
`remote` (CI, PRs, assigned issues) and a short `attention` list of strings.
Clone `status` is one of `updated`, `up-to-date`, `ahead`, `would-pull`,
`dirty`, `diverged`, `detached`, `no-upstream`, `busy` or `error`.

## Checks

Run these before opening a PR. CI runs the same ones.

```sh
uv run ruff format && uv run ruff check && uv run pytest && uv audit --preview-features audit-command
shellcheck scripts/*.sh
sqlite3 -bail :memory: ".read sqlite/schema.sql"   # the app opens ${APP_DATA:-.}/app.db
```

The owner (`benpshore`) may push their own work directly to `main` without a PR or passing CI. CI still runs after a push; releases retain their CI gates. Force-push, deletion, and merge commits remain blocked for everyone.

Other accounts must use a squash PR with passing, up-to-date `ci`. Agents must use PRs and CI even when authenticated as the owner (see `AGENTS.md`). GitHub checks the pushing account, so enforcing a human/agent boundary requires separate credentials.
