"""End-to-end behaviour of one clone, against real git repos in tmp_path."""

import hashlib
import re
from datetime import datetime

import pytest
from conftest import FakeRunner, commit, fake_token, git, remote_head

from syncghrepo import save, sync
from syncghrepo.run import Result

WIP = f"wip/{datetime.now().astimezone().strftime('%Y%m%d')}-{save.host_tag()}"
# Anything that could lose work or rewrite published history. None of it may ever run.
FORBIDDEN = ("reset", "clean", "checkout", "restore", "stash drop", "stash pop", "stash clear")


def assert_safe(runner: FakeRunner) -> None:
    for cmd in runner.git_commands():
        assert not cmd.startswith(FORBIDDEN), cmd
        assert "--force " not in cmd + " " and " -f " not in f" {cmd} ", cmd
        if "--force-with-lease" in cmd:
            assert "refs/heads/wip/" in cmd, cmd


def apply(clone, runner=None, **kw):
    runner = runner or FakeRunner()
    kw.setdefault("can_push", True)
    return sync.process(runner, clone, dry_run=False, **kw), runner


def head(repo, ref="HEAD"):
    return git(repo, "rev-parse", ref)


def test_clean_clone_up_to_date(origin, clone):
    result, runner = apply(clone)
    assert result["status"] == "up-to-date"
    assert [a["cmd"] for a in result["actions"]] == ["git fetch --prune"]
    assert_safe(runner)


def test_behind_clone_is_fast_forwarded(origin, clone):
    new = origin.advance()
    result, runner = apply(clone)
    assert result["status"] == "updated"
    assert head(clone) == new
    assert result["backup_refs"] == []  # nothing local to rewrite
    assert_safe(runner)


def test_work_on_main_moves_to_pushed_wip_branch(origin, clone):
    (clone / "a.txt").write_text("edited\n")
    (clone / "new.txt").write_text("new\n")
    main_before = head(clone)

    result, runner = apply(clone)

    branch = WIP
    assert re.fullmatch(r"wip/\d{8}-[a-z0-9-]+", branch)
    assert result["status"] == "updated", result["actions"]
    assert result["saved"]["branch"] == branch
    assert result["saved"]["files"] == ["a.txt", "new.txt"]
    assert git(clone, "branch", "--show-current") == branch
    assert head(clone, "main") == main_before  # nothing committed to main
    assert remote_head(origin, "main") == main_before
    assert remote_head(origin, branch) == head(clone)  # pushed
    assert git(clone, "rev-parse", "--abbrev-ref", "@{upstream}") == f"origin/{branch}"
    assert git(clone, "status", "--porcelain") == ""
    assert "wip: auto-save 2 file(s)" in git(clone, "log", "-1", "--format=%B")
    assert_safe(runner)


def test_second_wip_branch_the_same_day_gets_a_suffix(origin, clone):
    (clone / "a.txt").write_text("first\n")
    apply(clone)
    git(clone, "switch", "-q", "main")
    (clone / "a.txt").write_text("second\n")
    result, _ = apply(clone)
    assert result["saved"]["branch"] == f"{WIP}-2"


def test_main_fast_forwards_while_on_wip_branch(origin, clone):
    (clone / "a.txt").write_text("edited\n")
    new = origin.advance()
    result, _ = apply(clone)
    assert head(clone, "main") == new
    assert any(a["cmd"] == "git fetch . origin/main:main" for a in result["actions"])


def test_work_on_feature_branch_is_committed_there_and_pushed(origin, clone):
    git(clone, "switch", "-q", "-c", "feat")
    commit(clone, "f.txt", "feature\n")
    git(clone, "push", "-q", "-u", "origin", "feat")
    (clone / "f.txt").write_text("more\n")

    result, runner = apply(clone)

    assert result["saved"]["branch"] == "feat"
    assert remote_head(origin, "feat") == head(clone)
    assert not any(cmd.startswith("switch") for cmd in runner.git_commands())


def test_local_only_branch_is_pushed_with_upstream(origin, clone):
    git(clone, "switch", "-q", "-c", "idea")
    commit(clone, "i.txt", "idea\n")
    result, _ = apply(clone)
    assert remote_head(origin, "idea") == head(clone)
    assert result["status"] == "updated"


