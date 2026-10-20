import json
from datetime import datetime

from conftest import FakeRunner, git, remote_head
from test_github import pr, repo_line, status_payload

from syncghrepo import cli, save
from syncghrepo.run import Result

WIP = f"wip/{datetime.now().astimezone().strftime('%Y%m%d')}-{save.host_tag()}"


def gh_replies(**permissions):
    """A fake GitHub where Ben owns proj (cloned locally) and other (not cloned)."""
    status = {
        "benpshore/proj": status_payload(
            "benpshore/proj",
            ci="FAILURE",
            prs=[pr(7, "dependabot")],
            permission=permissions.get("proj", "ADMIN"),
        ),
        "benpshore/other": status_payload("benpshore/other"),
    }

    def graphql(cmd):
        owner = next(a for a in cmd if a.startswith("owner="))[6:]
        name = next(a for a in cmd if a.startswith("name="))[5:]
        return Result(0, status[f"{owner}/{name}"])

    return {
        ("api", "user"): Result(0, "benpshore\n"),
        ("api", "--paginate"): Result(
            0, repo_line("benpshore/proj") + "\n" + repo_line("benpshore/other") + "\n"
        ),
        ("search", "prs"): Result(0, ""),
        ("api", "graphql"): graphql,
    }


def point_origin_at_github(clone, name="benpshore/proj"):
    """The clone looks like a GitHub clone, but fetches and pushes to the local bare repo."""
    bare = git(clone, "remote", "get-url", "origin")
    url = f"https://github.com/{name}.git"
    git(clone, "remote", "set-url", "origin", url)
    git(clone, "config", f"url.{bare}.insteadOf", url)


def test_default_is_a_dry_run_with_json(tmp_path, origin, clone, capsys):
    point_origin_at_github(clone)
    (clone / "a.txt").write_text("edited\n")
    (tmp_path / "code" / "plain").mkdir()
    git(tmp_path / "code" / "plain", "init", "-q")
    runner = FakeRunner(gh_replies())

    code = cli.main(["--root", str(tmp_path / "code"), "--json"], runner=runner)

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["user"] == "benpshore" and report["dry_run"] is True
    repos = {r["name"]: r for r in report["repos"]}
    assert set(repos) == {"benpshore/proj", "benpshore/other", "plain"}
    proj = repos["benpshore/proj"]["clones"][0]
    assert proj["status"] == "would-update"
    planned = [a["cmd"] for a in proj["actions"]]
    assert planned[-1] == "git-sync"
    assert f"git push -u origin HEAD:refs/heads/{WIP}" in planned
    assert git(clone, "branch", "--show-current") == "main"
    assert git(clone, "diff", "--name-only") == "a.txt"  # still uncommitted
    assert git(clone, "diff", "--cached", "--name-only") == ""
    attention = repos["benpshore/proj"]["attention"]
    assert any(item.startswith("CI failure on main") for item in attention)
    assert any("PR #7 by dependabot [needs review]" in item for item in attention)
    assert repos["plain"]["github"] is False and repos["plain"]["remote"] is None
    assert report["summary"]["saved_files"] == 1
    assert report["summary"]["not_cloned"] == 1
    # Read-only: no fetch/pull/push/commit, and only read-only gh calls.
    assert not {"fetch", "pull", "push", "commit"} & set(runner.git_subcommands())
    assert {tuple(c[1:3]) for c in runner.calls if c[0] == "gh"} <= {
        ("api", "user"),
        ("api", "--paginate"),
        ("search", "prs"),
        ("api", "graphql"),
    }


