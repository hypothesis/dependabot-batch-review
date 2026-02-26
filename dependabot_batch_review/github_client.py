from getpass import getpass
import json
import os
from subprocess import CalledProcessError, run
from typing import Any, Self
import sys  # Added import

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

    @classmethod
    def init(cls) -> Self:
        """
        Initialize an authenticated GitHubClient.

        This will read from the `GITHUB_TOKEN` env var if set, query the `gh`
        command if installed, or prompt otherwise.
        """
        access_token = os.environ.get("GITHUB_TOKEN")

        if not access_token:
            try:
                access_token = run(
                    ["gh", "auth", "token"], check=True, capture_output=True, text=True
                ).stdout.strip()
            except (CalledProcessError, FileNotFoundError):
                pass

        if not access_token:
            if not os.isatty(sys.stdin.fileno()):
                raise Exception(
                    "No GitHub token found and not running in an interactive terminal. Please set GITHUB_TOKEN or run `gh auth login`."
                )
            access_token = getpass("GitHub API token: ")

        return cls(access_token)
