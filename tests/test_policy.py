"""Ben's policy: no AI/LLM calls. Plain git, gh and the standard library only."""

import ast
import sys
import tomllib
from pathlib import Path

import pytest

from syncghrepo import resolve, run

ROOT = Path(__file__).parent.parent


def test_no_runtime_dependencies():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["dependencies"] == []


def test_source_imports_only_the_standard_library():
    for path in (ROOT / "src" / "syncghrepo").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name.split(".")[0] in sys.stdlib_module_names, f"{path.name}: {name}"


@pytest.mark.parametrize(
    "cmd",
    [
        ["gh", "models", "run", "openai/gpt-4.1", "hi"],
        ["gh", "copilot", "suggest", "x"],
        ["gh", "extension", "exec", "x"],
        ["curl", "https://example.com"],
        ["python3", "-c", "print(1)"],
        ["claude", "-p", "hi"],
        ["uv", "run", "anything"],
        ["sh", "-c", "git status"],
    ],
)
def test_run_refuses_anything_not_allowlisted(cmd, tmp_path):
    result = run.run(cmd, str(tmp_path))
    assert result.code == 126
    assert "refused" in result.err


def test_allowlist_is_only_git_gh_and_lockfile_tools():
    assert {prefix[0] for prefix in run.ALLOWED} == {"git", "gh", "uv", "cargo", "npm", "pnpm",
                                                      "bundle"}  # fmt: skip
    assert {prefix[1] for prefix in run.ALLOWED if prefix[0] == "gh"} == {"api", "search", "repo"}


def test_every_lockfile_command_is_allowed():
    for cmd in resolve.LOCKFILES.values():
        assert run.allowed(cmd), cmd


def test_allowed_commands_still_run(tmp_path):
    assert run.run(["git", "--version"], str(tmp_path)).ok
