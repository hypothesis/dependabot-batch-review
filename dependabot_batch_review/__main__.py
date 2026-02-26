import sys
from argparse import ArgumentParser
from blessings import Terminal  # type: ignore
from pathlib import Path # Added this import

from .github_client import GitHubClient
from .review import (
    fetch_dependency_prs,
    review_updates,
    DependencyUpdatePR,
    PromptAbortError,
    generate_xlsx_report,
)


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument(
        "organization", help="GitHub user or organization to search for Dependabot PRs"
    )
    parser.add_argument(
        "--label",
        "-l",
        default=[],
        nargs="*",
        help="Specify additional labels to filter PRs",
    )
    parser.add_argument(
        "--repo-filter", "-r", help="Filter PRs against a repository pattern"
    )
    parser.add_argument(
        "--type", "-t", help="""Specify package type (eg. "npm_and_yarn", "pip")"""
    )
    parser.add_argument(
        "--output-md",
        help="Path to a Markdown file to write the review output to.",
    )
    parser.add_argument(
        "--output-xlsx",
        help="Path to an XLSX file to write the review output to, using the template.",
    )
    args = parser.parse_args()

    gh_client = GitHubClient.init()
    t = Terminal()

    print(f"Finding Dependabot PRs in {t.bold}{args.organization}{t.normal}'s repos…")

    labels = ["dependencies"]
    for label in args.label:
        labels.append(label)

    updates = fetch_dependency_prs(
        gh_client,
        organization=args.organization,
        labels=labels,
        repo_filter=args.repo_filter,
    )

    if args.type:
        updates = [u for u in updates if u.package_type == args.type]

    updates_by_group_name: dict[str, list[DependencyUpdatePR]] = {}
    for update in updates:
        if update.group_name not in updates_by_group_name:
            updates_by_group_name[update.group_name] = []
        updates_by_group_name[update.group_name].append(update)

    groups = sorted(updates_by_group_name.keys())
    print(f"Found {len(updates)} PRs for {len(groups)} dependencies\n")

    if args.output_xlsx:
        try:
            template_path = Path("./hypothesis_dependabot_alerts_tracker_example.xlsx")
            output_path = Path(args.output_xlsx)
            generate_xlsx_report(updates, template_path, output_path)
        except Exception as e:
            print(f"Error generating XLSX report: {e}", file=sys.stderr)
            return 1
    elif args.output_md:
        try:
            review_updates(gh_client, updates, output_md_path=args.output_md)
        except PromptAbortError:
            return 0
    else:
        to_review = len(updates)
        for group in groups:
            group_updates = updates_by_group_name[group]
            group_type = "group" if group_updates[0].is_group else "dependency"

            print(f"{len(group_updates)} updates for {group_type} {t.bold}{group}{t.normal}:")

            try:
                review_updates(gh_client, group_updates)
            except PromptAbortError:
                return 0

            to_review -= len(group_updates)
            print("")

    return 0


if __name__ == "__main__":
    sys.exit(main())
