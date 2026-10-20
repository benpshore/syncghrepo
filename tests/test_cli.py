import json

from conftest import FakeRunner, git
from test_github import pr, repo_line, status_payload

from syncghrepo import cli
from syncghrepo.run import Result


def gh_replies(**over):
    """A fake GitHub where Ben owns proj (cloned locally) and other (not cloned)."""
    status = {
        "benpshore/proj": status_payload("benpshore/proj", ci="FAILURE", prs=[pr(7, "dependabot")]),
        "benpshore/other": status_payload("benpshore/other"),
    }

    def graphql(cmd):
        owner = next(a for a in cmd if a.startswith("owner="))[6:]
        name = next(a for a in cmd if a.startswith("name="))[5:]
        return Result(0, status[f"{owner}/{name}"])

    replies = {
        ("api", "user"): Result(0, "benpshore\n"),
        ("api", "--paginate"): Result(
            0, repo_line("benpshore/proj") + "\n" + repo_line("benpshore/other") + "\n"
        ),
        ("search", "prs"): Result(0, ""),
        ("api", "graphql"): graphql,
    }
    return replies | over


def point_origin_at_github(clone):
    """The clone looks like a GitHub clone, but fetches from the local bare repo."""
    bare = git(clone, "remote", "get-url", "origin")
    git(clone, "remote", "set-url", "origin", "https://github.com/benpshore/proj.git")
    git(clone, "config", "url." + bare + ".insteadOf", "https://github.com/benpshore/proj.git")


def test_dry_run_json_report(tmp_path, origin, clone, capsys):
    point_origin_at_github(clone)
    (clone / "a.txt").write_text("edited\n")
    (tmp_path / "code" / "plain").mkdir()
    git(tmp_path / "code" / "plain", "init", "-q")
    runner = FakeRunner(gh_replies())

    code = cli.main(["--root", str(tmp_path / "code"), "--dry-run", "--json"], runner=runner)

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["user"] == "benpshore" and report["dry_run"] is True
    repos = {r["name"]: r for r in report["repos"]}
    assert set(repos) == {"benpshore/proj", "benpshore/other", "plain"}
    proj = repos["benpshore/proj"]
    assert proj["clones"][0]["status"] == "dirty"
    assert proj["remote"]["ci"] == "failure"
    assert any(item.startswith("CI failure on main") for item in proj["attention"])
    assert any("PR #7 by dependabot [needs review]" in item for item in proj["attention"])
    assert repos["plain"]["github"] is False and repos["plain"]["remote"] is None
    assert repos["benpshore/other"]["clones"] == [] and repos["benpshore/other"]["attention"] == []
    assert report["summary"]["ci_failing"] == 1
    assert report["summary"]["prs_needing_review"] == 1
    assert report["summary"]["not_cloned"] == 1
    assert report["repos"][0]["name"] == "benpshore/proj"  # attention first
    # Dry run: no fetch/pull, no gh repo sync, only read-only gh calls.
    assert not {"fetch", "pull"} & set(runner.git_subcommands())
    assert {tuple(c[1:3]) for c in runner.calls if c[0] == "gh"} <= {
        ("api", "user"),
        ("api", "--paginate"),
        ("search", "prs"),
        ("api", "graphql"),
    }


def test_human_summary_and_real_run(tmp_path, origin, clone, capsys):
    point_origin_at_github(clone)
    new_head = origin.advance()
    runner = FakeRunner(gh_replies())

    code = cli.main(["--root", str(tmp_path / "code")], runner=runner)

    out = capsys.readouterr().out
    assert code == 0
    assert git(clone, "rev-parse", "HEAD") == new_head
    assert "Clones: 1 updated" in out
    assert "Needs attention (1):" in out
    assert "CI failure on main (abcdef1)" in out
    assert "Not cloned, nothing open (1): benpshore/other" in out


def test_someone_elses_clone_only_shows_my_prs(tmp_path, origin, clone, capsys):
    git(clone, "remote", "set-url", "origin", "git@github.com:stranger/tool.git")
    payload = status_payload("stranger/tool", ci="FAILURE", prs=[pr(1, "dependabot"), pr(2)])
    replies = gh_replies() | {("api", "graphql"): lambda cmd: Result(0, payload)}
    runner = FakeRunner(replies)
    cli.main(["--root", str(tmp_path / "code"), "--dry-run", "--json"], runner=runner)
    tool = next(
        r for r in json.loads(capsys.readouterr().out)["repos"] if r["name"] == "stranger/tool"
    )
    assert tool["sources"] == ["local"]
    assert [p["number"] for p in tool["remote"]["prs"]] == [2]
    assert tool["attention"] == ["PR #2 [mine]: PR 2"]


def test_gh_unavailable_still_reports_local(tmp_path, clone, capsys):
    runner = FakeRunner({("api", "user"): Result(4, "", "gh auth login required")})
    code = cli.main(["--root", str(tmp_path / "code"), "--dry-run"], runner=runner)
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


def test_roots_from_environment(monkeypatch):
    monkeypatch.setenv("SYNCGHREPO_ROOTS", "/a:/b")
    assert cli.default_roots() == ["/a", "/b"]
    monkeypatch.delenv("SYNCGHREPO_ROOTS")
    assert cli.default_roots() == ["~/code"]
