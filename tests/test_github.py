import json

import pytest
from conftest import FakeRunner

from syncghrepo import github
from syncghrepo.run import Result


def repo_line(name, *, fork=False, archived=False, owner_type="User"):
    return json.dumps(
        {
            "name": name,
            "url": f"https://github.com/{name}",
            "fork": fork,
            "archived": archived,
            "default_branch": "main",
            "owner": name.split("/")[0],
            "owner_type": owner_type,
        }
    )


def status_payload(name="benpshore/proj", *, ci="SUCCESS", prs=(), issues=(), fork=False):
    return json.dumps(
        {
            "data": {
                "repository": {
                    "nameWithOwner": name,
                    "url": f"https://github.com/{name}",
                    "isFork": fork,
                    "isArchived": False,
                    "parent": {"nameWithOwner": "up/proj"} if fork else None,
                    "defaultBranchRef": {
                        "name": "main",
                        "target": {"oid": "abcdef1234567", "statusCheckRollup": {"state": ci}},
                    },
                    "pullRequests": {"totalCount": len(prs), "nodes": list(prs)},
                    "issues": {"totalCount": len(issues), "nodes": list(issues)},
                }
            }
        }
    )


def pr(number, author="benpshore", *, draft=False, decision=None, requested=()):
    return {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/x/y/pull/{number}",
        "isDraft": draft,
        "updatedAt": "2026-10-19T00:00:00Z",
        "reviewDecision": decision,
        "author": {"login": author} if author else None,
        "reviewRequests": {"nodes": [{"requestedReviewer": {"login": r}} for r in requested]},
    }


def test_discover_merges_owned_org_collab_and_contributed():
    lines = "\n".join(
        [
            repo_line("benpshore/proj"),
            repo_line("benpshore/fork", fork=True),
            repo_line("caligoapp/app", owner_type="Organization"),
            repo_line("friend/shared"),
        ]
    )
    runner = FakeRunner(
        {
            ("api", "--paginate"): Result(0, lines + "\n"),
            ("search", "prs"): Result(0, "benpshore/proj\nupstream/lib\nupstream/lib\n"),
        }
    )

    repos, errors = github.discover(runner, "BenPShore")

    assert errors == []
    assert {k: v["sources"] for k, v in repos.items()} == {
        "benpshore/proj": ["owner", "contributed"],
        "benpshore/fork": ["owner"],
        "caligoapp/app": ["org"],
        "friend/shared": ["collaborator"],
        "upstream/lib": ["contributed"],
    }
    assert repos["benpshore/fork"]["fork"] is True
    assert repos["upstream/lib"]["fork"] is None


def test_discover_keeps_going_when_one_query_fails():
    runner = FakeRunner(
        {
            ("api", "--paginate"): Result(1, "", "HTTP 502: Bad Gateway"),
            ("search", "prs"): Result(0, "upstream/lib\n"),
        }
    )
    repos, errors = github.discover(runner, "benpshore")
    assert list(repos) == ["upstream/lib"]
    assert errors == ["listing repos: gh api --paginate: HTTP 502: Bad Gateway"]


def test_repo_status_classifies_prs_ci_and_issues():
    prs = [
        pr(1),  # mine, open
        pr(2, draft=True),  # my draft
        pr(3, "dependabot", decision=None),  # someone else's: needs review
        pr(4, "friend", decision="APPROVED"),  # approved: does not need review
        pr(5, "friend", decision="APPROVED", requested=["benpshore"]),  # asked me again
        pr(6, None, draft=True),  # deleted author, draft
    ]
    issues = [{"number": 9, "title": "Fix it", "url": "u", "updatedAt": "t"}]
    runner = FakeRunner(
        {("api", "graphql"): Result(0, status_payload(ci="FAILURE", prs=prs, issues=issues))}
    )

    status = github.repo_status(runner, "benpshore/proj", "benpshore")

    by_number = {p["number"]: p for p in status["prs"]}
    assert [n for n, p in by_number.items() if p["needs_review"]] == [3, 5]
    assert [n for n, p in by_number.items() if p["draft"]] == [2, 6]
    assert [n for n, p in by_number.items() if p["mine"]] == [1, 2]
    assert by_number[6]["author"] == "ghost"
    assert status["ci"] == "failure" and status["ci_commit"] == "abcdef1"
    assert status["open_prs"] == 6
    assert status["assigned_issues"] == [
        {"number": 9, "title": "Fix it", "url": "u", "updated_at": "t"}
    ]
    sent = runner.calls[0]
    assert "owner=benpshore" in sent and "name=proj" in sent and "me=benpshore" in sent


def test_repo_status_for_contributed_repo_keeps_only_my_prs():
    prs = [pr(1), pr(2, "stranger"), pr(3, "stranger", requested=["benpshore"])]
    runner = FakeRunner({("api", "graphql"): Result(0, status_payload("up/lib", prs=prs))})
    status = github.repo_status(runner, "up/lib", "benpshore", all_prs=False)
    assert [p["number"] for p in status["prs"]] == [1, 3]


def test_repo_status_raises_on_gh_failure():
    runner = FakeRunner({("api", "graphql"): Result(1, "", "Could not resolve to a Repository")})
    with pytest.raises(github.GhError, match="Could not resolve"):
        github.repo_status(runner, "benpshore/gone", "benpshore")
