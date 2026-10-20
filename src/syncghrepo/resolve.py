"""Conflict auto-cleaning for `git pull --rebase`.

Trivial conflicts are resolved; anything else makes the whole rebase abort, so the
branch is back exactly where it was (and a backup ref was made before it started).

Trivial means, per file:
  identical   both sides made the same change
  whitespace  the sides differ only in trailing whitespace, line endings or trailing
              blank lines (indentation is meaningful, so it is never ignored); keeps mine
  union       go.sum: the union of both sides' lines
  lockfile    take upstream's lockfile, then regenerate it with the package manager

In a rebase, stage 2 is upstream (the commits being rebased onto) and stage 3 is the
local commit being replayed ("mine").
"""

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .local import busy
from .run import Git, Runner

LOCKFILES = {
    "uv.lock": ["uv", "lock"],
    "Cargo.lock": ["cargo", "update", "--workspace"],
    "package-lock.json": [
        "npm",
        "install",
        "--package-lock-only",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
    ],
    "pnpm-lock.yaml": ["pnpm", "install", "--lockfile-only", "--ignore-scripts"],
    "Gemfile.lock": ["bundle", "lock"],
}
UNION = frozenset({"go.sum"})
SCRATCH_IDENTITY = {
    "GIT_AUTHOR_NAME": "syncghrepo",
    "GIT_AUTHOR_EMAIL": "syncghrepo@localhost",
    "GIT_COMMITTER_NAME": "syncghrepo",
    "GIT_COMMITTER_EMAIL": "syncghrepo@localhost",
}


def normalize(data: bytes) -> bytes:
    return b"\n".join(line.rstrip() for line in data.splitlines()).rstrip(b"\n")


def classify(path: str, upstream: bytes | None, mine: bytes | None) -> dict | None:
    """How to resolve one conflicted file, or None if a person has to look at it.

    Returns {"how", "content", "regenerate"}; regenerate is a command or None.
    """
    if upstream is None or mine is None:
        return None  # one side deleted or renamed it
    if upstream == mine:
        return {"how": "identical", "content": mine, "regenerate": None}
    name = PurePosixPath(path).name
    if name in UNION:
        lines = sorted(set(upstream.splitlines()) | set(mine.splitlines()))
        return {"how": "union", "content": b"\n".join(lines) + b"\n", "regenerate": None}
    if name in LOCKFILES:
        return {"how": "lockfile", "content": upstream, "regenerate": LOCKFILES[name]}
    if normalize(upstream) == normalize(mine):
        return {"how": "whitespace", "content": mine, "regenerate": None}
    return None


def staged_versions(git: Git, listing: str, env=None) -> dict[str, dict[int, bytes | None]]:
    """{path: {1: base, 2: upstream, 3: mine}} from ls-files -u / merge-tree entries."""
    found: dict[str, dict[int, bytes | None]] = {}
    for entry in listing.split("\0"):
        if "\t" not in entry:
            continue
        meta, path = entry.split("\t", 1)
        parts = meta.split()
        if len(parts) != 3:
            continue
        mode, oid, stage = parts
        blob = git.runner(["git", "cat-file", "blob", oid], git.where, env or {})
        data = blob.out.encode("utf-8", "surrogateescape") if blob.ok else None
        if mode.startswith("12") or mode.startswith("16"):
            data = None  # symlinks and submodules are never auto-resolved
        found.setdefault(path, {1: None, 2: None, 3: None})[int(stage)] = data
    return found


def describe(path: str, choice: dict | None) -> dict:
    if choice is None:
        return {"path": path, "how": "manual"}
    detail = {"how": choice["how"], "path": path}
    if choice["regenerate"]:
        detail["regenerate"] = " ".join(choice["regenerate"])
    return detail


def plan_files(versions: dict) -> tuple[list[dict], list[dict]]:
    """Split conflicted files into (resolved, unresolved) descriptions."""
    resolved, unresolved = [], []
    for path, stages in sorted(versions.items()):
        choice = classify(path, stages[2], stages[3])
        if choice and choice["regenerate"] and not shutil.which(choice["regenerate"][0]):
            choice = None
        (resolved if choice else unresolved).append(describe(path, choice))
    return resolved, unresolved