def test_apply_saves_syncs_pushes_and_summarizes(tmp_path, origin, clone, capsys):
    point_origin_at_github(clone)
    new_main = origin.advance()
    (clone / "a.txt").write_text("edited\n")

    code = cli.main(["--root", str(tmp_path / "code"), "--apply"], runner=FakeRunner(gh_replies()))

    out = capsys.readouterr().out
    assert code == 0, out
    assert git(clone, "rev-parse", "main") == new_main
    assert remote_head(origin, WIP) == git(clone, "rev-parse", "HEAD")
    assert "Clones: 1 updated" in out
    assert "Saved: 1 file(s), 0 held back" in out
    assert "Done:" in out and "ok git-sync  # In sync, all fine." in out
    assert "CI failure on main (abcdef1)" in out
    assert "Not cloned, nothing open (1): benpshore/other" in out


def test_read_only_permission_means_no_push(tmp_path, origin, clone, capsys):
    point_origin_at_github(clone)
    git(clone, "switch", "-q", "-c", "feat")
    (clone / "a.txt").write_text("edited\n")
    runner = FakeRunner(gh_replies(proj="READ"))
    cli.main(["--root", str(tmp_path / "code"), "--apply"], runner=runner)
    assert "push" not in runner.git_subcommands()
    assert "not synced: no known push access; local work left as is" in capsys.readouterr().out


def test_conflict_exits_1(tmp_path, origin, clone, capsys):
    point_origin_at_github(clone)
    git(clone, "switch", "-q", "-c", "feat")
    git(clone, "push", "-q", "-u", "origin", "feat")
    origin.advance("a.txt", "theirs\n", branch="feat")
    (clone / "a.txt").write_text("mine\n")

    code = cli.main(["--root", str(tmp_path / "code"), "--apply"], runner=FakeRunner(gh_replies()))

    out = capsys.readouterr().out
    assert code == 1
    assert "conflict needs a person: a.txt" in out
    assert "backup: refs/syncghrepo/backup/" in out


def test_someone_elses_clone_only_shows_my_prs(tmp_path, origin, clone, capsys):
    git(clone, "remote", "set-url", "origin", "git@github.com:stranger/tool.git")
    payload = status_payload(
        "stranger/tool", ci="FAILURE", prs=[pr(1, "dependabot"), pr(2)], permission="READ"
    )
    replies = gh_replies() | {("api", "graphql"): lambda cmd: Result(0, payload)}
    cli.main(["--root", str(tmp_path / "code"), "--json"], runner=FakeRunner(replies))
    repos = json.loads(capsys.readouterr().out)["repos"]
    tool = next(r for r in repos if r["name"] == "stranger/tool")
    assert tool["sources"] == ["local"]
    assert [p["number"] for p in tool["remote"]["prs"]] == [2]
    assert tool["attention"] == ["PR #2 [mine]: PR 2"]


def test_gh_unavailable_still_reports_local_and_exits_1(tmp_path, clone, capsys):
    runner = FakeRunner({("api", "user"): Result(4, "", "gh auth login required")})
    code = cli.main(["--root", str(tmp_path / "code")], runner=runner)
    out = capsys.readouterr().out
    assert code == 1
    assert "gh not usable" in out
    assert "Clean (1): proj" in out


def test_no_github_flag_makes_no_gh_calls(tmp_path, clone, capsys):
    runner = FakeRunner()
    assert cli.main(["--root", str(tmp_path / "code"), "--no-github", "--json"], runner=runner) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["user"] is None
    assert all(cmd[0] == "git" for cmd in runner.calls)


def test_mode_flags():
    assert cli.parse_args([]).apply is False
    assert cli.parse_args(["--dry-run"]).apply is False
    assert cli.parse_args(["--apply"]).apply is True


def test_dry_run_and_apply_are_exclusive(capsys):
    try:
        cli.main(["--dry-run", "--apply"])
    except SystemExit as exit:
        assert exit.code == 2
    else:
        raise AssertionError("expected a usage error")


def test_roots_from_environment(monkeypatch):
    monkeypatch.setenv("SYNCGHREPO_ROOTS", "/a:/b")
    assert cli.default_roots() == ["/a", "/b"]
    monkeypatch.delenv("SYNCGHREPO_ROOTS")
    assert cli.default_roots() == ["~/code"]
