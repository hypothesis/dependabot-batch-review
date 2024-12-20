from dataclasses import dataclass
from enum import Enum
import re
import os
from typing import Optional
import subprocess
import sys

from bs4 import BeautifulSoup, PageElement

from .github_client import GitHubClient


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
class DependencyUpdate:
    name: str
    """Name of the dependency being updated."""

    from_version: Optional[str]
    """
    The version of the dependency before the update.

    May be `None` if the version could not be found in the PR details.
    """

    to_version: Optional[str]
    """
    The version of the dependency after the update.

    May be `None` if the version could not be found in the PR details.
    """

    notes: str
    """Release notes for this update."""


@dataclass
class DependencyUpdatePR:
    id: str
    """ID of the pull request"""

    package_type: str
    """Type of package (pip, npm etc.)"""

    is_group: bool
    """True if this an update of a group of dependencies."""

    group_name: str
    """Name of the dependency or group of dependencies updated in this PR."""

    updates: list[DependencyUpdate]
    """The updates included in this PR."""

    url: str
    """URL of the pull request on GitHub"""

    approved: bool
    """Whether this PR has been given an approving review"""

    check_status: CheckStatus
    """The status of automated checks for this commit (eg. CI)"""

    merge_method: str
    """The preferred merge method for this PR"""


@dataclass
class DependencyUpdateDetails:
    """
    Details about contents of PR extracted from title and body.

    This is a subset of `DependencyUpdatePR`.
    """

    group_name: str
    is_group: bool
    updates: list[DependencyUpdate]


def parse_dependabot_pr(title: str, body: str) -> DependencyUpdateDetails:
    """
    Extract information about updates in a Dependabot PR.

    :param title: PR title
    :param body: HTML body of PR
    """
    soup = BeautifulSoup(body, "html.parser")

    # PRs that update a single dependency have a title such as "Bump foo from
    # 1.0.0 to 2.0.0" at the top.
    title_re = r"Bump (\S+) from (\S+) to (\S+)"
    fields_match = re.search(title_re, title, re.IGNORECASE)
    if fields_match:
        dependency, from_version, to_version = fields_match.groups()

        # The body of a Dependabot PR is a series of sections, each of which is
        # wrapped in a `<details>` container. The final `<details>` container lists
        # the standard commands which can be issued to the bot via comments on the PR.
        details = [
            d.get_text()
            for d in soup.find_all("details")
            if not d.get_text().strip().startswith("Dependabot commands and options")
        ]

        return DependencyUpdateDetails(
            group_name=dependency,
            is_group=False,
            updates=[
                DependencyUpdate(
                    name=dependency,
                    from_version=from_version,
                    to_version=to_version,
                    notes="\n\n".join(details),
                )
            ],
        )

    # PRs that update a named dependency group have a title of the form "Bump
    # the foo group with 2 updates". Dependabot may also update multiple
    # dependencies in a PR which are not part of a named group, if those
    # dependencies need to be updated together. Those PRs have titles like "Bump
    # foo and bar".
    #
    # For each update there is a paragraph in the body containing the text
    # "Updates bar from 1.0.0 to 2.0.0" followed by `<details>` sections for
    # release notes, changelog and commits.
    #
    # As an exception, if there is only one update, the "Updates bar ..."
    # paragraph is omitted and instead there is a paragraph with the text
    # "Bumps the foo group with 1 update: bar".
    group_title_re = r"Bump the (\S+) group"
    group_title_match = re.search(group_title_re, title, re.IGNORECASE)
    if not group_title_match:
        # Fallback for titles like "Bump foo and bar".
        group_title_match = re.search(r"Bump (.*)", title, re.IGNORECASE)

    if not group_title_match:
        raise ValueError("PR title does not match known patterns")
    (group_title,) = group_title_match.groups()

    update_heading_pat = r"Updates (\S+) from (\S+) to (\S+)"

    def contains_update_heading(el: PageElement) -> bool:
        return re.search(update_heading_pat, el.get_text()) is not None

    headings = [p for p in soup.find_all("p") if contains_update_heading(p)]

    # Handle case of a single update where the "Updates ..." headings are
    # missing.
    single_update_pat = r"Bumps the \S+ group with 1 update: (\S+)"
    if not headings:
        headings = [
            p for p in soup.find_all("p") if re.match(single_update_pat, p.get_text())
        ]
        if not headings:
            raise ValueError("Package names not found in PR body")

    updates = []
    for heading in headings:
        fields_match = re.search(update_heading_pat, heading.get_text(), re.IGNORECASE)
        if fields_match:
            dependency, from_version, to_version = fields_match.groups()
        else:
            fields_match = re.search(
                single_update_pat, heading.get_text(), re.IGNORECASE
            )
            assert fields_match
            (dependency,) = fields_match.groups()
            from_version = None
            to_version = None

        notes: list[str] = []

        # Gather notes from `<details>` elements following the heading, until
        # we come to the next heading or the `<hr>` that separates the
        # update-specific notes from the general Dependabot commands and
        # options.
        curr = heading.next_sibling
        while curr and not contains_update_heading(curr) and curr.name != "hr":
            if curr.name == "details":
                notes.append(curr.get_text())
            curr = curr.next_sibling

        updates.append(
            DependencyUpdate(
                name=dependency,
                from_version=from_version,
                to_version=to_version,
                notes="\n\n".join(notes),
            )
        )

    return DependencyUpdateDetails(
        group_name=group_title, is_group=True, updates=updates
    )


