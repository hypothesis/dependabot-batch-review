from getpass import getpass
import json
from typing import Any
import os

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
    def init(cls):
        """
        Initialize a GitHubClient.
        """
        access_token = os.environ.get("GITHUB_TOKEN")
        if not access_token:
            access_token = getpass("GitHub API token: ")
        return GitHubClient(access_token)