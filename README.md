# depandabot-batch-review

CLI tool for batch review of
[Dependabot](https://docs.github.com/en/code-security/dependabot) PRs and
security alerts across all of a user or organization's repositories.

Note: The alert-reviewing functionality is being migrated to a separate [hypothesis/dependabot-alerts](https://github.com/hypothesis/dependabot-alerts) tool.

## Introduction

This tool enables efficient review of Dependabot activity across an organization
or user account. This includes:

- Reviewing and merging PRs to update dependencies
- Reviewing security alerts

It is built on the GitHub [GraphQL API](https://docs.github.com/en/graphql).

## Installation

1. Install [Pipenv](https://pipenv.pypa.io/en/latest/)

2. Clone this repository and install Python dependencies with:

   ```
   pipenv install --dev
   ```

## Authorization

In order of precedence, this tool will obtain a GitHub API token from:

 - The `GITHUB_TOKEN` environment variable
 - The [GitHub CLI](https://cli.github.com) if installed and logged in
 - Prompting for a token when the tool is run

For reviewing security alerts, the token must have permission to query alerts
in the target organization/user account. For reviewing updates, this token must
have permission to read and merge PRs in the target organization/user account.

## Reviewing security alerts

To review open security alerts for a user or organization, run:

```sh
pipenv run alerts [organization]
```

This will search for all open Dependabot alerts against repositories in the
organization and list their details.

If the same alert is reported multiple times against a single repository,
only one instance will be shown.

### Resolving alerts

To resolve an alert, you can:

1. Merge a Dependabot PR that has been created. This can be done either through
   the GitHub UI or using this tool.
2. Manually create a PR to update affected dependencies.
3. Dismiss an alert if the risk to a project is low or the alert is not
   relevant.
4. Archive the GitHub project if it is no longer being maintained. This will
   disable all vulnerability reports.
5. Set up custom Dependabot alert rules on a per repository basis. This can
   be used to fine tune which alerts are reported based on the lockfile path
   and other criteria.

   See https://docs.github.com/en/code-security/dependabot/dependabot-alert-rules/about-dependabot-alert-rules

### Slack notifications

If run with the `--slack` flag, the tool will send a report of vulnerabilities
found to the Slack channel specified using the `SLACK_CHANNEL` environment
variable. An authentication token for a Slack app must be provided via the
`SLACK_TOKEN` environment variable. You can create a Slack app at
https://api.slack.com/apps.

## Reviewing updates

To review Dependabot dependency update PRs for a user or organization, run:

```sh
pipenv run review [organization]
```

This will query for open PRs from Dependabot in the organization `organization`,
which can also be a GitHub username. It will group the updates by package name,
then go through each package in alphabetical order, show a summary of the PRs
updating that package and prompt for an action.

For each package it will show the name, version ranges of updates and status of
continuous integration checks. At this point you can then choose to review
release notes for the update, merge all PRs in the group that have passed CI
checks, or see individual PRs in the group.

```shellsession
$ pipenv run review hypothesis
Finding open Dependabot PRs for user or organization hypothesis…
Found 11 PRs for 7 dependencies

1 updates for dependency @babel/core:
Versions:
  @babel/core 7.17.9 -> 7.17.10
Check status: 1 passed, 0 failed
[m]erge all passing, [s]kip, [q]uit, [r]eview notes, [l]ist PR urls:
```

### Grouped updates

When using Dependabot's [grouped
updates](https://github.blog/changelog/2023-06-30-grouped-version-updates-for-dependabot-public-beta/)
feature, this tool will treat the group name of a PR like a package name for the
purposes of grouping PRs across repositories.

If for example you had configured a group called "babel" in multiple
repositories which matched all npm dependencies whose name matches the pattern
"@babel/", then this tool would group together all the PRs that updated the
"babel" group across different repositories.

```shellsession
1 updates for group babel:
Versions:
  @babel/preset-typescript 7.22.15 -> 7.23.0
  @babel/core 7.22.17 -> 7.23.0
Checks: 1 passed
```

In this example, there is one PR updating a group called "babel", which updates
two different packages.

### Filtering updates

There are several options to filter PRs:

- `--label <label>` finds PRs with a specific label. By default Dependabot adds
  a label for the language (eg. "javascript").
- `--repo-filter <pattern>` finds PRs only in repositories that match a given pattern
- `--type <type>` finds PRs that update a specific type of package. Type values
  come from the branch names of Dependabot PRs, which have the form
  `dependabot/{package_type}/{package_name}-{version}`. For example "pip" or
  "npm_and_yarn".

### Limitations

This tool currently only fetches up to 100 PRs per run. To continue reviewing
after processing these, simply run the tool again.