def test_secret_hit_skips_the_whole_clone(origin, clone, monkeypatch):
    monkeypatch.setattr(save, "MAX_FILE_BYTES", 1000)
    (clone / ".gitignore").write_text("*.log\n")
    (clone / "debug.log").write_text("ignored\n")
    (clone / ".env").write_text("TOKEN=x\n")
    (clone / ".env.example").write_text("TOKEN=\n")
    (clone / "config.py").write_text(f"TOKEN = '{fake_token()}'\n")
    (clone / "big.bin").write_bytes(b"0" * 2000)
    (clone / "ok.py").write_text("print('hi')\n")
    (clone / "server.pem").write_text("cert\n")
    git(clone, "add", "server.pem")  # staged by hand: still a hit
    origin.advance()  # upstream moved, but the clone must not be touched at all
    before = head(clone)

    result, runner = apply(clone)

    held = {h["path"]: h["reason"] for h in result["held_back"]}
    assert set(held) == {".env", "config.py", "big.bin", "server.pem"}  # not debug.log
    assert held["config.py"] == "contains a secret-looking string"
    assert fake_token() not in str(result)
    assert result["status"] == "held-back" and "clone skipped" in result["note"]
    assert result["saved"] is None and result["actions"] == []
    assert head(clone) == before
    assert not {"commit", "add", "fetch", "pull", "push", "switch"} & set(runner.git_subcommands())
    assert (clone / ".env").exists() and (clone / "ok.py").exists()
    assert_safe(runner)


