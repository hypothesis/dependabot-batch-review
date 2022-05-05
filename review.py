from argparse import ArgumentParser
from dataclasses import dataclass
import json
import re
import os
import time
from typing import Any
import subprocess

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


@dataclass
class DependencyUpdatePR:
    id: str
    dependency: str
    from_version: str
    to_version: str
    notes: str
    url: str
    approved: bool
    checks_passed: bool
    merge_method: str


def parse_dependabot_pr_title(title: str) -> tuple[str, str, str]:
    """Extract package and version info from a Dependabot PR."""

    title_re = "Bump (\S+) from (\S+) to (\S+)"
    fields_match = re.match(title_re, title)
    if not fields_match:
        raise ValueError(f"Failed to parse tile '{title}'")
    dependency, from_version, to_version = fields_match.groups()
    return (dependency, from_version, to_version)


def fetch_dependency_prs(
    gh: GitHubClient, organization: str, label="dependencies"
) -> list[DependencyUpdatePR]:
    dependencies_query = f"""
    query($query: String!) {{ 
      search(type:ISSUE, query: $query, first:100) {{
        issueCount
        nodes {{
          ... on PullRequest {{
            repository {{
              name
              viewerDefaultMergeMethod
            }}

            id
            title
            bodyText
            reviewDecision
            url

            commits (last:1) {{
              nodes {{
                commit {{
                  statusCheckRollup {{
                    state
                  }}
                }}
              }}
            }}
          }}
        }}
      }}
    }}
    """
    query = f"org:{organization} label:{label} is:pr is:open"
    result = gh.query(query=dependencies_query, variables={"query": query})
    pull_requests = result["search"]["nodes"]

    updates: list[DependencyUpdatePR] = []
    for pr in pull_requests:
        dependency, from_version, to_version = parse_dependabot_pr_title(pr["title"])
        checks_passed = (
            pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["state"]
            == "SUCCESS"
        )
        updates.append(
            DependencyUpdatePR(
                id=pr["id"],
                approved=pr["reviewDecision"] == "APPROVED",
                checks_passed=checks_passed,
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


def read_action(prompt: str):
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

    updates_by_dependency = {}
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

        checks_passed = [u for u in updates if u.checks_passed]
        checks_failed = [u for u in updates if not u.checks_passed]
        print(f"Check status: {len(checks_passed)} passed, {len(checks_failed)} failed")
        for failed in checks_failed:
            print(f"  {failed.url} failed")

        while True:
            action = read_action(
                "[m]erge all passing, [s]kip, [q]uit, [r]eview notes, [l]ist PR urls"
            )
            if "quit".startswith(action):
                return
            elif "merge".startswith(action):
                for update in checks_passed:
                    print(f"Merging {update.url}…")
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
