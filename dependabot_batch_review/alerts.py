from argparse import ArgumentParser
from dataclasses import dataclass
from typing import Optional
import sys

from .github_client import GitHubClient


@dataclass
class Vulnerability:
    repo: str
    """Repository where this vulnerability was reported."""

    created_at: str
    """ISO date when this alert was created."""

    package_name: str
    """Name of the vulnerable package."""

    ecosystem: str
    """Package ecosytem (eg. npm) that the package comes from."""

    severity: str
    """Vulnerability severity level."""

    version_range: str
    """Version ranges of package affected by vulnerability."""

    number: str
    """Number of this vulnerability report."""

    url: str
    """Link to the vulernability report on GitHub."""

    pr: Optional[str]
    """Link to the Dependabot update PR that resolves this vulnerability."""

    title: str
    """Summary of what the vulnerability is."""


def fetch_alerts(gh: GitHubClient, organization: str) -> list[Vulnerability]:
    """
    Fetch details of all open vulnerability alerts in `organization`.

    To reduce the volume of noise, especially for repositories which include the
    same dependency in multiple lockfiles, only one vulnerability is reported
    per package per repository.

    Vulnerabilities are not reported from archived repositories.
    """

    query = """
query($organization: String!, $cursor: String) {
  organization(login: $organization) {
    repositories(first: 100, after: $cursor) {
      pageInfo {
        endCursor
        hasNextPage
      }
      nodes {
        name
        vulnerabilityAlerts(first: 100, states:OPEN) {
          nodes {
            number
            createdAt
            dependabotUpdate {
              pullRequest {
                url
              }
            }
            securityAdvisory {
              summary
            }
            securityVulnerability {
              package {
                name
                ecosystem
              }
              severity
              vulnerableVersionRange
            }
          }
        }
      }
    }
  }
}
"""

    vulns = []
    cursor = None
    has_next_page = True

    while has_next_page:
        result = gh.query(
            query=query, variables={"organization": organization, "cursor": cursor}
        )
        page_info = result["organization"]["repositories"]["pageInfo"]
        cursor = page_info["endCursor"]
        has_next_page = page_info["hasNextPage"]

        for repo in result["organization"]["repositories"]["nodes"]:
            alerts = repo["vulnerabilityAlerts"]["nodes"]

            if alerts:
                repo_name = repo["name"]
                vulnerable_packages = set()

                for alert in alerts:
                    sa = alert["securityAdvisory"]
                    sv = alert["securityVulnerability"]
                    number = alert["number"]
                    package_name = sv["package"]["name"]

                    if package_name in vulnerable_packages:
                        continue
                    vulnerable_packages.add(package_name)

                    pr = None

                    dep_update = alert["dependabotUpdate"]
                    if dep_update and dep_update["pullRequest"]:
                        pr = dep_update["pullRequest"]["url"]

                    vuln = Vulnerability(
                        repo=repo_name,
                        created_at=alert["createdAt"],
                        ecosystem=sv["package"]["ecosystem"],
                        number=number,
                        package_name=sv["package"]["name"],
                        pr=pr,
                        severity=sv["severity"],
                        title=sa["summary"],
                        url=f"https://github.com/{organization}/{repo_name}/security/dependabot/{number}",
                        version_range=sv["vulnerableVersionRange"],
                    )
                    vulns.append(vuln)

    return vulns


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument(
        "organization", help="GitHub user or organization to search for Dependabot PRs"
    )
    args = parser.parse_args()

    gh_client = GitHubClient.init()
    vulns = fetch_alerts(gh_client, args.organization)

    print(f"Found {len(vulns)} vulnerabilities.")
    for vuln in vulns:
        print(f"{args.organization}/{vuln.repo}: {vuln.package_name} {vuln.severity}")
        print(f"  {vuln.title}")
        print(f"  {vuln.url}")

        if vuln.pr:
            print(f"  Resolved by: {vuln.pr}")

        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
