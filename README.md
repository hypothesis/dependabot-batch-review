# depandabot-batch-review

CLI tool for batch review of
[Dependabot](https://docs.github.com/en/code-security/dependabot) PRs across all
of a user or organization's repositories.

## Introduction

This tool enables efficient review and merging of Dependabot PRs across the
repositories in an organization (or user account).

It is built on the GitHub [GraphQL API](https://docs.github.com/en/graphql).

## Installation

1. Install [Pipenv](https://pipenv.pypa.io/en/latest/)

2. Clone this repository and install Python dependencies with:

   ```
   pipenv install --dev
   ```

## Usage

First generate a [GitHub access
token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/creating-a-personal-access-token)
with permission to read and merge PRs in the organization you want to query.
Then in this repository run:

```sh
export GITHUB_TOKEN=$TOKEN
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

11 updates to review. Reviewing 1 updates for @babel/core:
Version ranges:
  7.17.9 -> 7.17.10
Check status: 1 passed, 0 failed
[m]erge all passing, [s]kip, [q]uit, [r]eview notes, [l]ist PR urls:
```

## Limitations

This tool currently only fetches up to 100 PRs per run. To continue reviewing
after processing these, simply run the tool again.
