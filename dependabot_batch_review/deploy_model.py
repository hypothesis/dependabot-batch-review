"""
Per-repo deploy-coupling model.

Determines whether merging a Dependabot PR will trigger a *production* deploy, by
fetching and parsing the repo's ``.github/workflows/deploy.yml`` once (memoized)
and applying its ``paths-ignore`` semantics to the file(s) the PR is expected to
touch.

This is the backbone of the Tier-0 / Tier-1 split: a bump that provably does not
deploy is safe to auto-merge (Tier 0); a bump that deploys to production must pass
the post-deploy health gate (Tier 1).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Any

import yaml

from .github_client import GitHubClient
from .review import DependencyUpdatePR

_DEPLOY_FILE_QUERY = """
query($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    object(expression: "HEAD:.github/workflows/deploy.yml") {
      ... on Blob { text }
    }
  }
}
"""

_DEPLOY_BRANCHES = frozenset({"main", "master"})


def _glob_to_regex(pattern: str) -> str:
    """
    Translate a GitHub Actions path filter glob to a regex.

    Mirrors GitHub's semantics: ``*`` matches any character except ``/``; ``**``
    matches any character including ``/``; ``?`` matches a single non-``/`` char.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern[i + 1 : i + 2] == "*":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


def _path_matches(path: str, glob: str) -> bool:
    return re.fullmatch(_glob_to_regex(glob), path) is not None


@dataclass
class DeployModel:
    """The deploy behaviour of one repository's default branch."""

    repo: str
    is_deploying_service: bool
    deploy_branches: list[str] = field(default_factory=list)
    paths_ignore: list[str] = field(default_factory=list)
    raw_present: bool = False

    def path_triggers_deploy(self, changed_path: str) -> bool:
        """
        Apply GitHub's ``paths-ignore`` semantics to a single changed path.

        A path triggers the deploy unless it matches an ignore glob. Negated globs
        (``!foo``) re-include a path; patterns are evaluated in order and the last
        matching pattern wins (mirroring GitHub's filter evaluation).
        """
        if not self.is_deploying_service:
            return False
        if not self.paths_ignore:
            return True

        triggers = True
        for pattern in self.paths_ignore:
            negated = pattern.startswith("!")
            glob = pattern[1:] if negated else pattern
            if _path_matches(changed_path, glob):
                triggers = negated
        return triggers

    def _prod_requirement_files(self) -> list[str]:
        """Files re-included by a ``!`` negation are the repo's prod lock files."""
        return [p[1:] for p in self.paths_ignore if p.startswith("!")]

    def infer_changed_paths(
        self, pr: DependencyUpdatePR, is_dev_tool: bool
    ) -> list[str]:
        """
        Best-effort map a PR's ecosystem + branch to representative changed paths.

        The exact prod requirements filename varies per repo (``prod.txt`` vs
        ``requirements.txt``); we read it from the deploy.yml ``!`` negation when
        present, falling back to common defaults.
        """
        ecosystem = pr.package_type
        if ecosystem == "docker":
            return ["Dockerfile"]
        if ecosystem == "npm_and_yarn":
            return ["package.json"]
        if ecosystem == "github_actions":
            return [".github/workflows/ci.yml"]
        if ecosystem == "pip":
            if is_dev_tool:
                return ["requirements/dev.txt"]
            prod_files = self._prod_requirement_files()
            return prod_files or ["requirements/prod.txt", "requirements.txt"]
        # Unknown ecosystem: be conservative and assume it could deploy.
        return ["requirements.txt"]

    def deploys_to_prod(self, pr: DependencyUpdatePR, is_dev_tool: bool) -> bool:
        """Does merging this PR trigger a production deploy of this repo?"""
        if not self.is_deploying_service:
            return False
        if pr.package_type == "github_actions":
            # Workflow-file bumps never change the deployed artifact.
            return False
        # Prefer the PR's real changed files; the ecosystem inference is only a
        # fallback for PRs fetched without the files connection.
        paths = pr.changed_files or self.infer_changed_paths(pr, is_dev_tool)
        return any(self.path_triggers_deploy(path) for path in paths)


