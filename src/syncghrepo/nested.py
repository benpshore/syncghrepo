"""Submodules of a clone: bring them to the recorded commits after a sync.

`git submodule update --init --recursive` runs only in clones with a .gitmodules,
only when a submodule is missing or not at the commit the clone records, and never
when it could move local work: a submodule checked out on a branch, with
uncommitted changes, with commits on no remote, or in a merge conflict is left as
it is (the update is skipped and the reason noted). Such a submodule is synced on
its own, like any nested repo (see local.nested_repos).
"""

from pathlib import Path

from . import local
from .run import Git, Runner

UPDATE = "git submodule update --init --recursive"


def blockers(runner: Runner, path: Path, entries: list[tuple[str, str]]) -> list[str]:
    """Submodules an update could move off local work, with the reason."""
    found = []
    for state, rel in entries:
        if state == "U":
            found.append(f"{rel} (merge conflict)")
            continue
        if state == "-":
            continue
        git = Git(runner, path / rel)
        branch_result = git("symbolic-ref", "--quiet", "--short", "HEAD")
        branch = branch_result.out.strip() if branch_result.ok else None
        status = git("status", "--porcelain")
        unpushed = git.out("rev-list", "--count", "HEAD", "--not", "--remotes")
        if branch_result.code not in (0, 1) or not status.ok:
            found.append(f"{rel} (could not inspect local work)")
        elif local.busy(git):
            found.append(f"{rel} (git operation in progress)")
        elif branch:
            found.append(f"{rel} (on branch {branch})")
        elif status.out.strip():
            found.append(f"{rel} (uncommitted changes)")
        elif unpushed != "0":
            found.append(f"{rel} (commits on no remote)")
    return found


def update(runner: Runner, clone: dict, *, dry_run: bool) -> None:
    """Init and update the submodules of a processed clone (adds to its actions)."""
    path = Path(clone["path"])
    if not (path / ".gitmodules").is_file() or clone["busy"] or clone["status"] == "error":
        return
    entries = local.submodule_status(runner, path)
    if entries is None:
        clone["actions"].append(
            {"cmd": "git submodule status --recursive", "ok": False, "note": "failed"}
        )
        clone.update(status="error", note="git submodule status --recursive failed")
        return
    missing = sum(state == "-" for state, _ in entries)
    moved = sum(state == "+" for state, _ in entries)
    if not (missing or moved or any(state == "U" for state, _ in entries)):
        return
    stuck = blockers(runner, path, entries)
    if stuck:
        note = "not run, it could move local work: " + ", ".join(stuck)
        clone["actions"].append({"cmd": UPDATE, "ok": None, "note": note})
        return
    note = f"init {missing}, update {moved} submodule(s)"
    if dry_run:
        clone["actions"].append({"cmd": UPDATE, "ok": None, "note": note})
        if clone["status"] == "up-to-date":
            clone["status"] = "would-update"
        return
    result = Git(runner, path)("submodule", "update", "--init", "--recursive")
    if result.ok:
        clone["actions"].append({"cmd": UPDATE, "ok": True, "note": note})
        if clone["status"] == "up-to-date":
            clone["status"] = "updated"
    else:
        clone["actions"].append({"cmd": UPDATE, "ok": False, "note": result.last_line()})
        clone.update(status="error", note=f"{UPDATE} failed: {result.last_line()}")


def settle_submodule(runner: Runner, clone: dict) -> None:
    """A submodule on a detached HEAD is normal (git submodule leaves it so): with
    nothing unsaved it is up to date, not skipped."""
    if clone["status"] != "skipped" or clone["branch"] or clone["busy"] or not clone["head"]:
        return
    if clone["changes"] or clone["untracked"]:
        return
    unpushed = Git(runner, clone["path"]).out("rev-list", "--count", "HEAD", "--not", "--remotes")
    if unpushed == "0":
        clone.update(
            status="up-to-date", note="submodule checkout (detached HEAD), nothing unsaved"
        )