def finish_rebase(git: Git, runner: Runner, root: Path, limit: int = 200) -> tuple[list, list]:
    """Resolve trivial conflicts and continue until the rebase finishes.

    Returns (resolved, unresolved). If unresolved is not empty, the rebase is still
    in progress and the caller must abort it.
    """
    resolved: list[dict] = []
    for _ in range(limit):
        if busy(git) != "rebase":
            return resolved, []
        listing = git("ls-files", "-u", "-z")
        versions = staged_versions(git, listing.out) if listing.ok else {}
        step_unresolved = []
        regenerate: dict[tuple[str, ...], list[str]] = {}
        choices = {}
        for path, stages in sorted(versions.items()):
            choice = classify(path, stages[2], stages[3])
            if choice is None:
                step_unresolved.append({"path": path, "how": "manual"})
            choices[path] = choice
        if step_unresolved:
            return resolved, step_unresolved
        for path, choice in choices.items():
            target = root / path
            target.write_bytes(choice["content"])
            if not git("add", "--", path).ok:
                return resolved, [{"path": path, "how": "manual", "error": "git add failed"}]
            if choice["regenerate"]:
                regenerate.setdefault(tuple(choice["regenerate"]), []).append(path)
            resolved.append(describe(path, choice))
        for cmd, paths in regenerate.items():
            for path in paths:
                folder = str((root / path).parent)
                result = runner(list(cmd), folder)
                if not result.ok:
                    note = f"{' '.join(cmd)} failed: {result.last_line()}"
                    return resolved, [{"path": path, "how": "manual", "error": note}]
                git("add", "--", path)
        result = git("rebase", "--continue")
        if not result.ok and busy(git) == "rebase" and not versions:
            # Nothing was conflicted, yet the rebase stopped: give up rather than guess.
            return resolved, [{"path": "", "how": "manual", "error": result.last_line()}]
    return resolved, [{"path": "", "how": "manual", "error": "rebase did not finish"}]


@contextmanager
def scratch(git: Git) -> Iterator[dict[str, str]]:
    """An environment where git writes objects and the index to a temp dir.

    The clone's own objects stay readable, so dry runs can build commits and run
    merge-tree without writing anything to the clone.
    """
    common = git.out("rev-parse", "--path-format=absolute", "--git-common-dir")
    with tempfile.TemporaryDirectory(prefix="syncghrepo-") as tmp:
        objects = Path(tmp, "objects")
        for sub in ("pack", "info"):
            (objects / sub).mkdir(parents=True)
        alternates = [str(Path(common or ".", "objects"))]
        yield {
            "GIT_OBJECT_DIRECTORY": str(objects),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.pathsep.join(alternates),
            "GIT_INDEX_FILE": str(Path(tmp, "index")),
            **SCRATCH_IDENTITY,
        }


def snapshot(git: Git, files: list[str], env: dict) -> str | None:
    """A commit of HEAD plus these working-tree files, written only to the scratch env."""
    if not git("read-tree", "HEAD", env=env).ok:
        return None
    with tempfile.NamedTemporaryFile("wb", delete=False) as spec:
        spec.write(b"\0".join(p.encode("utf-8", "surrogateescape") for p in files))
    try:
        added = git(
            "add", "--all", f"--pathspec-from-file={spec.name}", "--pathspec-file-nul", env=env
        )
    finally:
        os.unlink(spec.name)
    tree = git.out("write-tree", env=env) if added.ok else None
    return git.out("commit-tree", tree, "-p", "HEAD", "-m", "wip", env=env) if tree else None


def predict(git: Git, upstream: str, mine: str, env: dict) -> tuple[list, list] | None:
    """Predicted (resolved, unresolved) conflicts of rebasing mine onto upstream.

    Uses one three-way merge (git merge-tree) in the scratch env; a rebase replays
    commit by commit, so this is a close prediction, not a guarantee.
    """
    result = git("merge-tree", "--write-tree", "-z", "--no-messages", upstream, mine, env=env)
    if result.code not in (0, 1):
        return None
    if result.code == 0:
        return [], []
    listing = result.out.split("\0", 1)[1] if "\0" in result.out else ""
    return plan_files(staged_versions(git, listing, env))
