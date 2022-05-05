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
        return body["data"]


@dataclass
class DependencyUpdatePR:
    dependency: str
    from_version: str
    to_version: str
    notes: str
    url: str


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
            }}
            title
            bodyText
            url
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
        updates.append(
            DependencyUpdatePR(
                dependency=dependency,
                from_version=from_version,
                to_version=to_version,
                notes=pr["bodyText"],
                url=pr["url"],
            )
        )

    return updates


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
    args = parser.parse_args()

    access_token = os.environ["GITHUB_TOKEN"]
    gh_client = GitHubClient(token=access_token)

    org = "hypothesis"

    print(f"Finding Dependabot PRs for organization {org}…")
    updates = fetch_dependency_prs(gh_client, organization="hypothesis")

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
        print(f"Example URL: {updates[0].url}")

        while True:
            action = read_action(
                "[m]erge all, [s]kip, [q]uit, [r]eview notes, [l]ist urls"
            )
            if "quit".startswith(action):
                return
            elif "merge".startswith(action):
                for update in updates:
                    print(f"Merging {update.url}…")
                    time.sleep(2)
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
