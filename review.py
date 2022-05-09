from argparse import ArgumentParser
from dataclasses import dataclass
from enum import Enum
import json
import re
import os
from typing import Any, Optional
import subprocess
import sys

from blessings import Terminal  # type: ignore
from bs4 import BeautifulSoup
import requests


class GitHubClient:
    """
    Client for GitHub's GraphQL API.

    See https://docs.github.com/en/graphql.
    """

    def __init__(self, token: str) -> None:
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
    def description(self) -> str:
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


def parse_dependabot_pr_body(html: str) -> str:
    """
    Extract release notes from the body of a Dependabot PR.
    """
    soup = BeautifulSoup(html, "html.parser")

    # The body of a Dependabot PR is a series of sections, each of which is
    # wrapped in a `<details>` container. The final `<details>` container lists
    # the standard commands which can be issued to the bot via comments on the PR.
    details = [
        d.get_text()
        for d in soup.find_all("details")
        if not d.get_text().strip().startswith("Dependabot commands and options")
    ]
    return "\n\n".join(details)


def fetch_dependency_prs(
    gh: GitHubClient, organization: str, label: str = "dependencies"
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
            bodyHTML
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
        notes = parse_dependabot_pr_body(pr["bodyHTML"])
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
                notes=notes,
                to_version=to_version,
                url=pr["url"],
            )
        )

    return updates


def merge_pr(gh: GitHubClient, pr_id: str, merge_method: str = "MERGE") -> None:
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


class PromptAbortError(Exception):
    """
    Exception raised if the user attempts to exit an interactive prompt.
    """

    pass


def read_action(prompt: str, actions: list[str], default: Optional[str] = None) -> str:
    """
    Read a command from the user.

    The user can enter any action from `actions` or a prefix of one. Matching
    is case-insensitive.

    :param prompt: Prompt telling the user what commands are available
    :param actions: List of actions the user can perform. These should all be lower-case.
    :param default: Default response in non-interactive environments
    :return: Action from the `actions` list
    """
    if not os.isatty(sys.stdout.fileno()) and default:
        return default

    while True:
        try:
            user_input = input(f"{prompt} > ").strip().lower()
        except EOFError as e:  # Ctrl+D
            raise PromptAbortError() from e
        except KeyboardInterrupt as e:  # Ctrl+C
            raise PromptAbortError() from e

        # Look for an exact match
        for action in actions:
            if action == user_input:
                return action

        # If no exact match found, look for a prefix match
        for action in actions:
            if action.startswith(user_input):
                return action


def open_url(url: str) -> None:
    """Open a URL in the user's default browser."""
    subprocess.call(["open", url])


def review_updates(gh_client: GitHubClient, updates: list[DependencyUpdatePR]) -> None:
    """
    Perform an interactive review/merge of a batch of updates for a dependency.
    """

    version_bumps = {(u.from_version, u.to_version) for u in updates}
    print("Versions:")
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
    print(f"Checks: {', '.join(check_statuses)}")

    for update in updates:
        if update.check_status == CheckStatus.SUCCESS:
            continue
        print(f"  {update.url} checks {update.check_status.description}")

    while True:
        action = read_action(
            prompt="[m]erge all passing, [s]kip, [q]uit, [r]eview changes, [v]iew in browser, [l]ist URLs",
            actions=["merge", "skip", "quit", "review", "list", "view"],
            default="skip",
        )
        if action == "quit":
            return
        elif action == "merge":
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
        elif action == "skip":
            break
        elif action == "review":
            notes = updates[0].notes
            for line in notes.splitlines():
                print(f"  {line}")
        elif action == "view":
            open_url(updates[0].url)
        elif action == "list":
            urls = sorted(u.url for u in updates)
            for url in urls:
                print(f"  {url}")


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument(
        "organization", help="GitHub user or organization to search for Dependabot PRs"
    )
    args = parser.parse_args()

    access_token = os.environ["GITHUB_TOKEN"]
    gh_client = GitHubClient(token=access_token)
    t = Terminal()

    print(f"Finding Dependabot PRs in {t.bold}{args.organization}{t.normal}'s repos…")
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

        print(f"{len(updates)} updates for {t.bold}{dep}{t.normal}:")

        try:
            review_updates(gh_client, updates)
        except PromptAbortError:
            return 0

        to_review -= len(updates)
        print("")

    return 0


if __name__ == "__main__":
    sys.exit(main())
