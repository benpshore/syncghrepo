"""Local clones: find them and take a read-only snapshot of each."""

import os
import re
import time
from pathlib import Path

from .run import Git, Runner

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
PROTECTED = frozenset({"main", "master"})


def github_name(url: str | None) -> str | None:
    """owner/name for a GitHub remote URL (https, ssh or scp-style), else None."""
    match = GITHUB_URL.search(url.strip()) if url else None
    return f"{match[1]}/{match[2]}" if match else None


def find_clones(roots: list[Path], max_depth: int = 3) -> list[Path]:
    """Git clones under roots (a root may itself be a clone), at most max_depth down.

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


def ghq_clones(runner: Runner) -> tuple[list[Path] | None, str | None]:
    """Clones ghq manages (ghq list -p), as (paths, error); (None, None) without ghq.

    Only lists: syncghrepo never changes ghq.root, runs ghq get or moves a clone.
    """
    result = runner(["ghq", "list", "-p"])
    if result.code == 127:  # not installed
        return None, None
    if not result.ok:
        return None, f"ghq list -p failed: {result.last_line()}"
    listed = [Path(line.strip()) for line in result.out.splitlines() if line.strip()]
    return find_clones(listed, 0), None


def short_path(path: str | Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if text == home or text.startswith(home + os.sep) else text


def busy(git: Git) -> str | None:
    """The git operation in progress (merge, rebase, ...), if any."""
    git_dir = git.out("rev-parse", "--absolute-git-dir")
    return next(
        (op for name, op in IN_PROGRESS.items() if git_dir and Path(git_dir, name).exists()), None
    )


def is_protected(branch: str | None, default: str | None) -> bool:
    """Work is never committed or pushed to the default branch (or main/master)."""
    return branch is not None and (branch == default or branch in PROTECTED)


def inspect(runner: Runner, path: Path, *, stale_days: int = 30, now: float | None = None) -> dict:
    """Read-only snapshot of a clone. Uses remote-tracking refs as of the last fetch."""
    now = time.time() if now is None else now
    git = Git(runner, path)

    branch = git.out("symbolic-ref", "--quiet", "--short", "HEAD")
    upstream = (
        git.out("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        if branch
        else None
    )
    # A branch configured to track a remote branch that no longer exists (merged and deleted).
    upstream_gone = bool(branch and not upstream and git.out("config", f"branch.{branch}.merge"))
    default = git.out("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    default = default.split("/", 1)[1] if default and "/" in default else None

    changes = untracked = 0
    for line in (git.out("status", "--porcelain") or "").splitlines():
        if line.startswith("??"):
            untracked += 1
        elif line.strip():
            changes += 1

    ahead = behind = None
    if upstream:
        counts = git.out("rev-list", "--left-right", "--count", "HEAD...@{upstream}")
        if counts:
            ahead, behind = (int(n) for n in counts.split())
    unpushed = git.out("rev-list", "--count", "HEAD", "--not", "--remotes") if branch else None

    branches = []
    for line in (
        git.out("for-each-ref", f"--format={BRANCH_FORMAT}", "refs/heads") or ""
    ).splitlines():
        name, branch_upstream, track, stamp = [*line.split("\t"), "", "", ""][:4]
        count = git.out("rev-list", "--count", f"refs/heads/{name}", "--not", "--remotes")
        age = int((now - int(stamp)) // 86400) if stamp.isdigit() else 0
        info = {
            "name": name,
            "upstream": branch_upstream or None,
            "unpushed": int(count) if count and count.isdigit() else 0,
            "age_days": age,
            "gone": track == "gone",
            "stale": name not in (default, branch) and age >= stale_days,
        }
        if info["unpushed"] or info["gone"] or info["stale"]:
            branches.append(info)

    stash_lines = git.out("stash", "list", "--format=%H%x09%gd%x09%gs%x09%cI") or ""
    stashes = []
    for line in stash_lines.splitlines():
        commit, ref, message, created = [*line.split("\t", 3), "", "", ""][:4]
        stashes.append({"commit": commit, "ref": ref, "message": message, "created": created})
    worktree_lines = (git.out("worktree", "list", "--porcelain") or "").splitlines()
    worktrees = [line[9:] for line in worktree_lines if line.startswith("worktree ")][1:]

    return {
        "path": str(path),
        "origin": git.out("remote", "get-url", "origin"),
        "branch": branch,
        "upstream": upstream,
        "upstream_gone": upstream_gone,
        "default_branch": default,
        "head": git.out("rev-parse", "--short", "HEAD"),
        "changes": changes,
        "untracked": untracked,
        "busy": busy(git),
        "ahead": ahead,
        "behind": behind,
        "unpushed": int(unpushed) if unpushed and unpushed.isdigit() else 0,
        "branches": branches,
        "stashes": stashes,
        "worktrees": worktrees,
    }
