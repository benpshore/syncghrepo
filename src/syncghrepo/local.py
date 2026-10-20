"""Local clones: find them, inspect them, and fast-forward them safely.

Safety rules: never force, never stash, never reset or discard. A clone is only
pulled (`git pull --ff-only`) when it is on a branch with an upstream, has no
uncommitted changes to tracked files, has no merge/rebase in progress, and has
not diverged from its upstream. Everything else is reported, not touched.
"""

import os
import re
import time
from pathlib import Path

from .run import Runner

SKIP_DIRS = frozenset({"node_modules", "target", "build", "dist", "DerivedData", "vendor"})
IN_PROGRESS = {
    "MERGE_HEAD": "merge",
    "rebase-merge": "rebase",
    "rebase-apply": "rebase",
    "CHERRY_PICK_HEAD": "cherry-pick",
    "REVERT_HEAD": "revert",
    "BISECT_LOG": "bisect",
}
GITHUB_URL = re.compile(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/*$", re.IGNORECASE)
BRANCH_FORMAT = (
    "%(refname:short)%09%(upstream:short)%09%(upstream:track,nobracket)%09%(committerdate:unix)"
)


def github_name(url: str | None) -> str | None:
    """owner/name for a GitHub remote URL (https, ssh or scp-style), else None."""
    match = GITHUB_URL.search(url.strip()) if url else None
    return f"{match[1]}/{match[2]}" if match else None


def find_clones(roots: list[Path], max_depth: int = 3) -> list[Path]:
    """Git clones under roots, at most max_depth levels down.

    Linked worktrees and submodules (where .git is a file) are skipped; they are
    reported through their main clone. Hidden and build directories are not searched.
    """
    found: set[Path] = set()
    for root in roots:
        stack = [(root.expanduser(), 0)]
        while stack:
            folder, depth = stack.pop()
            dot_git = folder / ".git"
            if dot_git.is_dir():
                found.add(folder.resolve())
                continue
            if dot_git.exists() or depth >= max_depth:
                continue
            try:
                children = [
                    child
                    for child in folder.iterdir()
                    if child.is_dir()
                    and not child.is_symlink()
                    and not child.name.startswith(".")
                    and child.name not in SKIP_DIRS
                ]
            except OSError:
                continue
            stack.extend((child, depth + 1) for child in children)
    return sorted(found)


def short_path(path: str | Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if text == home or text.startswith(home + os.sep) else text


def inspect(runner: Runner, path: Path, *, stale_days: int = 30, now: float | None = None) -> dict:
    """Read-only snapshot of a clone. Uses remote-tracking refs as of the last fetch."""
    now = time.time() if now is None else now

    def out(*args: str) -> str | None:
        result = runner(["git", "--no-optional-locks", *args], str(path))
        return result.out.strip() if result.ok else None

    branch = out("symbolic-ref", "--quiet", "--short", "HEAD")
    upstream = (
        out("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}") if branch else None
    )
    default = out("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    default = default.split("/", 1)[1] if default and "/" in default else None

    changes = untracked = 0
    for line in (out("status", "--porcelain") or "").splitlines():
        if line.startswith("??"):
            untracked += 1
        elif line.strip():
            changes += 1

    git_dir = out("rev-parse", "--absolute-git-dir")
    busy = next(
        (op for name, op in IN_PROGRESS.items() if git_dir and Path(git_dir, name).exists()), None
    )

    ahead = behind = None
    if upstream:
        counts = out("rev-list", "--left-right", "--count", "HEAD...@{upstream}")
        if counts:
            ahead, behind = (int(n) for n in counts.split())

    branches = []
    for line in (out("for-each-ref", f"--format={BRANCH_FORMAT}", "refs/heads") or "").splitlines():
        name, branch_upstream, track, stamp = [*line.split("\t"), "", "", ""][:4]
        unpushed = out("rev-list", "--count", f"refs/heads/{name}", "--not", "--remotes")
        age = int((now - int(stamp)) // 86400) if stamp.isdigit() else 0
        info = {
            "name": name,
            "upstream": branch_upstream or None,
            "unpushed": int(unpushed) if unpushed and unpushed.isdigit() else 0,
            "age_days": age,
            "gone": track == "gone",
            "stale": name not in (default, branch) and age >= stale_days,
        }
        if info["unpushed"] or info["gone"] or info["stale"]:
            branches.append(info)

    stash_lines = out("stash", "list", "--format=%gd: %gs (%cr)") or ""
    worktree_lines = (out("worktree", "list", "--porcelain") or "").splitlines()
    worktrees = [line[9:] for line in worktree_lines if line.startswith("worktree ")][1:]

    return {
        "path": str(path),
        "origin": out("remote", "get-url", "origin"),
        "branch": branch,
        "upstream": upstream,
        "default_branch": default,
        "head": out("rev-parse", "--short", "HEAD"),
        "changes": changes,
        "untracked": untracked,
        "busy": busy,
        "ahead": ahead,
        "behind": behind,
        "branches": branches,
        "stashes": [line for line in stash_lines.splitlines() if line],
        "worktrees": worktrees,
    }


def plan(local: dict) -> tuple[str, str]:
    """(status, note) for a clone. Only status "pull" may be fast-forwarded."""
    if local["busy"]:
        return "busy", f"{local['busy']} in progress"
    if not local["branch"]:
        return "detached", "detached HEAD"
    if not local["upstream"]:
        return "no-upstream", f"{local['branch']} has no upstream (or it was deleted)"
    if local["changes"]:
        return "dirty", f"{local['changes']} uncommitted change(s) on {local['branch']}"
    ahead, behind = local["ahead"] or 0, local["behind"] or 0
    if ahead and behind:
        return "diverged", f"{ahead} ahead, {behind} behind {local['upstream']}"
    if behind:
        return "pull", f"{behind} behind {local['upstream']}"
    if ahead:
        return "ahead", f"{ahead} unpushed commit(s) on {local['branch']}"
    return "up-to-date", ""


def update(
    runner: Runner,
    path: Path,
    *,
    fork: str | None = None,
    dry_run: bool = True,
    stale_days: int = 30,
    now: float | None = None,
) -> dict:
    """Inspect a clone and, unless dry_run, sync and fast-forward it.

    fork is the GitHub owner/name when the clone's origin is a fork; it is synced
    from its parent first with `gh repo sync` (fast-forward only, never --force).
    """
    where = str(path)
    steps = ([f"gh repo sync {fork}"] if fork else []) + ["git fetch", "git pull --ff-only"]
    actions: list[dict] = []
    if not dry_run:
        if fork:
            result = runner(["gh", "repo", "sync", fork], where)
            actions.append({"cmd": steps[0], "ok": result.ok, "note": result.last_line()})
        result = runner(["git", "fetch", "--quiet"], where)
        actions.append({"cmd": "git fetch", "ok": result.ok, "note": result.last_line()})

    local = inspect(runner, path, stale_days=stale_days, now=now)
    status, note = plan(local)
    if status == "pull" and dry_run:
        status, note = "would-pull", f"{note} as of last fetch"
    elif status == "pull":
        result = runner(["git", "pull", "--ff-only", "--quiet"], where)
        actions.append({"cmd": "git pull --ff-only", "ok": result.ok, "note": result.last_line()})
        if result.ok:
            local = inspect(runner, path, stale_days=stale_days, now=now)
            status, note = "updated", f"fast-forwarded to {local['head']}"
        else:
            status, note = "error", f"git pull --ff-only: {result.last_line()}"
    elif dry_run and status == "up-to-date":
        note = "as of last fetch"

    if status in ("busy", "detached", "no-upstream", "dirty", "diverged"):
        steps = steps[:-1]
    local.update(
        status=status,
        note=note,
        fork=fork,
        actions=actions,
        would_run=steps if dry_run else [],
    )
    return local
