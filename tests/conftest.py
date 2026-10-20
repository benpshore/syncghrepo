"""Test helpers. No test touches the network: git only talks to bare repos in
tmp_path, and gh is always a fake that answers from canned replies."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from syncghrepo.run import Result, run


@pytest.fixture(autouse=True)
def isolated_git(tmp_path_factory, monkeypatch):
    """Keep tests away from the user's global git config (signing, hooks, identity)."""
    cfg = tmp_path_factory.mktemp("gitcfg") / "gitconfig"
    cfg.write_text("[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test User")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")


class FakeRunner:
    """Runs git for real against local repos; answers gh from canned replies.

    gh maps an argument prefix (tuple) to a Result or a callable(cmd) -> Result.
    Any gh call without a reply fails the test, so nothing reaches GitHub.
    """

    def __init__(self, gh: dict | None = None):
        self.gh = gh or {}
        self.calls: list[list[str]] = []

    def __call__(self, cmd, cwd=None) -> Result:
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[0] == "gh":
            for prefix, reply in self.gh.items():
                if tuple(cmd[1 : 1 + len(prefix)]) == prefix:
                    return reply(cmd) if callable(reply) else reply
            raise AssertionError(f"unexpected gh call: {cmd}")
        assert cmd[0] == "git", cmd
        return run(cmd, cwd, timeout=30)

    def git_subcommands(self) -> list[str]:
        """git subcommands run, skipping global options such as --no-optional-locks."""
        return [
            next(a for a in c[1:] if not a.startswith("-")) for c in self.calls if c[0] == "git"
        ]


def git(cwd: Path, *args: str, **env: str) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
    )
    return done.stdout.strip()


def commit(repo: Path, name: str, text: str, **env: str) -> str:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"edit {name}", **env)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def origin(tmp_path):
    """A bare 'GitHub' repo plus a seed clone used to push new commits to it."""
    bare = tmp_path / "remote" / "proj.git"
    bare.parent.mkdir()
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(bare), str(seed))
    commit(seed, "a.txt", "one\n")
    git(seed, "push", "-q", "origin", "main")

    def advance(name: str = "b.txt", text: str = "two\n") -> str:
        sha = commit(seed, name, text)
        git(seed, "push", "-q", "origin", "main")
        return sha

    return SimpleNamespace(bare=bare, seed=seed, advance=advance)


@pytest.fixture
def clone(tmp_path, origin):
    """Ben's local clone, under a fake ~/code root."""
    path = tmp_path / "code" / "GitHub" / "proj"
    path.parent.mkdir(parents=True)
    git(tmp_path, "clone", "-q", str(origin.bare), str(path))
    return path
