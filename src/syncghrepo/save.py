"""Auto-save: commit uncommitted work as a WIP commit, never on the default branch.

Never commits ignored files (git add skips them), secret-looking files, files that
contain secret-looking strings, or very large files; those are held back and reported.
"""

import re
import stat
import tempfile
from pathlib import Path

from .local import is_protected
from .run import Git, Result

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_FILES = 2000  # more than this usually means a missing .gitignore, not work
SECRET_NAME = re.compile(
    r"(^|/)(\.env(\.(?!example$|sample$|template$)[^/]+)?|\.envrc|\.netrc|\.pgpass"
    r"|\.npmrc|\.pypirc|credentials(\.json)?|secrets?\.[^/]+|id_(rsa|dsa|ecdsa|ed25519)"
    r"|[^/]+\.(pem|key|p12|pfx|jks|keystore|ppk|db|sqlite3?|duckdb))$",
    re.IGNORECASE,
)
SECRET_TEXT = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    rb"|\bgh[pousr]_[A-Za-z0-9]{36,}"
    rb"|\bgithub_pat_[A-Za-z0-9_]{40,}"
    rb"|\bglpat-[A-Za-z0-9_-]{20,}"
    rb"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    rb"|\bxox[abposr]-[A-Za-z0-9-]{10,}"
    rb"|\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}"
    rb"|\bAIza[0-9A-Za-z_-]{35}"
)


def changed_paths(git: Git) -> list[str] | None:
    """Every path with uncommitted changes, including untracked (not ignored) files."""
    result = git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    if not result.ok:
        return None
    fields = result.out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC" and i < len(fields):  # rename/copy: the next field is the source
            paths.append(fields[i])
            i += 1
    return sorted(set(paths))


def hold_reason(root: Path, rel: str) -> str | None:
    """Why this path must not be auto-committed, or None if it is safe."""
    if SECRET_NAME.search(rel):
        return "secret-looking file name"
    path = root / rel
    try:
        info = path.lstat()
    except OSError:
        return None  # deleted: committing the deletion is safe
    if not stat.S_ISREG(info.st_mode):
        return None
    if info.st_size > MAX_FILE_BYTES:
        return f"larger than {MAX_FILE_BYTES // 2**20} MB"
    try:
        data = path.read_bytes()
    except OSError as err:
        return f"unreadable: {err.strerror}"
    if SECRET_TEXT.search(data):
        return "contains a secret-looking string"
    return None


def wip_branch(git: Git, today: str) -> str:
    """wip/<date>, or wip/<date>-N if that name is taken locally or on origin."""
    for n in range(1, 1000):
        name = f"wip/{today}" + (f"-{n}" if n > 1 else "")
        taken = any(
            git("show-ref", "--quiet", "--verify", ref).ok
            for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}")
        )
        if not taken:
            return name
    raise RuntimeError("no free wip branch name")


def plan(git: Git, root: Path, info: dict, today: str) -> dict | None:
    """What auto-save would commit and where, or None if there is nothing to save."""
    paths = changed_paths(git)
    if paths is None:
        return {"files": [], "held_back": [], "branch": None, "new_branch": False,
                "skipped": "git status failed"}  # fmt: skip
    if not paths:
        return None
    held = [{"path": p, "reason": r} for p in paths if (r := hold_reason(root, p))]
    held_paths = {h["path"] for h in held}
    files = [p for p in paths if p not in held_paths]
    branch = info["branch"]
    new_branch = is_protected(branch, info["default_branch"])
    skipped = None
    if len(files) > MAX_FILES:
        skipped = f"{len(files)} files changed (over {MAX_FILES}; missing .gitignore?)"
    elif files and new_branch:
        branch = wip_branch(git, today)
    return {
        "files": files,
        "held_back": held,
        "branch": branch,
        "new_branch": bool(files) and new_branch and not skipped,
        "skipped": skipped,
    }


def commit(git: Git, saving: dict, stamp: str) -> Result:
    """Create the WIP commit (on a new wip/ branch if needed). Only saving['files'] go in."""
    if saving["new_branch"]:
        switched = git("switch", "--quiet", "-c", saving["branch"])
        if not switched.ok:
            return switched
    files = saving["files"]
    listing = "\n".join(f"  {p}" for p in files[:50]) + ("\n  ..." if len(files) > 50 else "")
    message = f"wip: auto-save {len(files)} file(s)\n\nSaved by syncghrepo at {stamp}:\n{listing}\n"
    with tempfile.TemporaryDirectory() as tmp:
        spec = Path(tmp, "paths")
        spec.write_bytes(b"\0".join(p.encode("utf-8", "surrogateescape") for p in files))
        from_file = (f"--pathspec-from-file={spec}", "--pathspec-file-nul")
        added = git("add", "--all", *from_file)
        if not added.ok:
            return added
        # A pathspec commits only these paths, so anything else already staged stays out.
        return git("commit", "--quiet", "--no-verify", "-m", message, *from_file)
