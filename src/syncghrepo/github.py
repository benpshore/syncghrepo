"""GitHub side: which repos you have, and the open work in each. Read-only."""

import json

from .run import Runner

USER_REPOS = "user/repos?affiliation=owner,collaborator,organization_member&per_page=100"
REPO_FIELDS = (
    ".[] | {name: .full_name, url: .html_url, fork, archived, default_branch,"
    " owner: .owner.login, owner_type: .owner.type}"
)
STATUS_QUERY = """
query($owner: String!, $name: String!, $me: String!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner url isFork isArchived
    parent { nameWithOwner }
    defaultBranchRef { name target { ... on Commit { oid statusCheckRollup { state } } } }
    pullRequests(states: OPEN, first: 50, orderBy: {field: UPDATED_AT, direction: DESC}) {
      totalCount
      nodes {
        number title url isDraft updatedAt reviewDecision author { login }
        reviewRequests(first: 20) { nodes { requestedReviewer { ... on User { login } } } }
      }
    }
    issues(states: OPEN, first: 50, filterBy: {assignee: $me}) {
      totalCount
      nodes { number title url updatedAt }
    }
  }
}
"""


class GhError(RuntimeError):
    pass


def gh(runner: Runner, *args: str) -> str:
    result = runner(["gh", *args], None)
    if not result.ok:
        raise GhError(f"gh {' '.join(args[:2])}: {result.last_line()}")
    return result.out


def whoami(runner: Runner) -> str:
    return gh(runner, "api", "user", "--jq", ".login").strip()


def new_repo(name: str, source: str | None = None) -> dict:
    return {
        "name": name,
        "url": f"https://github.com/{name}",
        "github": True,
        "fork": None,
        "archived": None,
        "default_branch": None,
        "sources": [source] if source else [],
    }


def discover(runner: Runner, me: str) -> tuple[dict[str, dict], list[str]]:
    """Repos you own, your orgs' repos, repos you collaborate on, and repos you
    opened PRs in. Keyed by lowercase owner/name."""
    repos: dict[str, dict] = {}
    errors: list[str] = []
    try:
        for line in gh(runner, "api", "--paginate", USER_REPOS, "--jq", REPO_FIELDS).splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            if data["owner"].lower() == me.lower():
                source = "owner"
            elif data["owner_type"] == "Organization":
                source = "org"
            else:
                source = "collaborator"
            entry = new_repo(data["name"], source)
            entry.update(
                url=data["url"],
                fork=data["fork"],
                archived=data["archived"],
                default_branch=data["default_branch"],
            )
            repos[data["name"].lower()] = entry
    except (GhError, ValueError, KeyError) as err:
        errors.append(f"listing repos: {err}")
    try:
        found = gh(
            runner,
            "search",
            "prs",
            "--author=@me",
            "--limit=1000",
            "--json=repository",
            "--jq=.[].repository.nameWithOwner",
        )
        for name in sorted(set(found.split())):
            entry = repos.setdefault(name.lower(), new_repo(name))
            if "contributed" not in entry["sources"]:
                entry["sources"].append("contributed")
    except GhError as err:
        errors.append(f"searching PRs: {err}")
    return repos, errors


def repo_status(runner: Runner, name: str, me: str, *, all_prs: bool = True) -> dict:
    """Open PRs, CI on the default branch, and issues assigned to me.

    With all_prs False (repos I only contributed to), keep just my PRs and PRs
    that request my review.
    """
    owner, repo = name.split("/", 1)
    raw = gh(
        runner,
        "api",
        "graphql",
        "-f",
        f"query={STATUS_QUERY}",
        "-f",
        f"owner={owner}",
        "-f",
        f"name={repo}",
        "-f",
        f"me={me}",
    )
    data = (json.loads(raw).get("data") or {}).get("repository")
    if not data:
        raise GhError(f"{name}: not found")
    ref = data.get("defaultBranchRef") or {}
    commit = ref.get("target") or {}
    rollup = commit.get("statusCheckRollup") or {}

    prs = []
    for node in data["pullRequests"]["nodes"]:
        author = (node.get("author") or {}).get("login") or "ghost"
        requested = any(
            ((req.get("requestedReviewer") or {}).get("login") or "").lower() == me.lower()
            for req in node["reviewRequests"]["nodes"]
        )
        mine = author.lower() == me.lower()
        draft = bool(node["isDraft"])
        if not (all_prs or mine or requested):
            continue
        prs.append(
            {
                "number": node["number"],
                "title": node["title"],
                "url": node["url"],
                "author": author,
                "mine": mine,
                "draft": draft,
                "review_decision": node.get("reviewDecision"),
                "review_requested": requested,
                "needs_review": not draft
                and (requested or (not mine and node.get("reviewDecision") != "APPROVED")),
                "updated_at": node["updatedAt"],
            }
        )

    return {
        "name": data["nameWithOwner"],
        "url": data["url"],
        "fork": data["isFork"],
        "archived": data["isArchived"],
        "parent": (data.get("parent") or {}).get("nameWithOwner"),
        "default_branch": ref.get("name"),
        "ci": (rollup.get("state") or "").lower() or None,
        "ci_commit": (commit.get("oid") or "")[:7] or None,
        "open_prs": data["pullRequests"]["totalCount"],
        "prs": prs,
        "assigned_issues": [
            {k: node[k] for k in ("number", "title", "url")} | {"updated_at": node["updatedAt"]}
            for node in data["issues"]["nodes"]
        ],
        "assigned_issues_total": data["issues"]["totalCount"],
    }
