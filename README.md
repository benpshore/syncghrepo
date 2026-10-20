# syncghrepo

Keep all work in your local clones saved and in sync, and list what is still
open, for you or for an agent to triage. One plain CLI with exit codes and
`--json`, so a Makefile can call it. Standard library plus existing tools it
drives instead of reimplementing them: `git`, `gh`,
[git-sync](https://github.com/simonthum/git-sync) (saving and syncing),
[ghq](https://github.com/x-motemen/ghq) (finding clones) and
[gh-dash](https://github.com/dlvhdr/gh-dash) (reviewing PRs and issues). No other
dependencies, and no code shared with makeghrepo or reconcile-tasks.

Pipeline: `makeghrepo` (create repos) → **`syncghrepo`** (save, sync, list open
work) → `reconcile-tasks` (turn that work into tasks). The only contract between
syncghrepo and reconcile-tasks is the versioned `--json` report below.

**No AI.** syncghrepo makes no AI/LLM calls and has no network code of its own:
only `git`, `git-sync` (a bash script that runs git), `gh` (`api`, `search prs`,
`repo sync`), `ghq list`, the lockfile tools listed below, and the Python standard library. `run.ALLOWED` enforces this: any other command
(`gh models`, `gh copilot`, `curl`, an AI CLI, ...) is refused, and tests check
that the code imports only the standard library and has no dependencies.

**Three commands, one flag.**

```sh
syncghrepo           # status: what is open and what sync would do; writes nothing
syncghrepo sync      # save and sync every clone, then the same report
syncghrepo dash      # write the gh-dash config, then print the gh dash command
```

`--json` (any command) prints JSON instead of text. Nothing else is a flag; see
[Use](#use) for the two environment variables.

## What it does, per clone

The saving and syncing is done by [git-sync](https://github.com/simonthum/git-sync)
(Simon Thum, CC0): it auto-commits, fetches, fast-forwards or rebases, and pushes
the current branch, and stops when anything is not trivial. syncghrepo does not
reimplement that. It decides where git-sync may run, prepares the branch, calls
it and reports what happened:

1. **Secret check.** If any changed file has a secret-looking name (`.env`,
   `.envrc`, keys, `credentials*`, databases, ...), contains a secret-looking
   string (GitHub/AWS/Slack/OpenAI/Google tokens, private keys) or is over 50 MB,
   the whole clone is skipped and the files are reported. Over 2,000 changed files
   (a missing `.gitignore`?) is skipped too.
2. **Never the default branch.** git-sync never runs on the default branch (or
   `main`/`master`). Uncommitted work found there moves to a new
   `wip/<YYYYMMDD>-<host>` branch (`-2`, `-3`, ... if taken), which is pushed so it
   has the same-named remote branch git-sync needs. The default branch itself is
   only fast-forwarded (`git merge --ff-only`, or `git fetch . origin/main:main`
   when it is not checked out); local commits on it are reported, never pushed.
3. **Opt in.** Only for a branch with something to sync, and only if GitHub says
   you can push: `git config --bool branch.<b>.sync true` and
   `branch.<b>.syncNewFiles true` (so new, non-ignored files are committed;
   git-sync uses `git add -A`, which never adds ignored files). A local-only branch
   is first pushed with `-u`. Branches whose upstream was deleted (merged PRs) are
   not recreated. Clean, in-sync clones are not touched, not even their config.
4. **git-sync check first.** `git-sync -s -n check` (read-only) runs before
   anything is changed, so a clone it would refuse (staged changes, a merge in
   progress, ...) is left untouched and its message reported.
5. **Backup, then git-sync.** If the branch is behind its upstream and has local
   work, a backup ref of HEAD plus any uncommitted files is made first:
   `refs/syncghrepo/backup/<YYYYMMDDTHHMMSS>` (reused when a backup of the same
   tree exists). Then `git-sync` runs. It commits ("changes from <host> on
   <date>"), fetches, fast-forwards or rebases, and pushes. It never forces.
6. **Conflicts.** git-sync leaves a conflicted rebase in progress. syncghrepo then
   resolves trivial conflicts:
   - *identical*: both sides made the same change;
   - *whitespace*: the sides differ only in trailing whitespace, line endings or
     trailing blank lines; mine is kept. Indentation is never ignored;
   - *lockfiles*: take upstream's, then regenerate (`uv lock`, `cargo update
     --workspace`, `npm install --package-lock-only --ignore-scripts`, `pnpm install
     --lockfile-only --ignore-scripts`, `bundle lock`); `go.sum` takes the union.

   If they are all trivial, it reruns git-sync to push. Otherwise it runs
   `git rebase --abort`: the branch is back exactly as it was (with git-sync's WIP
   commit), the backup ref is kept, and the conflict is reported.
7. **Forks** get `gh repo sync owner/name` first (fast-forward only).
8. **Report** open PRs (yours, drafts, needing review, review requested), failing
   CI on the default branch, issues assigned to you, unpushed, stale and
   deleted-upstream branches, stashes and linked worktrees. Repos come from `gh`:
   yours, your organisations', collaborations, and repos you opened PRs in.

**Finding clones.** Clones come from [ghq](https://github.com/x-motemen/ghq)
(`ghq list -p`) plus `~/code` (searched 3 levels deep), deduplicated; if
`$SYNCGHREPO_ROOTS` is set, from exactly those folders instead. syncghrepo only lists: it never changes `ghq.root`,
runs `ghq get` or moves a clone. Without ghq, the roots alone are used.

**Review UI.** `syncghrepo dash` writes a
[gh-dash](https://github.com/dlvhdr/gh-dash) config with sections for your open
PRs, your drafts, review requests and issues assigned to you, all scoped to the
discovered repos (`repo:owner/name ...`), plus `repoPaths` so gh dash can check a PR
out in your clone. It goes to `~/.config/gh-dash/config.yml` (or
`$XDG_CONFIG_HOME/gh-dash/`) only if no file is there; otherwise next to it as
`config.syncghrepo.yml`, so your own config is never touched (then:
`gh dash --config ~/.config/gh-dash/config.syncghrepo.yml`), and prints the
`gh dash` command to run. It only discovers repos and clones (no syncing), and
never launches `gh dash`.

Clones in the middle of a merge or rebase, with a detached HEAD, or with no
commits yet, are left alone and reported. Linked worktrees are listed but not
synced. Nothing is ever reset, cleaned, force-pushed or discarded.

git-sync is found via `$SYNCGHREPO_GIT_SYNC`, `PATH`, then
`~/.local/bin/git-sync`. Without it, clones with work are reported, not synced,
and the run exits 2. To stop syncing a branch: `git config --unset branch.<b>.sync`.

## Use

```sh
uv tool install git+https://github.com/benpshore/syncghrepo
syncghrepo                     # status: exactly what sync would commit, push, resolve
syncghrepo sync                # do it
syncghrepo --json              # full report for agents (works with sync too)
syncghrepo dash && gh dash     # review your PRs and issues
SYNCGHREPO_ROOTS=~/code/GitHub/proj syncghrepo sync   # one clone (colon-separated list)
```

Environment: `SYNCGHREPO_ROOTS` (folders of clones, colon-separated, instead of
ghq + `~/code`); `SYNCGHREPO_GIT_SYNC` (the git-sync to run). Fixed settings: roots
are searched 3 levels deep, a branch is stale after 30 days without commits, each
command times out after 120 s, and 8 clones are processed in parallel.

From a checkout: `uv run syncghrepo`. In a Makefile:

```make
status: ; syncghrepo
sync:   ; syncghrepo sync
dash:   ; syncghrepo dash
plan:   ; syncghrepo --json > sync-plan.json
```

`syncghrepo` (status) writes nothing: no commits, refs, config, index or
working-tree changes, no gh-dash config, and no fetch, so it works from the
remote-tracking refs of the last fetch (`sync` fetches). git-sync runs only in its
read-only `check` mode. Conflicts are predicted with `git merge-tree` in a scratch
object store outside the clone; a rebase replays commit by commit, so the
prediction is close but not a guarantee.

**Exit status:** 0 when all is fine (open PRs are reported, not counted); 1 when
something needs attention: uncommitted work, a conflict, a held-back file, a clone
mid-merge/rebase or detached, or failing CI on a default branch; 2 for an error (a
step failed, `gh` or git-sync unusable, bad arguments).

The JSON report is documented below.

To recover from a backup ref: `git switch -c rescue refs/syncghrepo/backup/<YYYYMMDDTHHMMSS>`
(list them with `git for-each-ref refs/syncghrepo`).

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
| `dry_run` | bool | true for `syncghrepo` (status), false for `syncghrepo sync` |
| `roots` | [string] | folders searched for clones |
| `ghq_clones` | int or null | clones found with `ghq list -p`; null when ghq was not used (`$SYNCGHREPO_ROOTS` set) or is not installed |
| `gh_dash` | object or null | the gh-dash config: `path`, `written` (always false here: only `syncghrepo dash` writes it), `existing_config` (a `config.yml` was already there, so `path` is `config.syncghrepo.yml`); null without GitHub |
| `summary` | object | counts, below |
| `items` | [item] | unresolved work, below |
| `repos` | [repo] | full detail per repo, below |
| `errors` | [string] | run-level problems (e.g. `gh` unusable); non-empty means exit 2 |

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
| `held_back` | `path:file` | secret-looking or oversized file; the whole clone was skipped |
| `not_pushed` | `path:branch` | work not synced or pushed: commits on the default branch, a diverged default branch, deleted upstream, no push access, or a branch git-sync cannot handle |
| `unpushed_branch` | `path:branch` | another local branch with commits on no remote |
| `gone_branch` | `path:branch` | local branch whose upstream was deleted (merged?) |
| `stale_branch` | `path:branch` | no commits for 30 days |
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
- `status`: `up-to-date`, `updated`, `would-update` (status), `held-back`,
  `conflict`, `error` or `skipped`; `note` explains it.
- `actions`: each `cmd`, `ok` (true/false; null in status or when skipped) and
  `note`.
- `git_sync` (null if git-sync did not run, or ran only its check and passed): the
  last git-sync run's `code` (exit status: 0 in sync, 1 refused or rebase stopped,
  2 unsafe state, 3 fetch/push failed) and `log` (its last output lines).
- `saved` (null if nothing was saved): `branch`, `files`, `commit` (null in status).
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
