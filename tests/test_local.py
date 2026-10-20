import time

import pytest
from conftest import FakeRunner, commit, git

from syncghrepo import local

# Anything that could lose work or rewrite history. None of it may ever run.
FORBIDDEN = {"stash push", "stash drop", "stash pop", "reset", "clean", "checkout", "restore"}


def assert_safe(runner: FakeRunner) -> None:
    for cmd in runner.calls:
        text = " ".join(cmd)
        assert "--force" not in cmd and "-f" not in cmd, text
        assert not any(f"git --no-optional-locks {bad}" in text for bad in FORBIDDEN), text
        assert not any(f"git {bad}" in text for bad in FORBIDDEN), text


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("https://github.com/benpshore/syncghrepo.git", "benpshore/syncghrepo"),
        ("https://github.com/benpshore/syncghrepo", "benpshore/syncghrepo"),
        ("https://github.com/benpshore/syncghrepo/", "benpshore/syncghrepo"),
        ("git@github.com:caligoapp/pdf.git", "caligoapp/pdf"),
        ("ssh://git@github.com/benpshore/tz.util.git", "benpshore/tz.util"),
        ("https://ben@github.com/benpshore/x-y_z.git\n", "benpshore/x-y_z"),
        ("https://gitlab.com/benpshore/syncghrepo.git", None),
        ("../remote/proj.git", None),
        (None, None),
    ],
)
def test_github_name(url, name):
    assert local.github_name(url) == name


def test_find_clones_skips_worktrees_hidden_and_deep_dirs(tmp_path):
    root = tmp_path / "code"
    for repo in ("GitHub/a", "GitHub/b", "c", "GitHub/org/deep/too-deep"):
        (root / repo / ".git").mkdir(parents=True)
    (root / "GitHub/a/nested/.git").mkdir(parents=True)  # inside a clone: not searched
    (root / "worktrees/a-feature").mkdir(parents=True)
    (root / "worktrees/a-feature/.git").write_text("gitdir: ../../GitHub/a/.git/worktrees/x\n")
    (root / "GitHub/node_modules/pkg/.git").mkdir(parents=True)
    (root / ".hidden/repo/.git").mkdir(parents=True)

    found = local.find_clones([root, tmp_path / "missing"], max_depth=3)

    assert [p.relative_to(root.resolve()).as_posix() for p in found] == [
        "GitHub/a",
        "GitHub/b",
        "c",
    ]
    assert len(local.find_clones([root], max_depth=4)) == 4


def state(**over):
    base = {
        "busy": None,
        "branch": "main",
        "upstream": "origin/main",
        "changes": 0,
        "ahead": 0,
        "behind": 0,
    }
    return base | over


@pytest.mark.parametrize(
    ("over", "status"),
    [
        ({}, "up-to-date"),
        ({"behind": 2}, "pull"),
        ({"ahead": 1}, "ahead"),
        ({"ahead": 1, "behind": 2}, "diverged"),
        ({"changes": 3, "behind": 2}, "dirty"),
        ({"upstream": None, "behind": None}, "no-upstream"),
        ({"branch": None, "upstream": None}, "detached"),
        ({"busy": "rebase", "behind": 2}, "busy"),
    ],
)
def test_plan(over, status):
    assert local.plan(state(**over))[0] == status


def test_pulls_clean_clone_that_is_behind(origin, clone):
    new_head = origin.advance()
    runner = FakeRunner()

    result = local.update(runner, clone, dry_run=False)

    assert result["status"] == "updated"
    assert git(clone, "rev-parse", "HEAD") == new_head
    assert [a["cmd"] for a in result["actions"]] == ["git fetch", "git pull --ff-only"]
    assert_safe(runner)


def test_up_to_date_clone_is_left_alone(origin, clone):
    runner = FakeRunner()
    result = local.update(runner, clone, dry_run=False)
    assert result["status"] == "up-to-date"
    assert "pull" not in runner.git_subcommands()


def test_dry_run_runs_no_network_or_write_commands(origin, clone):
    before = git(clone, "rev-parse", "HEAD")
    origin.advance()
    git(clone, "fetch", "-q")  # the last fetch already knows the remote moved
    runner = FakeRunner()

    result = local.update(runner, clone, fork="ben/proj", dry_run=True)

    assert result["status"] == "would-pull"
    assert result["behind"] == 1
    assert result["would_run"] == ["gh repo sync ben/proj", "git fetch", "git pull --ff-only"]
    assert git(clone, "rev-parse", "HEAD") == before
    assert not {"fetch", "pull", "merge"} & set(runner.git_subcommands())
    assert all(cmd[0] == "git" for cmd in runner.calls)  # fake gh would raise anyway


