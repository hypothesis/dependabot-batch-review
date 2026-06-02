"""
Post-merge orchestration for live Tier-1 merges.

For each production-deploying merge, verify health and — if degraded — triage and
roll back, posting the verdict to Slack. Kept separate from ``automerge`` so the
engine stays unit-testable without the health/rollback dependencies.
"""

from __future__ import annotations

import os

from .automation_types import MergeOutcome
from .config import Config
from .github_client import GitHubClient
from .health import check_health
from .rollback import revert_merge
from .slack_messages import format_rollback, format_tier1_health
from .slack import SlackClient
from .triage import triage_pr


def health_gate_outcomes(
    gh: GitHubClient, outcomes: list[MergeOutcome], cfg: Config
) -> None:
    """Health-check each Tier-1 merge; roll back and alert on failure."""
    token = os.environ.get("SLACK_TOKEN")
    channel = cfg.slack_channel
    slack = SlackClient(token) if (token and channel) else None

    for outcome in outcomes:
        verdict = check_health(gh, outcome, cfg.health)

        if verdict.healthy:
            if slack and channel:
                slack.post_message(channel, format_tier1_health(outcome, verdict))
            continue

        triage = triage_pr(outcome.pr)
        if outcome.merge_commit_sha:
            rollback = revert_merge(
                gh,
                outcome.owner,
                outcome.repo,
                outcome.merge_commit_sha,
                original_title=outcome.pr.group_name,
                original_pr_url=outcome.pr.url,
                dry_run=cfg.dry_run,
                merge_method=outcome.pr.merge_method,
            )
        else:
            from .rollback import RollbackResult

            rollback = RollbackResult(
                performed=False,
                revert_pr_url=None,
                revert_commit_sha=None,
                reason="no merge commit SHA captured; manual rollback required",
                dry_run=cfg.dry_run,
            )

        if slack and channel:
            slack.post_message(
                channel, format_rollback(outcome, verdict, rollback, triage)
            )
