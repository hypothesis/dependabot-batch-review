from argparse import ArgumentParser
from dataclasses import dataclass
from enum import Enum
import json
import re
import os
from typing import Any
import subprocess
import sys

import requests


class GitHubClient:
    """
    Client for GitHub's GraphQL API.

    See https://docs.github.com/en/graphql.
    """

    def __init__(self, token: str):
        self.token = token
        self.endpoint = "https://api.github.com/graphql"

    def query(self, query: str, variables: dict[str, Any] = {}) -> Any:
        data = {"query": query, "variables": variables}
        result = requests.post(
            url=self.endpoint,
            headers={"Authorization": f"Bearer {self.token}"},
            data=json.dumps(data),
        )
        body = result.json()
        result.raise_for_status()
        if "errors" in body:
            errors = body["errors"]
            raise Exception(f"Query failed: {json.dumps(errors)}")
        return body["data"]


class CheckStatus(Enum):
    """
    Summary of the results of an automated check suite.

    See https://docs.github.com/en/graphql/reference/objects#statuscheckrollup
    and https://docs.github.com/en/graphql/reference/enums#statusstate.
    """

    SUCCESS = 1
    FAILED = 2
    MISSING = 3
    PENDING = 4

    @property
    def description(self):
        return check_status_descriptions[self]


check_status_descriptions = {
    CheckStatus.SUCCESS: "passed",
    CheckStatus.FAILED: "failed",
    CheckStatus.MISSING: "missing",
    CheckStatus.PENDING: "pending",
}


@dataclass
class DependencyUpdatePR:
    id: str
    """ID of the pull request"""

    dependency: str
    """Name of the dependency being updated"""

    from_version: str
    """The version of the dependency that the PR is updating from"""

    to_version: str
    """The version of the dependency that the PR updates to"""

    notes: str
    """Release notes from the body of the PR description"""

    url: str
    """URL of the pull request on GitHub"""

    approved: bool
    """Whether this PR has been given an approving review"""

    check_status: CheckStatus
    """The status of automated checks for this commit (eg. CI)"""

    merge_method: str
    """The preferred merge method for this PR"""


def parse_dependabot_pr_title(title: str) -> tuple[str, str, str]:
    """Extract package and version info from a Dependabot PR."""

    title_re = r"Bump (\S+) from (\S+) to (\S+)"
    fields_match = re.search(title_re, title, re.IGNORECASE)
    if not fields_match:
        raise ValueError(f"Failed to parse tile '{title}'")
    dependency, from_version, to_version = fields_match.groups()
    return (dependency, from_version, to_version)


def fetch_dependency_prs(
    gh: GitHubClient, organization: str, label="dependencies"
) -> list[DependencyUpdatePR]:
    dependencies_query = """
    query($query: String!) {
      search(type:ISSUE, query: $query, first:100) {
        issueCount
        nodes {
          ... on PullRequest {
            repository {
              name
              viewerDefaultMergeMethod
            }

            author { login }
            id
            title
            bodyText
            reviewDecision
            url

            commits (last:1) {
              nodes {
                commit {
                  statusCheckRollup {
                    state
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    query = f"org:{organization} label:{label} is:pr is:open author:app/dependabot"
    result = gh.query(query=dependencies_query, variables={"query": query})
    pull_requests = result["search"]["nodes"]

    updates: list[DependencyUpdatePR] = []
    for pr in pull_requests:
        dependency, from_version, to_version = parse_dependabot_pr_title(pr["title"])
        status_check_rollup = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]

        rollup_state = status_check_rollup["state"] if status_check_rollup else None
        if rollup_state == "SUCCESS":
            check_status = CheckStatus.SUCCESS
        elif rollup_state == "PENDING" or rollup_state == "EXPECTED":
            check_status = CheckStatus.PENDING
        elif rollup_state == "ERROR" or rollup_state == "FAILURE":
            check_status = CheckStatus.FAILED
        elif rollup_state is not None:
            # Any states we don't recognize are treated as failed
            check_status = CheckStatus.FAILED
        else:
            check_status = CheckStatus.MISSING

        updates.append(
            DependencyUpdatePR(
                id=pr["id"],
                approved=pr["reviewDecision"] == "APPROVED",
                check_status=check_status,
                dependency=dependency,
                from_version=from_version,
                merge_method=pr["repository"]["viewerDefaultMergeMethod"],
                notes=pr["bodyText"],
                to_version=to_version,
                url=pr["url"],
            )
        )

    return updates


def merge_pr(gh: GitHubClient, pr_id: str, merge_method="MERGE"):
    """
    Merge a GitHub Pull Request.

    :param merge_method: Merge strategy to use. See https://docs.github.com/en/graphql/reference/enums#pullrequestmergemethod
    """

    merge_query = """
    mutation mergePullRequest($input: MergePullRequestInput!) {
      mergePullRequest(input: $input) {
        pullRequest {
          merged
          url
        }
      }
    }
    """
    gh.query(
        merge_query, {"input": {"pullRequestId": pr_id, "mergeMethod": merge_method}}
    )


def read_action(prompt: str, default=None) -> str:
    """
    Read a command from the user.

    :param default: Default response in non-interactive environments
    """
    if not os.isatty(sys.stdout.fileno()) and default:
        return default

    action = ""
    while not action:
        action = input(f"{prompt}: ").strip()
    return action


def open_url(url: str):
    """Open a URL in the user's default browser."""
    subprocess.call(["open", url])


