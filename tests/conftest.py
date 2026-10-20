"""Test helpers. No test touches the network: git only talks to bare repos in
tmp_path, and gh and lockfile tools are always fakes that answer from canned replies."""

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
    cfg.write_text("[init]\n\tdefaultBranch = main\n[advice]\n\tmergeConflict = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test User")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")


class FakeRunner:
    """Runs git for real against local repos; answers everything else from fakes.

    gh maps a gh argument prefix (tuple) to a Result or a callable(cmd) -> Result.
    tools maps a full command prefix, e.g. ("uv", "lock"), to callable(cmd, cwd).
    git_hook(cmd) may return a Result to replace a git call. Anything else fails
    the test, so nothing reaches GitHub or a package index.
    """

    def __init__(self, gh: dict | None = None, tools: dict | None = None, git_hook=None):
        self.gh = gh or {}
        self.tools = tools or {}
        self.git_hook = git_hook
        self.calls: list[list[str]] = []

    def __call__(self, cmd, cwd=None, env=None) -> Result:
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[0] == "gh":
            for prefix, reply in self.gh.items():
                if tuple(cmd[1 : 1 + len(prefix)]) == prefix:
                    return reply(cmd) if callable(reply) else reply
            raise AssertionError(f"unexpected gh call: {cmd}")
        if cmd[0] == "git":
            hooked = self.git_hook(cmd) if self.git_hook else None
            return hooked or run(cmd, cwd, timeout=30, env=env)
        for prefix, reply in self.tools.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                return reply(cmd, cwd)
        raise AssertionError(f"unexpected command: {cmd}")

    def git_subcommands(self) -> list[str]:
        """git subcommands run, skipping global options such as -c and --no-optional-locks."""
        return [subcommand(c) for c in self.calls if c[0] == "git"]

    def git_commands(self) -> list[str]:
        return [" ".join(c[c.index(subcommand(c)) :]) for c in self.calls if c[0] == "git"]


def subcommand(cmd: list[str]) -> str:
    args = iter(cmd[1:])
    for arg in args:
        if arg == "-c":
            next(args)
        elif not arg.startswith("-"):
            return arg
    return ""


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
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
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

    def advance(name: str = "b.txt", text: str = "two\n", branch: str = "main") -> str:
        git(seed, "fetch", "-q")
        git(seed, "switch", "-q", "-C", branch, f"origin/{branch}")
        sha = commit(seed, name, text)
        git(seed, "push", "-q", "origin", branch)
        return sha

    return SimpleNamespace(bare=bare, seed=seed, advance=advance)


@pytest.fixture
def clone(tmp_path, origin):
    """Ben's local clone, under a fake ~/code root."""
    path = tmp_path / "code" / "GitHub" / "proj"
    path.parent.mkdir(parents=True)
    git(tmp_path, "clone", "-q", str(origin.bare), str(path))
    return path


def remote_head(origin, branch: str) -> str | None:
    try:
        return git(origin.bare, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    except subprocess.CalledProcessError:
        return None


def fake_token() -> str:
    """A GitHub-token-shaped string, built at runtime so no literal sits in the repo."""
    return "gh" + "p_" + "A1b2C3d4" * 5