def parse_deploy_yaml(repo: str, text: str | None) -> DeployModel:
    """
    Build a :class:`DeployModel` from the raw text of a ``deploy.yml`` file.

    ``text`` is ``None`` when the repo has no ``deploy.yml`` (a non-deploying
    library/tooling repo). If the file exists but cannot be parsed we fail *safe*:
    treat the repo as deploying with no path filter, so bumps land in Tier 1
    (health-gated) rather than being wrongly auto-merged as Tier 0.
    """
    if text is None:
        return DeployModel(repo=repo, is_deploying_service=False, raw_present=False)

    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        print(f"deploy.yml parse error for {repo}: {exc}", file=sys.stderr)
        return DeployModel(repo=repo, is_deploying_service=True, raw_present=True)

    if not isinstance(data, dict):
        return DeployModel(repo=repo, is_deploying_service=True, raw_present=True)

    # YAML 1.1 parses the bare key `on:` as the boolean True, so check both.
    on_section = data.get("on", data.get(True))

    # A push trigger can be spelled `on: push`, `on: [push]`, a bare `push:` key,
    # or a `push:` mapping. All forms without a `branches:` filter fire on every
    # branch — including main — so they must count as deploying (fail-safe).
    push: Any = None
    push_present = False
    if isinstance(on_section, str):
        push_present = on_section == "push"
    elif isinstance(on_section, list):
        push_present = "push" in on_section
    elif isinstance(on_section, dict):
        push_present = "push" in on_section
        push = on_section.get("push")

    branches: list[str] = []
    paths_ignore: list[str] = []
    if isinstance(push, dict):
        raw_branches = push.get("branches") or []
        if isinstance(raw_branches, list):
            branches = [str(b) for b in raw_branches]
        raw_ignore = push.get("paths-ignore") or []
        if isinstance(raw_ignore, list):
            paths_ignore = [str(p) for p in raw_ignore]

    if push_present and not branches:
        deploying = True
    else:
        deploying = any(b in _DEPLOY_BRANCHES for b in branches)
    return DeployModel(
        repo=repo,
        is_deploying_service=deploying,
        deploy_branches=branches,
        paths_ignore=paths_ignore,
        raw_present=True,
    )


class DeployModelCache:
    """Fetches and memoizes the :class:`DeployModel` for each repo in an org."""

    def __init__(
        self,
        gh: GitHubClient,
        organization: str,
        publish_on_merge_repos: list[str] | None = None,
    ) -> None:
        self._gh = gh
        self._organization = organization
        self._publish_on_merge = frozenset(publish_on_merge_repos or [])
        self._cache: dict[str, DeployModel] = {}

    def get(self, repo: str) -> DeployModel:
        if repo not in self._cache:
            if repo in self._publish_on_merge:
                # npm-publishing libs ship on merge despite having no deploy.yml;
                # treat every bump as production-deploying (Tier 1).
                self._cache[repo] = DeployModel(
                    repo=repo, is_deploying_service=True, raw_present=False
                )
            else:
                self._cache[repo] = self._fetch(repo)
        return self._cache[repo]

    def _fetch(self, repo: str) -> DeployModel:
        try:
            result = self._gh.query(
                _DEPLOY_FILE_QUERY,
                variables={"owner": self._organization, "repo": repo},
            )
        except Exception as exc:  # network / permissions -> fail safe (deploying)
            print(f"deploy.yml fetch failed for {repo}: {exc}", file=sys.stderr)
            return DeployModel(repo=repo, is_deploying_service=True, raw_present=False)

        obj = (result or {}).get("repository", {}).get("object")
        text = obj.get("text") if isinstance(obj, dict) else None
        return parse_deploy_yaml(repo, text)