def main():
    parser = ArgumentParser()
    parser.add_argument(
        "organization", help="GitHub user or organization to search for Dependabot PRs"
    )
    args = parser.parse_args()

    access_token = os.environ["GITHUB_TOKEN"]
    gh_client = GitHubClient(token=access_token)

    print(f"Finding open Dependabot PRs for user or organization {args.organization}…")
    updates = fetch_dependency_prs(gh_client, organization=args.organization)

    updates_by_dependency: dict[str, list[DependencyUpdatePR]] = {}
    for update in updates:
        if update.dependency not in updates_by_dependency:
            updates_by_dependency[update.dependency] = []
        updates_by_dependency[update.dependency].append(update)

    deps = sorted(updates_by_dependency.keys())
    print(f"Found {len(updates)} PRs for {len(deps)} dependencies\n")

    to_review = len(updates)
    for dep in deps:
        updates = updates_by_dependency[dep]
        version_bumps = {(u.from_version, u.to_version) for u in updates}

        print(
            f"{to_review} updates to review. Reviewing {len(updates)} updates for {dep}:"
        )
        print("Version ranges:")
        for from_ver, to_ver in version_bumps:
            print(f"  {from_ver} -> {to_ver}")

        updates_by_status: dict[CheckStatus, list[DependencyUpdatePR]] = {}
        for update in updates:
            if update.check_status not in updates_by_status:
                updates_by_status[update.check_status] = []
            updates_by_status[update.check_status].append(update)

        check_statuses: list[str] = []
        for status, items in updates_by_status.items():
            check_statuses.append(f"{len(items)} {status.description}")
        print(f"Check status: {', '.join(check_statuses)}")

        for update in updates:
            if update.check_status == CheckStatus.SUCCESS:
                continue
            print(f"  {update.url} checks {update.check_status.description}")

        while True:
            action = read_action(
                "[m]erge all passing, [s]kip, [q]uit, [r]eview notes, [l]ist PR urls",
                default="skip",
            )
            if "quit".startswith(action):
                return
            elif "merge".startswith(action):
                for update in updates:
                    if update.check_status != CheckStatus.SUCCESS:
                        # Skip PRs with missing or failed checks
                        continue

                    print(f"Merging {update.url} …")
                    try:
                        merge_pr(
                            gh_client, pr_id=update.id, merge_method=update.merge_method
                        )
                    except Exception as e:
                        print("Merge failed: ", repr(e))
                break
            elif "skip".startswith(action):
                break
            elif "review".startswith(action):
                open_url(updates[0].url)
            elif "list".startswith(action):
                urls = sorted(u.url for u in updates)
                for url in urls:
                    print(f"  {url}")

        to_review -= len(updates)
        print("")


if __name__ == "__main__":
    main()
