"""Independent regressions: real Git repositories, synthetic GitHub responses."""

from pathlib import Path

import pytest
from conftest import FakeRunner, git
from test_cli import point_origin_at_github, use_home
from test_clone_nested import fake_github, nested_clone, super_clone

from syncghrepo import cli, local, nested
from syncghrepo.run import Result


@pytest.mark.parametrize(
    "url", ["https://notgithub.com/benpshore/proj.git", "/local/github.com/benpshore/proj.git"]
)
def test_origin_requires_actual_github_host(url):
    assert local.github_name(url) is None


def test_unreadable_local_discovery_disables_cloning(tmp_path, origin, clone, monkeypatch):
    use_home(monkeypatch, tmp_path)
    point_origin_at_github(clone)
    blocked = tmp_path / "code" / "unreadable"
    blocked.mkdir()
    original = Path.iterdir

    def iterdir(path):
        if path == blocked:
            raise PermissionError("synthetic permission failure")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    found = cli.discover(FakeRunner(fake_github({"benpshore/proj": origin.bare})))
    assert not found["complete"]
    assert any("unreadable" in error for error in found["errors"])


def test_overlapping_roots_are_processed_parent_first(tmp_path, origin, clone, monkeypatch):
    point_origin_at_github(clone)
    bare, inner = nested_clone(tmp_path, clone, "tools/inner", "inner")
    monkeypatch.setenv("SYNCGHREPO_ROOTS", f"{clone}:{inner}")
    active = set()
    original = cli.sync.process

    def process(runner, path, **kwargs):
        assert not any(path.is_relative_to(p) or p.is_relative_to(path) for p in active)
        if path == inner:
            assert clone in finished
        active.add(path)
        try:
            return original(runner, path, **kwargs)
        finally:
            active.remove(path)
            finished.add(path)

    finished = set()
    monkeypatch.setattr(cli.sync, "process", process)
    cli.build_report(
        cli.parse_args(["--dry-run"]),
        FakeRunner(fake_github({"benpshore/proj": origin.bare, "benpshore/inner": bare})),
    )


def test_recursive_update_checks_even_matching_submodules(tmp_path, origin, monkeypatch):
    path = super_clone(tmp_path, origin, monkeypatch)
    git(path, "submodule", "update", "--init")
    sub = path / "libs/subby"
    git(sub, "switch", "-c", "work")
    # An ancestor update can change this currently matching child's recorded commit.
    assert nested.blockers(FakeRunner(), path, [(" ", "libs/subby")])


def test_failed_dirty_check_blocks_submodule_update(tmp_path, origin, monkeypatch):
    path = super_clone(tmp_path, origin, monkeypatch)
    git(path, "submodule", "update", "--init")
    runner = FakeRunner(
        git_hook=lambda cmd: (
            Result(1, "", "status failed") if "status" in cmd and "--porcelain" in cmd else None
        )
    )
    assert nested.blockers(runner, path, [("+", "libs/subby")])


def test_recursive_parent_move_preserves_child_branch(tmp_path, origin, monkeypatch):
    from conftest import commit
    from test_clone_nested import allow_file_submodules, bare_repo

    allow_file_submodules(monkeypatch)
    leaf_bare = bare_repo(tmp_path, "leaf")
    middle_bare = bare_repo(tmp_path, "middle")
    middle_seed = tmp_path / "seeds/middle"
    git(middle_seed, "submodule", "add", str(leaf_bare), "leaf")
    git(middle_seed, "commit", "-am", "record leaf v1")
    git(middle_seed, "push")
    git(origin.seed, "submodule", "add", str(middle_bare), "middle")
    git(origin.seed, "commit", "-am", "record middle v1")
    git(origin.seed, "push")
    root = tmp_path / "root"
    git(tmp_path, "clone", "--recurse-submodules", str(origin.bare), str(root))
    leaf = root / "middle/leaf"
    git(leaf, "switch", "-c", "active-work")
    before = git(leaf, "rev-parse", "HEAD")

    commit(tmp_path / "seeds/leaf", "new.txt", "v2\n")
    git(tmp_path / "seeds/leaf", "push")
    git(middle_seed / "leaf", "fetch")
    git(middle_seed / "leaf", "checkout", "origin/main")
    git(middle_seed, "commit", "-am", "record leaf v2")
    git(middle_seed, "push")
    git(origin.seed / "middle", "fetch")
    git(origin.seed / "middle", "checkout", "origin/main")
    git(origin.seed, "commit", "-am", "record middle v2")
    git(origin.seed, "push")
    git(root, "-c", "fetch.recurseSubmodules=false", "pull", "--ff-only")

    # middle is '+' but leaf is still ' ' relative to the old middle checkout.
    assert (" ", "middle/leaf") in local.submodule_status(FakeRunner(), root)
    report = {"path": str(root), "busy": None, "status": "up-to-date", "actions": []}
    nested.update(FakeRunner(), report, dry_run=False)
    assert git(leaf, "branch", "--show-current") == "active-work"
    assert git(leaf, "rev-parse", "HEAD") == before
    assert "not run" in report["actions"][-1]["note"]


@pytest.mark.parametrize("kind", ["file", "directory", "dangling-symlink"])
def test_occupied_destination_never_cloned(tmp_path, kind):
    from syncghrepo import clone as cloner
    from syncghrepo import github

    target = tmp_path / "proj"
    if kind == "file":
        target.write_text("keep me")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(tmp_path / "missing")
    runner = FakeRunner()
    records = cloner.plan(
        runner, {"benpshore/proj": github.new_repo("benpshore/proj")}, "benpshore", set(), tmp_path
    )
    assert records[0]["status"] == "skipped"
    cloner.clone(runner, records[0])
    assert not runner.calls
    assert target.exists() or target.is_symlink()


def test_failed_submodule_discovery_disables_cloning(tmp_path, origin, monkeypatch):
    use_home(monkeypatch, tmp_path)
    super_clone(tmp_path, origin, monkeypatch)
    runner = FakeRunner(
        fake_github({"benpshore/proj": origin.bare}),
        git_hook=lambda cmd: (
            Result(1, "", "cannot list submodules")
            if "submodule" in cmd and "status" in cmd
            else None
        ),
    )
    found = cli.discover(runner)
    assert not found["complete"]
    assert any("submodule discovery" in error for error in found["errors"])
