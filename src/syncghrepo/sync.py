"""One clone, start to finish: save, sync, clean conflicts, push.

Order: gh repo sync (forks) -> git fetch --prune -> auto-save WIP commit ->
git pull --rebase --autostash (trivial conflicts resolved, else aborted) ->
fast-forward the default branch if it is not checked out -> push.

Never loses work: a backup ref (refs/syncghrepo/backup/...) is made before every
rewrite, nothing is reset or discarded, and the only forced push is
--force-with-lease on wip/ branches. With dry_run nothing is written: every step
is listed as it would run, and conflicts are predicted in a scratch object store.
"""

import time
from datetime import datetime
from pathlib import Path

from . import local, resolve, save
from .run import Git, Result, Runner

BACKUP_PREFIX = "refs/syncghrepo/backup"
# These alone do not make a clone "updated".
ROUTINE = ("git push (skipped)", "git fetch --prune", "gh repo sync")


def process(
    runner: Runner,
    path: Path,
    *,
    can_push: bool = False,
    fork: str | None = None,
    dry_run: bool = True,
    fetch: bool = False,
    stale_days: int = 30,
    now: float | None = None,
) -> dict:
    """Process one clone. fetch makes a dry run fetch first (it only updates
    remote-tracking refs), so its plan reflects what is on the remote now."""
    now = time.time() if now is None else now
    moment = datetime.fromtimestamp(now).astimezone()
    git = Git(runner, path)
    actions: list[dict] = []
    report: dict = {
        "saved": None,
        "held_back": [],
        "save_skipped": None,
        "resolved": [],
        "conflicts": [],
        "backup_refs": [],
        "actions": actions,
    }

    def act(cmd: str, result: Result | None = None, note: str = "", ok: bool | None = None):
        if result is not None:
            ok = result.ok
            if not result.ok:
                note = f"{note}: {result.last_line()}" if note else result.last_line()
        actions.append({"cmd": cmd, "ok": ok, "note": note})

    def inspect() -> dict:
        return local.inspect(runner, path, stale_days=stale_days, now=now)

    def finish(status: str, note: str = "") -> dict:
        return {**inspect(), **report, "status": status, "note": note, "fork": fork,
                "can_push": can_push}  # fmt: skip

    info = inspect()
    if info["busy"]:
        return finish("skipped", f"{info['busy']} in progress; left alone")
    if not info["branch"]:
        return finish("skipped", "detached HEAD; left alone")
    if not info["head"]:
        return finish("skipped", f"no commits yet on {info['branch']}; left alone")

    # 1. Forks: bring the fork on GitHub up to date with its parent (never --force).
    if fork and can_push:
        cmd = f"gh repo sync {fork}"
        act(cmd) if dry_run else act(cmd, runner(["gh", "repo", "sync", fork], str(path)))

    # 2. Fetch. Without fetch, a dry run uses remote-tracking refs as of the last fetch.
    if not dry_run or fetch:
        act("git fetch --prune", git("fetch", "--quiet", "--prune"))
        info = inspect()
    default = info["default_branch"]
    branch = info["branch"]

    # 3. Auto-save uncommitted work.
    saving = save.plan(git, path, info, moment.strftime("%Y-%m-%d"))
    new_commits = 0
    if saving:
        report["held_back"] = saving["held_back"]
        report["save_skipped"] = saving["skipped"]
        if saving["files"] and not saving["skipped"]:
            target = saving["branch"]
            label = f"git commit {len(saving['files'])} file(s) on {target}"
            report["saved"] = {"branch": target, "files": saving["files"], "commit": None}
            if dry_run:
                if saving["new_branch"]:
                    act(f"git switch -c {target}", note=f"never commit to {branch}")
                act(label, note="WIP commit")
                branch, new_commits = target, 1
            else:
                result = save.commit(git, saving, moment.isoformat(timespec="seconds"))
                act(label, result, "WIP commit")
                if not result.ok:
                    return finish("error", "auto-save failed; nothing else done")
                report["saved"]["commit"] = git.out("rev-parse", "--short", "HEAD")
                info = inspect()
                branch = info["branch"]
    created = bool(saving and saving["new_branch"])

    # 4. Sync the current branch from its upstream.
    upstream = None if (dry_run and created) else info["upstream"]
    behind = info["behind"] or 0
    ahead = (info["ahead"] or 0) + new_commits
    if upstream and behind:
        pull = "git pull --rebase --autostash"
        note = f"{behind} new commit(s) from {upstream}"
        backup = f"{BACKUP_PREFIX}/{branch}/{moment.strftime('%Y%m%d-%H%M%S')}"
        if dry_run:
            if ahead:
                act(f"git update-ref {backup} HEAD", note="backup before rebase")
            with resolve.scratch(git) as env:
                mine = resolve.snapshot(git, saving["files"], env) if new_commits else "HEAD"
                predicted = resolve.predict(git, upstream, mine or "HEAD", env)
            if predicted is None:
                note += "; conflicts could not be predicted"
            else:
                report["resolved"], report["conflicts"] = predicted
                if report["conflicts"]:
                    files = ", ".join(c["path"] for c in report["conflicts"])
                    note += f"; would abort on conflicts in {files} and leave the branch as is"
                elif report["resolved"]:
                    note += f"; would auto-resolve {len(report['resolved'])} conflict(s)"
            act(pull, note=note)
        else:
            existing = git.out(
                "for-each-ref", "--points-at", "HEAD", "--format=%(refname)",
                f"{BACKUP_PREFIX}/{branch}/",
            )  # fmt: skip
            if ahead and existing:  # an earlier run already backed up this exact commit
                report["backup_refs"].append(existing.splitlines()[0])
            elif ahead:
                made = git("update-ref", backup, "HEAD", "")
                act(f"git update-ref {backup} HEAD", made, "backup before rebase")
                if not made.ok:
                    return finish("error", "could not create a backup ref; did not rebase")
                report["backup_refs"].append(backup)
            stashes = len(info["stashes"])
            result = git("pull", "--rebase", "--autostash", "--quiet")
            if result.ok:
                act(pull, result, note)
            elif local.busy(git) == "rebase":
                report["resolved"], report["conflicts"] = resolve.finish_rebase(git, runner, path)
                if report["conflicts"]:
                    files = ", ".join(c["path"] or "?" for c in report["conflicts"])
                    act(
                        pull, ok=False, note=f"conflicts in {files}: aborted, branch left as it was"
                    )
                    act("git rebase --abort", git("rebase", "--abort"))
                else:
                    act(pull, ok=True, note=f"{note}; auto-resolved {len(report['resolved'])}")
            else:
                act(pull, result)
            info = inspect()
            if report["conflicts"] or local.busy(git) or not actions[-1]["ok"]:
                return done(finish, report, actions, dry_run)  # never push after a failed pull
            if len(info["stashes"]) > stashes:
                act("autostash", ok=False, note="held-back changes did not reapply; see git stash")
            branch = info["branch"] or branch

    # 4b. Fast-forward the default branch when it is not checked out here.
    if default and branch != default and git("show-ref", "--quiet", "--verify",
                                              f"refs/heads/{default}").ok:  # fmt: skip
        tracking = f"refs/remotes/origin/{default}"
        lag = git.out("rev-list", "--count", f"refs/heads/{default}..{tracking}")
        if lag and lag != "0" and git("merge-base", "--is-ancestor", default, tracking).ok:
            cmd = f"git fetch . origin/{default}:{default}"
            note = f"fast-forward {default} by {lag}"
            if dry_run:
                act(cmd, note=note)
            else:  # refuses non-fast-forwards and branches checked out in any worktree
                act(cmd, git("fetch", "--quiet", ".", f"{tracking}:refs/heads/{default}"), note)

    # 5. Push the current branch: never the default branch, never a deleted upstream.
    if local.is_protected(branch, default):
        pending = ahead if upstream else (info["unpushed"] or 0)
        if pending:
            act("git push (skipped)", note=f"{pending} local commit(s) on {branch} are not pushed")
        return done(finish, report, actions, dry_run)
    if info["upstream_gone"] and not created:
        act("git push (skipped)", note="upstream branch was deleted (merged?); not recreating it")
        return done(finish, report, actions, dry_run)
    if not dry_run:
        info = inspect()
        upstream, ahead = info["upstream"], info["ahead"] or 0
    if upstream:
        remote = git.out("config", f"branch.{branch}.remote") or "origin"
        merge = git.out("config", f"branch.{branch}.merge") or f"refs/heads/{branch}"
        count, args = ahead, ["push", "--quiet", remote, f"HEAD:{merge}"]
    else:
        count = (info["unpushed"] or 0) + new_commits
        remote, merge = "origin", f"refs/heads/{branch}"
        args = ["push", "--quiet", "-u", "origin", f"HEAD:{merge}"]
    if count:
        label = f"git {' '.join(a for a in args if a != '--quiet')}"
        if not can_push:
            act("git push (skipped)", note=f"{count} commit(s) not pushed: no known push access")
        elif dry_run:
            act(label, note=f"{count} commit(s)")
        else:
            result = git(*args)
            if not result.ok and upstream and branch.startswith("wip/"):
                expected = git.out("rev-parse", "@{upstream}") or ""
                lease = f"--force-with-lease={merge}:{expected}"
                act(label, note=f"rejected ({result.last_line()}); retrying with a lease")
                args = ["push", "--quiet", lease, remote, f"HEAD:{merge}"]
                label = f"git push {lease} {remote} HEAD:{merge}"
                result = git(*args)
            act(label, result, f"{count} commit(s)")
    return done(finish, report, actions, dry_run)


def done(finish, report: dict, actions: list[dict], dry_run: bool) -> dict:
    if report["conflicts"]:
        if dry_run:
            return finish("conflict", "predicted conflicts; the pull would be aborted")
        return finish("conflict", "unresolved conflicts; rebase aborted, nothing lost")
    if any(action["ok"] is False for action in actions):
        return finish("error", next(a["note"] for a in actions if a["ok"] is False))
    if report["held_back"] or report["save_skipped"]:
        return finish("held-back", report["save_skipped"] or "some files were not saved")
    if any(not action["cmd"].startswith(ROUTINE) for action in actions):
        return finish("would-update" if dry_run else "updated")
    return finish("up-to-date")
