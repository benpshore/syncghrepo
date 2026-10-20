"""syncghrepo: keep all work in your local clones saved and in sync, and list
what is still open.

For every clone under the roots: auto-save uncommitted work as a WIP commit
(on a wip/<date> branch when on the default branch; secrets, .env, ignored and
huge files are held back), gh repo sync forks, git pull --rebase --autostash
with trivial conflicts auto-resolved (anything else is aborted and reported),
then push (never the default branch). Then report open PRs, review requests,
failing CI, assigned issues, unpushed/stale branches and stashes.

Dry run by default: nothing is written until you pass --apply.

Exit status: 0 all clones saved and synced, 1 something needs a person
(conflict, error, held-back file, GitHub unreachable), 2 bad arguments."""

import argparse
import json
import os
import time
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import github, local, sync
from .run import Runner, make_runner

PROBLEMS = ("conflict", "error", "held-back")
FAILING_CI = ("failure", "error")
INVOLVED = {"owner", "org", "collaborator"}  # else: only contributed to, or just cloned
SHOWN_PER_REPO = 12


def involved(entry: dict) -> bool:
    """Whether all of the repo's PRs and its CI are your business, not just your own PRs."""
    return bool(INVOLVED & set(entry["sources"]))


def tool_version() -> str:
    try:
        return version("syncghrepo")
    except PackageNotFoundError:
        return "0.0.0"


