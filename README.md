# syncghrepo

Keep all work in your local clones saved and in sync, and list what is still
open, for you or for an agent to triage. One plain CLI with exit codes and
`--json`, so a Makefile can call it. Standard library plus the `git` and `gh`
CLIs; no other dependencies, and no code shared with makeghrepo or reconcile-tasks.

Pipeline: `makeghrepo` (create repos) → **`syncghrepo`** (save, sync, list open
work) → `reconcile-tasks` (turn that work into tasks). The only contract between
syncghrepo and reconcile-tasks is the versioned `--json` report below.

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

The JSON report is documented below.

To recover from a backup ref: `git switch -c rescue refs/syncghrepo/backup/<branch>/<time>`.

## JSON schema

`syncghrepo --json` prints one JSON object. `schema_version` is `1`.

**Stability.** Within a schema version, fields are never removed or renamed and
never change type or meaning; new fields may be added, so consumers must ignore
keys they do not know. Anything else bumps `schema_version`. `tests/test_schema.py`
locks every key below, so a change cannot slip in unnoticed. `attention` and
`title` strings are for people; match on `id`, `kind` and the structured fields.

**Consumers (reconcile-tasks) should read `items`**: a flat, sorted list of
unresolved work whose `id` stays the same across runs while the work is the same.

### Top level

| key | type | meaning |
| --- | --- | --- |
| `schema_version` | int | this schema's version (`1`) |
| `tool` | string | always `"syncghrepo"` |
| `version` | string | syncghrepo's version |
| `generated_at` | string | UTC time, ISO 8601 |
| `user` | string or null | GitHub login, null if GitHub was not used |
| `dry_run` | bool | true unless `--apply` |
| `roots` | [string] | folders searched for clones |
| `summary` | object | counts, below |
| `items` | [item] | unresolved work, below |
| `repos` | [repo] | full detail per repo, below |
| `errors` | [string] | run-level problems (e.g. `gh` unusable); non-empty means exit 1 |

### `items`: unresolved work

Every item has exactly these keys; absent values are `null` (`flags` is `[]`).

| key | type | meaning |
| --- | --- | --- |
| `id` | string | `<kind>:<key>`, stable across runs, unique in a report |
| `kind` | string | one of the kinds below |
| `repo` | string | `owner/name`, or the folder name for clones not on GitHub |
| `title` | string | short human-readable text |
| `url` | string or null | link on GitHub |
| `path` | string or null | local clone (or worktree) path |
| `branch` | string or null | branch concerned |
| `flags` | [string] | for `pr`: `mine`, `draft`, `needs_review`, `review_requested`; for `issue`: `assigned` |
| `updated_at` | string or null | ISO 8601 time of last change (PRs, issues, stashes) |
| `detail` | string or null | extra text: author, failing commit, reason, backup ref |

| kind | key in `id` | what it means |
| --- | --- | --- |
| `pr` | `owner/name#N` | open pull request (all PRs in your repos; only yours and review requests elsewhere) |
| `issue` | `owner/name#N` | open issue assigned to you |
| `ci_failing` | `owner/name@branch` | CI failing or erroring on the default branch |
| `github_error` | `owner/name` | the repo could not be queried |
| `conflict` | `path:branch` | pull aborted on conflicts that need a person; `detail` lists files and the backup ref |
| `error` | `path:branch` | a step failed (see the clone's `actions`) |
| `skipped` | `path:branch` | clone left alone: merge/rebase in progress, detached HEAD or no commits |
| `held_back` | `path:file` | file not auto-saved (secret-looking or too large) |
| `not_pushed` | `path:branch` | commits not pushed: default branch, deleted upstream or no push access |
| `unpushed_branch` | `path:branch` | another local branch with commits on no remote |
| `gone_branch` | `path:branch` | local branch whose upstream was deleted (merged?) |
| `stale_branch` | `path:branch` | no commits for `--stale-days` |
| `stash` | `path:commit` | a git stash |
| `worktree` | `path` | linked worktree (listed, not auto-saved) |

### `summary`

Integers unless noted: `repos`, `on_github`, `cloned`, `not_cloned`, `clones`,
`clone_status` (object: status → count), `saved_files`, `held_back_files`,
`auto_resolved`, `conflicts`, `open_prs`, `draft_prs`, `prs_needing_review`,
`ci_failing`, `assigned_issues`, `stashes`, `unpushed_branches`, `stale_branches`,
`needs_attention` (repos with any `attention`), `items` (object: kind → count).

### `repos[]`

- `name`, `url`; `github` (bool); `fork`, `archived` (bool or null);
  `default_branch`; `sources`: how it was found, from `owner`, `org`,
  `collaborator`, `contributed` (you opened PRs there), `local` (only cloned).
- `attention` ([string]): the human summary lines.
- `remote_error` (string or null).
- `remote` (object or null, from GitHub): `name`, `url`, `fork`, `archived`,
  `parent` (`owner/name` or null), `can_push`, `default_branch`, `ci` (`success`,
  `failure`, `error`, `pending`, `expected` or null), `ci_commit`, `open_prs` (total),
  `assigned_issues_total`, `prs` and `assigned_issues` (first 50 each).
  - each PR: `number`, `title`, `url`, `author`, `mine`, `draft`,
    `review_decision` (GitHub's, or null), `review_requested` (from you),
    `needs_review`, `updated_at`.
  - each issue: `number`, `title`, `url`, `updated_at`.
- `clones` ([clone]): every local clone of the repo.

### `clones[]`

- `path`, `origin` (URL), `branch` (null if detached), `upstream`, `upstream_gone`,
  `default_branch`, `head` (short SHA), `changes` (tracked files changed),
  `untracked`, `busy` (`merge`, `rebase`, ... or null), `ahead`, `behind`,
  `unpushed` (commits on HEAD on no remote), `fork`, `can_push`. All describe the
  clone after the run.
- `status`: `up-to-date`, `updated`, `would-update` (dry run), `held-back`,
  `conflict`, `error` or `skipped`; `note` explains it.
- `actions`: each `cmd`, `ok` (true/false; null in a dry run or when skipped) and
  `note`.
- `saved` (null if nothing was saved): `branch`, `files`, `commit` (null in a dry run).
- `held_back`: each `path` and `reason`. `save_skipped`: reason or null.
- `resolved`: each `path` and `how` (`identical`, `whitespace`, `union`, `lockfile`;
  lockfiles add `regenerate`, the command). `conflicts`: each `path` and `how`
  (`manual`), plus `error` if a step failed. `backup_refs`: [string].
- `branches`: local branches worth a look, each `name`, `upstream`, `unpushed`,
  `age_days`, `gone`, `stale`.
- `stashes`: each `commit`, `ref` (`stash@{N}`), `message`, `created` (ISO 8601).
- `worktrees`: [string] paths of linked worktrees.

## Checks

Run these before opening a PR. CI runs the same ones.

```sh
uv run ruff format && uv run ruff check && uv run pytest && uv audit --preview-features audit-command
shellcheck scripts/*.sh
sqlite3 -bail :memory: ".read sqlite/schema.sql"   # the app opens ${APP_DATA:-.}/app.db
```

The owner (`benpshore`) may push their own work directly to `main` without a PR or passing CI. CI still runs after a push; releases retain their CI gates. Force-push, deletion, and merge commits remain blocked for everyone.

Other accounts must use a squash PR with passing, up-to-date `ci`. Agents must use PRs and CI even when authenticated as the owner (see `AGENTS.md`). GitHub checks the pushing account, so enforcing a human/agent boundary requires separate credentials.