def test_clean_clone_without_secrets_still_commits_non_ignored_files(origin, clone):
    (clone / ".gitignore").write_text("*.log\n")
    (clone / "debug.log").write_text("ignored\n")
    (clone / ".env.example").write_text("TOKEN=\n")
    (clone / "ok.py").write_text("print('hi')\n")
    result, _ = apply(clone)
    committed = git(clone, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(committed) == [".env.example", ".gitignore", "ok.py"]
    assert result["status"] == "updated"


def test_too_many_files_is_not_auto_saved(origin, clone, monkeypatch):
    monkeypatch.setattr(save, "MAX_FILES", 2)
    for n in range(3):
        (clone / f"f{n}.txt").write_text("x\n")
    result, _ = apply(clone)
    assert result["status"] == "held-back"
    assert "over 2" in result["save_skipped"]
    assert git(clone, "branch", "--show-current") == "main"


def diverge(origin, clone, files_upstream: dict, files_mine: dict):
    """Put Ben's clone on a pushed feature branch, then change both sides."""
    git(clone, "switch", "-q", "-c", "feat")
    git(clone, "push", "-q", "-u", "origin", "feat")
    for name, text in files_upstream.items():
        origin.advance(name, text, branch="feat")
    for name, text in files_mine.items():
        commit(clone, name, text)
    git(clone, "fetch", "-q")


def test_trivial_conflicts_are_resolved_with_a_backup(origin, clone):
    commit(clone, "uv.lock", "lock base\n")
    commit(clone, "style.py", "x = 1\ny = 2\n")
    commit(clone, "same.txt", "base\n")
    git(clone, "push", "-q", "origin", "main")
    diverge(
        origin,
        clone,
        {"uv.lock": "lock upstream\n", "style.py": "x = 1   \ny = 2\n", "same.txt": "same\n"},
        {"uv.lock": "lock mine\n", "same.txt": "same\n"},
    )
    # One commit that also does real work, so git cannot drop it as an equivalent patch.
    (clone / "style.py").write_bytes(b"x = 1\r\ny = 2\n")
    (clone / "feature.py").write_text("feature = True\n")
    git(clone, "add", "style.py", "feature.py")
    git(clone, "commit", "-q", "-m", "feature plus line endings")
    mine_before = head(clone)
    regenerated = []

    def uv_lock(cmd, cwd):
        regenerated.append(cwd)
        (clone / "uv.lock").write_text("lock regenerated\n")
        return Result(0)

    runner = FakeRunner(tools={("uv", "lock"): uv_lock})
    result, _ = apply(clone, runner)

    assert result["status"] == "updated", result["actions"]
    assert {r["path"]: r["how"] for r in result["resolved"]} == {
        "uv.lock": "lockfile",
        "style.py": "whitespace",
    }
    assert regenerated == [str(clone)]
    assert (clone / "uv.lock").read_text() == "lock regenerated\n"
    assert (clone / "style.py").read_bytes() == b"x = 1\r\ny = 2\n"  # mine kept
    assert (clone / "feature.py").exists()
    assert git(clone, "merge-base", "--is-ancestor", "origin/feat", "HEAD") == ""
    assert remote_head(origin, "feat") == head(clone)
    (backup,) = result["backup_refs"]
    assert backup.startswith("refs/syncghrepo/backup/")
    assert head(clone, backup) == mine_before
    assert_safe(runner)


def test_real_conflict_is_aborted_and_nothing_is_lost(origin, clone):
    diverge(origin, clone, {"a.txt": "upstream edit\n"}, {"a.txt": "my edit\n"})
    (clone / "notes.txt").write_text("uncommitted\n")
    remote_before = remote_head(origin, "feat")

    result, runner = apply(clone)

    assert result["status"] == "conflict"
    assert [c["path"] for c in result["conflicts"]] == ["a.txt"]
    assert result["conflicts"][0]["how"] == "manual"
    assert not (clone / ".git" / "rebase-merge").exists()
    (backup,) = result["backup_refs"]
    assert head(clone) == head(clone, backup)  # the branch is exactly where it was
    assert (clone / "a.txt").read_text() == "my edit\n"
    assert "notes.txt" in git(clone, "show", "--name-only", "--format=", "HEAD")  # auto-saved
    assert remote_head(origin, "feat") == remote_before  # nothing pushed over upstream
    assert "rebase --abort" in runner.git_commands()
    assert "push" not in runner.git_subcommands()  # never push after a failed pull
    assert_safe(runner)

    again, _ = apply(clone)  # still conflicted: the same backup is reused, not duplicated
    assert again["backup_refs"] == [backup]
    assert git(clone, "for-each-ref", "refs/syncghrepo").count("\n") == 0


def test_dry_run_fetch_sees_new_upstream_commits(origin, clone):
    origin.advance()
    stale = sync.process(FakeRunner(), clone, dry_run=True)
    fresh = sync.process(FakeRunner(), clone, dry_run=True, fetch=True)
    assert stale["status"] == "up-to-date"
    assert fresh["status"] == "would-update"
    assert [a["cmd"] for a in fresh["actions"]] == [
        "git fetch --prune",
        "git pull --rebase --autostash",
    ]


def test_secret_in_tracked_file_blocks_rebase_and_push(origin, clone):
    commit(clone, "settings.py", "TOKEN = ''\n")
    git(clone, "push", "-q", "origin", "main")
    diverge(origin, clone, {"b.txt": "upstream\n"}, {"c.txt": "mine\n"})
    (clone / "settings.py").write_text(f"TOKEN = '{fake_token()}'\n")
    before = head(clone)

    result, runner = apply(clone)

    assert result["status"] == "held-back"
    assert head(clone) == before and not (clone / "b.txt").exists()
    assert fake_token() in (clone / "settings.py").read_text()
    assert result["stashes"] == []
    assert "rebase" not in runner.git_subcommands()


def test_dry_run_reports_the_secret_skip(origin, clone):
    (clone / ".env").write_text("SECRET=1\n")
    (clone / "a.txt").write_text("edited\n")
    result = sync.process(FakeRunner(), clone, can_push=True, dry_run=True)
    assert result["status"] == "held-back"
    assert [h["path"] for h in result["held_back"]] == [".env"]
    assert result["actions"] == []


def test_dry_run_writes_nothing_and_shows_the_plan(origin, clone):
    commit(clone, "uv.lock", "base\n")
    git(clone, "push", "-q", "origin", "main")
    diverge(origin, clone, {"uv.lock": "upstream\n", "a.txt": "theirs\n"}, {})
    (clone / "uv.lock").write_text("mine\n")
    (clone / "a.txt").write_text("ours\n")

    def fingerprint():
        files = sorted(p for p in (clone / ".git").rglob("*") if p.is_file())
        digest = hashlib.sha256()
        for path in files:
            if path.name != "index":  # git may refresh index stat data on reads
                digest.update(str(path).encode() + path.read_bytes())
        status = git(clone, "status", "--porcelain")
        return digest.hexdigest(), status, len(files)

    before = fingerprint()
    runner = FakeRunner(git_hook=lambda cmd: None)
    result = sync.process(runner, clone, can_push=True, fork="me/proj", dry_run=True)

    assert fingerprint() == before
    assert result["status"] == "conflict"
    cmds = [a["cmd"] for a in result["actions"]]
    assert cmds[0] == "gh repo sync me/proj"
    assert "git commit 2 file(s) on feat" in cmds
    assert any(c.startswith("git update-ref refs/syncghrepo/backup/") for c in cmds)
    assert "git pull --rebase --autostash" in cmds
    assert {r["path"]: r["how"] for r in result["resolved"]} == {"uv.lock": "lockfile"}
    assert [c["path"] for c in result["conflicts"]] == ["a.txt"]
    assert result["held_back"] == []
    assert all(a["ok"] is None for a in result["actions"])
    assert not {"fetch", "pull", "push", "commit", "rebase", "switch"} & set(
        runner.git_subcommands()
    )
    assert not any(c[0] == "gh" for c in runner.calls)


def test_dry_run_on_main_plans_wip_branch_and_push(origin, clone):
    (clone / "a.txt").write_text("edited\n")
    result = sync.process(FakeRunner(), clone, can_push=True, dry_run=True)
    cmds = [a["cmd"] for a in result["actions"]]
    assert cmds == [
        f"git switch -c {WIP}",
        f"git commit 1 file(s) on {WIP}",
        f"git push -u origin HEAD:refs/heads/{WIP}",
    ]
    assert result["status"] == "would-update"
    assert git(clone, "branch", "--show-current") == "main"


def test_wip_branch_rejected_push_retries_with_lease_only(origin, clone):
    git(clone, "switch", "-q", "-c", WIP)
    git(clone, "push", "-q", "-u", "origin", "HEAD")
    commit(clone, "w.txt", "work\n")
    rejected = Result(1, "", "! [rejected] (fetch first)")
    first = {"done": False}

    def hook(cmd):
        if "push" in cmd and not first["done"]:
            first["done"] = True
            return rejected
        return None

    result, runner = apply(clone, FakeRunner(git_hook=hook))

    pushes = [c for c in runner.git_commands() if c.startswith("push")]
    assert len(pushes) == 2 and "--force-with-lease=refs/heads/wip/" in pushes[1]
    assert remote_head(origin, WIP) == head(clone)
    assert result["status"] == "updated"
    assert_safe(runner)


def test_feature_branch_rejected_push_is_never_forced(origin, clone):
    git(clone, "switch", "-q", "-c", "feat")
    git(clone, "push", "-q", "-u", "origin", "feat")
    commit(clone, "w.txt", "work\n")
    hook = lambda cmd: Result(1, "", "! [rejected]") if "push" in cmd else None  # noqa: E731

    result, runner = apply(clone, FakeRunner(git_hook=hook))

    assert len([c for c in runner.git_commands() if c.startswith("push")]) == 1
    assert result["status"] == "error"
    assert_safe(runner)


def test_commits_on_main_are_never_pushed(origin, clone):
    commit(clone, "local.txt", "on main\n")
    before = remote_head(origin, "main")
    result, runner = apply(clone)
    assert remote_head(origin, "main") == before
    assert "push" not in runner.git_subcommands()
    assert any("not pushed" in a["note"] for a in result["actions"])


def test_deleted_upstream_is_not_recreated(origin, clone):
    git(clone, "switch", "-q", "-c", "merged")
    git(clone, "push", "-q", "-u", "origin", "merged")
    git(origin.bare, "branch", "-D", "merged")
    (clone / "late.txt").write_text("late\n")

    result, runner = apply(clone)

    assert result["saved"]["branch"] == "merged"
    assert remote_head(origin, "merged") is None
    assert "push" not in runner.git_subcommands()


def test_no_push_access_commits_but_does_not_push(origin, clone):
    git(clone, "switch", "-q", "-c", "feat")
    (clone / "x.txt").write_text("x\n")
    result, runner = apply(clone, can_push=False)
    assert result["saved"]["commit"]
    assert "push" not in runner.git_subcommands()
    assert any("no known push access" in a["note"] for a in result["actions"])


def test_fork_is_synced_with_gh(origin, clone):
    def gh_sync(cmd):
        origin.advance("upstream.txt", "from parent\n")
        return Result(0, "", "✓ Synced")

    runner = FakeRunner({("repo", "sync"): gh_sync})
    result, _ = apply(clone, runner, fork="benpshore/proj")
    assert ["gh", "repo", "sync", "benpshore/proj"] in runner.calls
    assert (clone / "upstream.txt").exists()
    assert result["status"] == "updated"


def test_empty_repo_is_skipped(tmp_path):
    repo = tmp_path / "empty"
    git(tmp_path, "init", "-q", str(repo))
    (repo / "x.txt").write_text("x\n")
    result, runner = apply(repo)
    assert result["status"] == "skipped" and "no commits yet" in result["note"]
    assert "commit" not in runner.git_subcommands()


@pytest.mark.parametrize("state", ["detached", "merge"])
def test_busy_or_detached_clones_are_left_alone(origin, clone, state):
    (clone / "a.txt").write_text("edit\n")
    if state == "detached":
        git(clone, "checkout", "-q", "--detach")
    else:
        (clone / ".git" / "MERGE_HEAD").write_text(head(clone) + "\n")
    result, runner = apply(clone)
    assert result["status"] == "skipped"
    assert set(runner.git_subcommands()) <= {
        "symbolic-ref", "rev-parse", "config", "status", "rev-list", "for-each-ref",
        "stash", "worktree", "remote",
    }  # fmt: skip
    assert (clone / "a.txt").read_text() == "edit\n"