def test_dirty_clone_is_reported_not_pulled_or_stashed(origin, clone):
    before = git(clone, "rev-parse", "HEAD")
    (clone / "a.txt").write_text("my local edit\n")
    origin.advance()
    runner = FakeRunner()

    result = local.update(runner, clone, dry_run=False)

    assert result["status"] == "dirty"
    assert result["changes"] == 1
    assert result["behind"] == 1
    assert git(clone, "rev-parse", "HEAD") == before
    assert (clone / "a.txt").read_text() == "my local edit\n"
    assert git(clone, "stash", "list") == ""
    assert "pull" not in runner.git_subcommands()
    assert_safe(runner)


def test_untracked_files_do_not_block_but_are_reported(origin, clone):
    (clone / "notes.txt").write_text("scratch\n")
    new_head = origin.advance()

    result = local.update(FakeRunner(), clone, dry_run=False)

    assert result["status"] == "updated"
    assert result["untracked"] == 1
    assert git(clone, "rev-parse", "HEAD") == new_head
    assert (clone / "notes.txt").exists()


def test_diverged_clone_is_reported_not_pulled(origin, clone):
    mine = commit(clone, "c.txt", "local work\n")
    origin.advance()
    runner = FakeRunner()

    result = local.update(runner, clone, dry_run=False)

    assert result["status"] == "diverged"
    assert (result["ahead"], result["behind"]) == (1, 1)
    assert git(clone, "rev-parse", "HEAD") == mine
    assert "pull" not in runner.git_subcommands()
    assert_safe(runner)


def test_detached_and_untracked_branches_are_skipped(origin, clone):
    origin.advance()
    git(clone, "checkout", "-q", "--detach")
    assert local.update(FakeRunner(), clone, dry_run=False)["status"] == "detached"

    git(clone, "switch", "-q", "-c", "local-only")
    result = local.update(FakeRunner(), clone, dry_run=False)
    assert result["status"] == "no-upstream"


def test_merge_in_progress_is_busy(origin, clone):
    git_dir = clone / ".git"
    (git_dir / "MERGE_HEAD").write_text(git(clone, "rev-parse", "HEAD") + "\n")
    origin.advance()
    runner = FakeRunner()
    assert local.update(runner, clone, dry_run=False)["status"] == "busy"
    assert "pull" not in runner.git_subcommands()


def test_reports_unpushed_stale_gone_branches_stashes_and_worktrees(tmp_path, origin, clone):
    git(clone, "switch", "-q", "-c", "wip")
    commit(clone, "w.txt", "wip\n")
    git(clone, "switch", "-q", "-c", "old", "main")
    old = "2020-01-01T00:00:00"
    commit(clone, "o.txt", "old\n", GIT_COMMITTER_DATE=old, GIT_AUTHOR_DATE=old)
    git(clone, "push", "-q", "-u", "origin", "old")
    git(clone, "switch", "-q", "-c", "merged", "main")
    commit(clone, "m.txt", "merged\n")
    git(clone, "push", "-q", "-u", "origin", "merged")
    git(clone, "push", "-q", "origin", "--delete", "merged")
    git(clone, "fetch", "-q", "--prune")
    git(clone, "switch", "-q", "main")
    (clone / "a.txt").write_text("stashed\n")
    git(clone, "stash", "push", "-q", "-m", "half done")
    git(clone, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "side")

    result = local.inspect(FakeRunner(), clone, stale_days=30, now=time.time())

    branches = {b["name"]: b for b in result["branches"]}
    assert set(branches) == {"wip", "old", "merged"}
    assert branches["wip"]["unpushed"] == 1 and branches["wip"]["upstream"] is None
    assert branches["old"]["stale"] and not branches["old"]["unpushed"]
    assert branches["merged"]["gone"] and branches["merged"]["unpushed"] == 1
    assert len(result["stashes"]) == 1 and "half done" in result["stashes"][0]
    assert [p.endswith("wt") for p in result["worktrees"]] == [True]
    assert result["default_branch"] == "main"
    assert result["changes"] == 0


def test_fork_is_synced_with_gh_before_pulling(origin, clone):
    runner = FakeRunner({("repo", "sync"): lambda cmd: origin_sync(origin)})

    result = local.update(runner, clone, fork="benpshore/proj", dry_run=False)

    assert ["gh", "repo", "sync", "benpshore/proj"] in runner.calls
    assert result["status"] == "updated"
    assert result["head"] == git(origin.seed, "rev-parse", "--short", "HEAD")


def origin_sync(origin):
    """Stand-in for gh repo sync: the fork on 'GitHub' gains the parent's commit."""
    from syncghrepo.run import Result

    origin.advance("upstream.txt", "from parent\n")
    return Result(0, "", "✓ Synced the main branch from parent")


def test_failed_fork_sync_is_reported_and_pull_still_safe(origin, clone):
    from syncghrepo.run import Result

    runner = FakeRunner({("repo", "sync"): Result(1, "", "can't sync: diverging changes")})
    result = local.update(runner, clone, fork="benpshore/proj", dry_run=False)
    assert result["actions"][0] == {
        "cmd": "gh repo sync benpshore/proj",
        "ok": False,
        "note": "can't sync: diverging changes",
    }
    assert result["status"] == "up-to-date"
    assert_safe(runner)
