"""Clone your repos that have no clone yet, into ~/code/GitHub/<name>.

A repo is cloned only if it is yours (owned by the GitHub user running syncghrepo)
and no clone of it exists anywhere under the roots: top-level clones, ghq's clones,
nested clones and submodules all count, matched by their origin URL. Archived repos
are cloned too (and reported as archived). An existing folder is never overwritten
or moved: if ~/code/GitHub/<name> exists and is not a clone of that repo, the repo
is skipped and reported. Cloning is done with `gh repo clone owner/name <path>`.
"""

from pathlib import Path

from . import local
from .run import Git, Runner


def clone_dir() -> Path:
    return Path.home() / "code" / "GitHub"


def occupied(runner: Runner, target: Path, name: str) -> str:
    """Why target cannot take a clone of name."""
    where = local.short_path(target)
    if (target / ".git").exists():
        other = local.github_name(Git(runner, target).out("remote", "get-url", "origin"))
        if other and other.lower() == name.lower():
            return f"{where} is a linked worktree or broken clone of {name}; left as is"
        return f"{where} exists and is a clone of {other or 'another repo'}; left as is"
    if target.is_symlink() or not target.is_dir():
        return f"{where} exists and is not a folder; left as is"
    return f"{where} exists and is not a clone of {name}; left as is"


def plan(runner: Runner, repos: dict, me: str, present: set[str], base: Path) -> list[dict]:
    """One record per repo of yours with no clone: would-clone, or skipped."""
    records = []
    for key, entry in sorted(repos.items()):
        owner, _, short = entry["name"].partition("/")
        if not entry["github"] or key in present or owner.lower() != me.lower() or not short:
            continue
        target = base / short
        record = {
            "name": entry["name"],
            "path": str(target),
            "archived": bool(entry["archived"]),
            "fork": bool(entry["fork"]),
            "status": "would-clone",
            "note": "archived" if entry["archived"] else "",
        }
        if target.exists() or target.is_symlink():
            record.update(status="skipped", note=occupied(runner, target, entry["name"]))
        records.append(record)
    return records


def clone(runner: Runner, record: dict) -> None:
    """gh repo clone owner/name path; records cloned or failed."""
    target = Path(record["path"])
    if target.exists() or target.is_symlink():  # appeared since the plan
        record.update(status="skipped", note=occupied(runner, target, record["name"]))
        return
    result = runner(["gh", "repo", "clone", record["name"], str(target)])
    if result.ok:
        record.update(status="cloned", note="archived" if record["archived"] else "")
    else:
        record.update(status="failed", note=result.last_line())