def parse_package_type_from_branch_name(branch: str) -> str:
    """
    Extract package type information from Dependabot PR.

    This relies on Dependabot PRs using branch names of the form `dependabot/{package_type}/{package_name}-{version}`
    """
    branch_name_re = "^dependabot/([^/]+)/.*"
    branch_name_match = re.search(branch_name_re, branch)
    if not branch_name_match:
        raise ValueError(f"Failed to parse branch name '{branch}'")
    package_type = branch_name_match.groups()[0]
    return package_type


def fetch_dependency_prs(
    gh: GitHubClient,
    organization: str,
    repo_filter: Optional[str] = None,
    labels: list[str] = ["dependencies"],
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
            headRefName
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

    label_terms = " ".join(f"label:{label}" for label in labels)
    query = f"org:{organization} {label_terms} is:pr is:open author:app/dependabot"
    result = gh.query(query=dependencies_query, variables={"query": query})
    pull_requests = result["search"]["nodes"]

    updates: list[DependencyUpdatePR] = []
    for pr in pull_requests:
        repo = pr["repository"]["name"]

        if repo_filter is not None:
            if repo_filter not in repo:
                continue

        try:
            update_details = parse_dependabot_pr(pr["title"], pr["bodyHTML"])
            status_check_rollup = pr["commits"]["nodes"][0]["commit"][
                "statusCheckRollup"
            ]
            package_type = parse_package_type_from_branch_name(pr["headRefName"])
        except ValueError as exc:
            print(f"Failed to parse details from {pr['url']}: {exc}", file=sys.stderr)
            continue

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
                is_group=update_details.is_group,
                group_name=update_details.group_name,
                approved=pr["reviewDecision"] == "APPROVED",
                check_status=check_status,
                updates=update_details.updates,
                merge_method=pr["repository"]["viewerDefaultMergeMethod"],
                package_type=package_type,
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


def get_package_diff_url(package_type: str, update: DependencyUpdate) -> str | None:
    """
    Get the URL of a web page showing the changes in the contents of a package.
    """
    if not update.from_version or not update.to_version:
        return None

    match package_type:
        case "npm_and_yarn":
            return f"https://diff.intrinsic.com/{update.name}/{update.from_version}/{update.to_version}"
        case _:
            # TODO - Find the best available equivalents for PyPI etc.
            return None


def review_updates(gh_client: GitHubClient, prs: list[DependencyUpdatePR]) -> None:
    """
    Perform an interactive review/merge of a batch of updates for a dependency.
    """

    version_bumps = set()
    for pr in prs:
        for u in pr.updates:
            version_bumps.add((u.name, u.from_version, u.to_version))

    print("Versions:")
    for name, from_ver, to_ver in version_bumps:
        from_ver = from_ver or "(unknown)"
        to_ver = to_ver or "(unknown)"
        print(f"  {name} {from_ver} -> {to_ver}")

    updates_by_status: dict[CheckStatus, list[DependencyUpdatePR]] = {}
    for update in prs:
        if update.check_status not in updates_by_status:
            updates_by_status[update.check_status] = []
        updates_by_status[update.check_status].append(update)

    check_statuses: list[str] = []
    for status, items in updates_by_status.items():
        check_statuses.append(f"{len(items)} {status.description}")
    print(f"Checks: {', '.join(check_statuses)}")

    for update in prs:
        if update.check_status == CheckStatus.SUCCESS:
            continue
        print(f"  {update.url} checks {update.check_status.description}")

    while True:
        action = read_action(
            prompt="[m]erge passing, [s]kip, [q]uit, [r]eview changes, package [d]iff, [v]iew in browser, [l]ist URLs",
            actions=["diff", "merge", "skip", "quit", "review", "list", "view"],
            default="skip",
        )
        if action == "quit":
            return
        elif action == "merge":
            for update in prs:
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
            notes = []
            for u in prs[0].updates:
                notes += u.notes.splitlines()
            max_lines = 35
            if len(notes) > max_lines:
                notes = notes[0:max_lines]
                notes.append('... (Enter "view" to see full notes in browser)')
            for line in notes:
                print(f"  {line}")
        elif action == "view":
            open_url(prs[0].url)
        elif action == "list":
            urls = sorted(u.url for u in prs)
            for url in urls:
                print(f"  {url}")
        elif action == "diff":
            diff_urls = set()
            for pr in prs:
                for pr_update in pr.updates:
                    if diff_url := get_package_diff_url(pr.package_type, pr_update):
                        diff_urls.add(diff_url)

            match len(diff_urls):
                case 0:
                    print(
                        """Package diffs are not available for these packages.

Package diffs are currently only available for npm packages."""
                    )
                case 1:
                    # There is one (package, from_version, to_version) diff.
                    # Open it directly
                    open_url(next(iter(diff_urls)))
                case _:
                    # There is more than one (package, from_version, to_version)
                    # combination, so list all the URLs.
                    for url in diff_urls:
                        print(url)
