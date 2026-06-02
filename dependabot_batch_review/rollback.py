"""
Auto-rollback by reverting a merged Dependabot commit.

Primary mechanism: a local ``git revert`` (matches the subprocess usage already in
``review.py``), which produces a correct revert for both merge commits
(``-m 1``) and squash/rebase single-parent commits. The revert is pushed to a new
branch and opened as a PR that is auto-merged, re-deploying clean code.

Idempotent (keyed by the revert branch name) and dry-run aware: in dry-run it does
all the reads but performs no branch push / PR creation / merge.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any

from .github_client import GitHubClient
from .review import merge_pr

# Matches the credential in an authenticated clone URL so it never reaches logs.
_TOKEN_RE = re.compile(r"x-access-token:[^@/\s]+@")


def _redact(text: str) -> str:
    return _TOKEN_RE.sub("x-access-token:***@", text)


_REPO_QUERY = """
query($owner: String!, $name: String!, $branch: String!) {
  repository(owner: $owner, name: $name) {
    id
    defaultBranchRef { name }
    pullRequests(headRefName: $branch, first: 1, states: [OPEN, MERGED]) {
      nodes { url merged }
    }
  }
}
"""

_CREATE_PR = """
mutation($input: CreatePullRequestInput!) {
  createPullRequest(input: $input) { pullRequest { id url } }
}
"""


@dataclass
class RollbackResult:
    performed: bool
    revert_pr_url: str | None
    revert_commit_sha: str | None
    reason: str
    dry_run: bool


def _run(cmd: list[str], cwd: str) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        # Redact the embedded clone token from both the command and stderr so it
        # can't leak into exception messages / logs / Slack on the fallback path.
        command = _redact(" ".join(cmd))
        stderr = _redact(result.stderr.strip())
        raise RuntimeError(f"`{command}` failed: {stderr}")
    return result.stdout.strip()


def _existing_revert(
    gh: GitHubClient, owner: str, repo: str, branch: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    result = gh.query(
        _REPO_QUERY, variables={"owner": owner, "name": repo, "branch": branch}
    )
    repository: dict[str, Any] = (result or {}).get("repository") or {}
    nodes = (repository.get("pullRequests") or {}).get("nodes") or []
    existing: dict[str, Any] | None = nodes[0] if nodes else None
    return repository, existing


def revert_merge(
    gh: GitHubClient,
    owner: str,
    repo: str,
    merge_commit_sha: str,
    *,
    original_title: str = "",
    original_pr_url: str = "",
    auto_merge: bool = True,
    dry_run: bool = False,
    merge_method: str = "SQUASH",
) -> RollbackResult:
    """Revert ``merge_commit_sha`` on ``owner/repo`` via a new auto-merged PR."""
    short = merge_commit_sha[:7]
    branch = f"revert-dependabot-{short}"

    repository, existing_pr = _existing_revert(gh, owner, repo, branch)
    if existing_pr is not None:
        return RollbackResult(
            performed=bool(existing_pr.get("merged")),
            revert_pr_url=str(existing_pr.get("url")),
            revert_commit_sha=None,
            reason="revert PR already exists (idempotent)",
            dry_run=dry_run,
        )

    if dry_run:
        return RollbackResult(
            performed=False,
            revert_pr_url=None,
            revert_commit_sha=None,
            reason=f"dry-run: would `git revert` {short} on {owner}/{repo}",
            dry_run=True,
        )

    repo_id = repository.get("id")
    base = (repository.get("defaultBranchRef") or {}).get("name") or "main"
    if not isinstance(repo_id, str):
        return _manual_fallback(
            owner, repo, merge_commit_sha, "could not resolve repo id"
        )

    try:
        revert_sha = _local_revert(gh, owner, repo, base, branch, merge_commit_sha)
    except Exception as exc:  # noqa: BLE001 - fall back to a manual instruction
        return _manual_fallback(owner, repo, merge_commit_sha, str(exc))

    title = f'Revert "{original_title}" (auto-rollback)'.strip()
    body = (
        f"Automated rollback of {original_pr_url or merge_commit_sha}.\n\n"
        f"The merge `{short}` degraded production health, so it was reverted "
        f"automatically. Investigate before re-attempting."
    )
    created = gh.query(
        _CREATE_PR,
        variables={
            "input": {
                "repositoryId": repo_id,
                "baseRefName": base,
                "headRefName": branch,
                "title": title,
                "body": body,
            }
        },
    )
    pr = (((created or {}).get("createPullRequest") or {}).get("pullRequest")) or {}
    pr_id = pr.get("id")
    pr_url = pr.get("url")

    if auto_merge and isinstance(pr_id, str):
        try:
            merge_pr(gh, pr_id=pr_id, merge_method=merge_method)
        except Exception as exc:  # noqa: BLE001 - PR is open; a human can merge it
            return RollbackResult(
                performed=False,
                revert_pr_url=pr_url,
                revert_commit_sha=revert_sha,
                reason=f"revert PR opened but auto-merge failed: {exc!r}",
                dry_run=False,
            )

    return RollbackResult(
        performed=True,
        revert_pr_url=pr_url,
        revert_commit_sha=revert_sha,
        reason="reverted and merged",
        dry_run=False,
    )


def _local_revert(
    gh: GitHubClient, owner: str, repo: str, base: str, branch: str, sha: str
) -> str:
    """Clone, revert (merge-aware), and push the revert branch. Returns its SHA."""
    token = gh.token
    url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
    with tempfile.TemporaryDirectory() as workdir:
        _run(["git", "clone", "--depth", "50", "--branch", base, url, "."], workdir)
        _run(["git", "config", "user.name", "dependabot-automerge[bot]"], workdir)
        _run(
            [
                "git",
                "config",
                "user.email",
                "dependabot-automerge[bot]@users.noreply.github.com",
            ],
            workdir,
        )
        _run(["git", "checkout", "-b", branch], workdir)

        parents = _run(
            ["git", "rev-list", "--parents", "-n", "1", sha], workdir
        ).split()
        is_merge_commit = len(parents) > 2
        revert_cmd = ["git", "revert", "--no-edit"]
        if is_merge_commit:
            revert_cmd += ["-m", "1"]
        revert_cmd.append(sha)
        _run(revert_cmd, workdir)

        revert_sha = _run(["git", "rev-parse", "HEAD"], workdir)
        _run(["git", "push", "origin", branch], workdir)
        return revert_sha


def _manual_fallback(owner: str, repo: str, sha: str, why: str) -> RollbackResult:
    short = sha[:7]
    return RollbackResult(
        performed=False,
        revert_pr_url=None,
        revert_commit_sha=None,
        reason=(
            f"automated revert failed ({why}). Manual: "
            f"`git revert -m 1 {short}` in {owner}/{repo}, or roll back the "
            f"Elastic Beanstalk environment via redeploy.yml (operation: redeploy)."
        ),
        dry_run=False,
    )