def default_roots() -> list[str]:
    env = os.environ.get("SYNCGHREPO_ROOTS", "")
    return [root for root in env.split(os.pathsep) if root] or ["~/code"]


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="syncghrepo",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="apply",
        action="store_false",
        help="(default) show exactly what would be committed, pushed and resolved; write nothing",
    )
    mode.add_argument("--apply", action="store_true", help="really commit, sync, resolve and push")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="dry run only: git fetch first (updates remote-tracking refs, nothing else)",
    )
    parser.add_argument(
        "--root",
        action="append",
        metavar="DIR",
        help="clone, or folder of clones; repeatable (default: $SYNCGHREPO_ROOTS or ~/code)",
    )
    parser.add_argument("--depth", type=int, default=3, help="how deep to search roots (3)")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument(
        "--no-github",
        action="store_true",
        help="no gh calls: local work only, and nothing is pushed",
    )
    parser.add_argument(
        "--stale-days", type=int, default=30, help="branch age that counts as stale (30)"
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds per command (120)")
    parser.add_argument("--jobs", type=int, default=8, help="repos processed in parallel (8)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {tool_version()}")
    parser.set_defaults(apply=False)  # dry run unless --apply, whatever argparse infers
    return parser.parse_args(argv)


def clone_items(clone: dict) -> list[str]:
    """Problems and local leftovers in one clone."""
    where = local.short_path(clone["path"])
    items = []
    if clone["status"] in ("skipped", *PROBLEMS):
        items.append(f"{where}: {clone['status']}: {clone['note']}")
    for action in clone["actions"]:
        if action["ok"] is False:
            items.append(f"{action['cmd']} failed: {action['note']}")
        elif action["cmd"].endswith("(skipped)"):
            items.append(f"not pushed: {action['note']}")
    if clone["upstream_gone"] and clone["status"] != "skipped":
        items.append(f"{clone['branch']}: upstream branch was deleted (merged?)")
    for held in clone["held_back"]:
        items.append(f"held back {held['path']}: {held['reason']}")
    for conflict in clone["conflicts"]:
        extra = f" ({conflict['error']})" if conflict.get("error") else ""
        items.append(f"conflict needs a person: {conflict['path'] or '?'}{extra}")
    if clone["backup_refs"] and clone["conflicts"]:
        items.append(f"backup: {', '.join(clone['backup_refs'])}")
    for branch in clone["branches"]:
        if branch["unpushed"] and branch["name"] != clone["branch"]:
            items.append(f"branch {branch['name']}: {branch['unpushed']} unpushed commit(s)")
        if branch["gone"]:
            items.append(f"branch {branch['name']}: upstream deleted (merged?)")
    stale = [b["name"] for b in clone["branches"] if b["stale"]]
    if stale:
        items.append(f"{len(stale)} stale branch(es): {', '.join(stale)}")
    if clone["stashes"]:
        items.append(f"{len(clone['stashes'])} stash(es): {clone['stashes'][0]}")
    if clone["worktrees"]:
        paths = ", ".join(local.short_path(p) for p in clone["worktrees"])
        items.append(f"{len(clone['worktrees'])} other worktree(s), not auto-saved: {paths}")
    return items


def attention(entry: dict) -> list[str]:
    """Short, human-readable list of unresolved work for one repo."""
    items = [item for clone in entry["clones"] for item in clone_items(clone)]
    remote = entry.get("remote")
    if entry.get("remote_error"):
        items.append(f"GitHub query failed: {entry['remote_error']}")
    if remote:
        if remote["ci"] in FAILING_CI and involved(entry):
            items.append(f"CI {remote['ci']} on {remote['default_branch']} ({remote['ci_commit']})")
        for pr in remote["prs"]:
            tags = [
                tag
                for tag, on in (
                    ("mine", pr["mine"]),
                    ("draft", pr["draft"]),
                    ("needs review", pr["needs_review"]),
                    ("review requested", pr["review_requested"]),
                )
                if on
            ]
            by = "" if pr["mine"] else f" by {pr['author']}"
            items.append(f"PR #{pr['number']}{by} [{', '.join(tags) or 'open'}]: {pr['title']}")
        if remote["open_prs"] > len(remote["prs"]) and involved(entry):
            items.append(f"{remote['open_prs'] - len(remote['prs'])} more open PR(s) not listed")
        for issue in remote["assigned_issues"]:
            items.append(f"issue #{issue['number']} assigned to me: {issue['title']}")
    return items


def summarize(repos: list[dict]) -> dict:
    clones = [clone for entry in repos for clone in entry["clones"]]
    prs = [pr for entry in repos if entry.get("remote") for pr in entry["remote"]["prs"]]
    return {
        "repos": len(repos),
        "on_github": sum(entry["github"] for entry in repos),
        "cloned": sum(bool(entry["clones"]) for entry in repos),
        "not_cloned": sum(
            entry["github"] and not entry["clones"] and not entry["archived"] for entry in repos
        ),
        "clones": len(clones),
        "clone_status": dict(sorted(Counter(clone["status"] for clone in clones).items())),
        "saved_files": sum(len((c["saved"] or {}).get("files", [])) for c in clones),
        "held_back_files": sum(len(clone["held_back"]) for clone in clones),
        "auto_resolved": sum(len(clone["resolved"]) for clone in clones),
        "conflicts": sum(len(clone["conflicts"]) for clone in clones),
        "open_prs": len(prs),
        "draft_prs": sum(pr["draft"] for pr in prs),
        "prs_needing_review": sum(pr["needs_review"] for pr in prs),
        "ci_failing": sum(
            bool(entry.get("remote")) and entry["remote"]["ci"] in FAILING_CI and involved(entry)
            for entry in repos
        ),
        "assigned_issues": sum(
            len(entry["remote"]["assigned_issues"]) for entry in repos if entry.get("remote")
        ),
        "stashes": sum(len(clone["stashes"]) for clone in clones),
        "unpushed_branches": sum(
            1 for clone in clones for branch in clone["branches"] if branch["unpushed"]
        ),
        "stale_branches": sum(
            1 for clone in clones for branch in clone["branches"] if branch["stale"]
        ),
        "needs_attention": sum(bool(entry["attention"]) for entry in repos),
    }


def origin_name(runner: Runner, path: Path) -> tuple[str | None, str | None]:
    """(owner/name on GitHub or None, origin URL or None) for a clone."""
    urls = [
        runner(cmd, str(path))
        for cmd in (
            ["git", "remote", "get-url", "origin"],  # after insteadOf rewrites
            ["git", "config", "--get", "remote.origin.url"],  # as written
        )
    ]
    names = [local.github_name(url.out) for url in urls if url.ok]
    url = urls[0].out.strip() if urls[0].ok else None
    return next((name for name in names if name), None), url


def build_report(args: argparse.Namespace, runner: Runner, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    roots = [Path(root).expanduser() for root in (args.root or default_roots())]
    errors: list[str] = []
    me = None
    repos: dict[str, dict] = {}
    if not args.no_github:
        try:
            me = github.whoami(runner)
        except github.GhError as err:
            errors.append(f"gh not usable, GitHub skipped and nothing pushed: {err}")
        if me:
            repos, found_errors = github.discover(runner, me)
            errors += found_errors

    clone_keys: list[tuple[Path, str]] = []
    for path in local.find_clones(roots, args.depth):
        name, url = origin_name(runner, path)
        if name:
            key = name.lower()
            repos.setdefault(key, github.new_repo(name, "local"))
        else:
            key = f"local:{path}"
            repos[key] = {
                "name": path.name,
                "url": url,
                "github": False,
                "fork": None,
                "archived": None,
                "default_branch": None,
                "sources": ["local"],
            }
        clone_keys.append((path, key))
    for entry in repos.values():
        entry.update(clones=[], remote=None, remote_error=None)

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        # GitHub first: push access and fork status decide what each clone may do.
        remote_jobs = [
            (
                key,
                pool.submit(github.repo_status, runner, entry["name"], me, all_prs=involved(entry)),
            )
            for key, entry in repos.items()
            if me and entry["github"] and not entry["archived"]
        ]
        for key, job in remote_jobs:
            entry = repos[key]
            try:
                status = job.result()
            except (github.GhError, ValueError, KeyError, TypeError) as err:
                entry["remote_error"] = str(err)
                continue
            entry["remote"] = status
            entry.update(
                fork=status["fork"],
                archived=status["archived"],
                default_branch=status["default_branch"],
            )

        def run_clone(path: Path, key: str) -> dict:
            remote = repos[key]["remote"] or {}
            can_push = bool(remote.get("can_push")) and not repos[key]["archived"]
            return sync.process(
                runner,
                path,
                can_push=can_push,
                fork=remote["name"] if remote.get("fork") else None,
                dry_run=not args.apply,
                fetch=args.fetch,
                stale_days=args.stale_days,
                now=now,
            )

        clone_jobs = [(key, pool.submit(run_clone, path, key)) for path, key in clone_keys]
        for key, job in clone_jobs:
            repos[key]["clones"].append(job.result())

    ordered = sorted(repos.values(), key=lambda entry: entry["name"].lower())
    for entry in ordered:
        entry["attention"] = attention(entry)
    ordered.sort(key=lambda entry: not entry["attention"])
    return {
        "tool": "syncghrepo",
        "version": tool_version(),
        "generated_at": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"),
        "user": me,
        "dry_run": not args.apply,
        "roots": [str(root) for root in roots],
        "summary": summarize(ordered),
        "repos": ordered,
        "errors": errors,
    }


def render(report: dict) -> str:
    """Plain-text summary for a human."""
    s = report["summary"]
    mode = "DRY RUN, nothing written (--apply to do it)" if report["dry_run"] else "applied"
    roots = ", ".join(local.short_path(root) for root in report["roots"])
    statuses = ", ".join(f"{n} {status}" for status, n in s["clone_status"].items()) or "none"
    lines = [
        f"syncghrepo {report['version']} · {report['user'] or 'no GitHub'} · "
        f"{report['generated_at']} · {mode}",
        f"{s['repos']} repos ({s['on_github']} on GitHub), {s['clones']} clone(s) under {roots}, "
        f"{s['not_cloned']} not cloned",
        f"Clones: {statuses}",
        f"Saved: {s['saved_files']} file(s), {s['held_back_files']} held back; "
        f"conflicts: {s['auto_resolved']} auto-resolved, {s['conflicts']} need a person",
        f"Work: {s['open_prs']} open PR(s) ({s['draft_prs']} draft, "
        f"{s['prs_needing_review']} need review), {s['ci_failing']} failing CI, "
        f"{s['assigned_issues']} assigned issue(s), {s['unpushed_branches']} unpushed "
        f"branch(es), {s['stale_branches']} stale branch(es), {s['stashes']} stash(es)",
    ]
    clones = [c for e in report["repos"] for c in e["clones"] if c["actions"]]
    if clones:
        lines += ["", "Would do:" if report["dry_run"] else "Done:"]
        for clone in sorted(clones, key=lambda c: c["path"]):
            lines.append(f"  {local.short_path(clone['path'])} ({clone['status']})")
            for action in clone["actions"]:
                mark = {True: "ok", False: "FAILED", None: "-"}[action["ok"]]
                note = f"  # {action['note']}" if action["note"] else ""
                lines.append(f"    {mark if not report['dry_run'] else '-'} {action['cmd']}{note}")
            saved = clone["saved"]
            if saved:
                shown = ", ".join(saved["files"][:8]) + (" ..." if len(saved["files"]) > 8 else "")
                lines.append(f"      files: {shown}")
            for item in clone["resolved"]:
                regen = f", then {item['regenerate']}" if item.get("regenerate") else ""
                lines.append(f"      resolve {item['path']}: {item['how']}{regen}")
    flagged = [entry for entry in report["repos"] if entry["attention"]]
    if flagged:
        lines += ["", f"Needs attention ({len(flagged)}):"]
        for entry in flagged:
            where = "; ".join(
                f"{local.short_path(c['path'])} on {c['branch'] or 'detached'}, {c['status']}"
                for c in entry["clones"]
            )
            lines.append(f"  {entry['name']}" + (f"  ({where})" if where else "  (not cloned)"))
            lines += [f"    - {item}" for item in entry["attention"][:SHOWN_PER_REPO]]
            if len(entry["attention"]) > SHOWN_PER_REPO:
                more = len(entry["attention"]) - SHOWN_PER_REPO
                lines.append(f"    - ... and {more} more (see --json)")
    clean = [e["name"] for e in report["repos"] if e["clones"] and not e["attention"]]
    if clean:
        lines += ["", f"Clean ({len(clean)}): {', '.join(clean)}"]
    missing = [
        e["name"]
        for e in report["repos"]
        if e["github"] and not e["clones"] and not e["attention"] and not e["archived"]
    ]
    if missing:
        lines += ["", f"Not cloned, nothing open ({len(missing)}): {', '.join(missing)}"]
    if report["errors"]:
        lines += ["", "Errors:"] + [f"  - {error}" for error in report["errors"]]
    return "\n".join(lines)


def exit_code(report: dict) -> int:
    clones = [clone for entry in report["repos"] for clone in entry["clones"]]
    return 1 if report["errors"] or any(c["status"] in PROBLEMS for c in clones) else 0


def main(argv: Sequence[str] | None = None, runner: Runner | None = None) -> int:
    args = parse_args(argv)
    report = build_report(args, runner or make_runner(args.timeout))
    print(json.dumps(report, indent=2) if args.json else render(report))
    return exit_code(report)
