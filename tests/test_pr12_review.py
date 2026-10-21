"""Independent safety regressions for PR #12; only temporary local remotes."""

import pytest
from conftest import FakeRunner, commit, git
from test_clone_nested import super_clone

from syncghrepo import nested, sync


@pytest.mark.parametrize("work", ["branch", "unstaged", "staged", "untracked"])
def test_parent_sync_does_not_recurse_before_safety_check(tmp_path, origin, monkeypatch, work):
    root = super_clone(tmp_path, origin, monkeypatch)
    git(root, "submodule", "update", "--init")
    child = root / "libs/subby"
    before = git(child, "rev-parse", "HEAD")
    if work == "branch":
        git(child, "switch", "-c", "active-work")
    elif work in ("unstaged", "staged"):
        (child / "README.md").write_text("precious local work\n")
        if work == "staged":
            git(child, "add", "README.md")
    else:
        (child / "notes.txt").write_text("precious untracked work\n")
    status = git(child, "status", "--porcelain")
    seed = tmp_path / "seeds/subby"
    commit(seed, "new.txt", "remote advancement\n")
    git(seed, "push")
    git(origin.seed / "libs/subby", "fetch")
    git(origin.seed / "libs/subby", "checkout", "origin/main")
    git(origin.seed, "commit", "-am", "advance child")
    git(origin.seed, "push")
    git(root, "config", "submodule.recurse", "true")
    report = sync.process(FakeRunner(), root, dry_run=False)
    nested.update(FakeRunner(), report, dry_run=False)
    assert git(child, "rev-parse", "HEAD") == before
    assert git(child, "status", "--porcelain") == status
    if work == "branch":
        assert git(child, "branch", "--show-current") == "active-work"
    assert report["status"] != "error"


def test_update_preserves_untracked_work_hidden_by_git_config(tmp_path, origin, monkeypatch):
    root = super_clone(tmp_path, origin, monkeypatch)
    git(root, "submodule", "update", "--init")
    child = root / "libs/subby"
    before = git(child, "rev-parse", "HEAD")
    (child / "notes.txt").write_text("unsaved notes\n")
    git(child, "config", "status.showUntrackedFiles", "no")
    seed = tmp_path / "seeds/subby"
    commit(seed, "new.txt", "remote advancement\n")
    git(seed, "push")
    git(root, "fetch", "--recurse-submodules=no")
    git(child, "fetch")
    git(
        root,
        "update-index",
        "--cacheinfo",
        "160000",
        git(child, "rev-parse", "origin/main"),
        "libs/subby",
    )
    report = {"path": str(root), "busy": None, "status": "up-to-date", "actions": []}
    nested.update(FakeRunner(), report, dry_run=False)
    assert git(child, "rev-parse", "HEAD") == before
    assert (child / "notes.txt").read_text() == "unsaved notes\n"
    assert "not run" in report["actions"][-1]["note"]


def test_disappearing_discovered_directory_marks_inventory_incomplete(tmp_path, monkeypatch):
    from pathlib import Path

    from syncghrepo import local

    root = tmp_path / "root"
    child = root / "disappeared"
    child.mkdir(parents=True)
    original = Path.iterdir

    def iterdir(path):
        if path == child:
            raise FileNotFoundError("discovered directory disappeared")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    errors = []
    local.find_clones([root], errors=errors)
    assert errors
    errors = []
    local.find_clones([tmp_path / "absent-default-root"], errors=errors)
    assert not errors
