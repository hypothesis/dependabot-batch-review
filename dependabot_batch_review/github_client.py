from getpass import getpass
import json
import os
import time
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

    def query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        data = {"query": query, "variables": variables or {}}
        headers = {"Authorization": f"Bearer {self.token}"}
        if extra_headers:
            headers.update(extra_headers)

        # GitHub's GraphQL endpoint intermittently returns transient 5xx / HTML
        # (e.g. a 502 when a heavy `bodyHTML` search times out). Retry those with
        # backoff so the daily automation doesn't fall over on a blip. A request
        # timeout ensures a hung connection can't stall a scheduled run forever.
        last_error = "unknown error"
        for attempt in range(4):
            result = requests.post(
                url=self.endpoint,
                headers=headers,
                data=json.dumps(data),
                timeout=30,
            )
            if result.status_code >= 500:
                last_error = f"HTTP {result.status_code}"
                time.sleep(2**attempt)
                continue
            try:
                body = result.json()
            except ValueError:
                last_error = "non-JSON response from GitHub"
                time.sleep(2**attempt)
                continue
            result.raise_for_status()
            if "errors" in body:
                raise Exception(f"Query failed: {json.dumps(body['errors'])}")
            return body["data"]

        raise Exception(f"GitHub GraphQL request failed after retries: {last_error}")

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
