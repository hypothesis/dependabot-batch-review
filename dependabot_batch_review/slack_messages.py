"""
Slack message formatters (mrkdwn) for the automation layer.

Pure functions returning Slack ``mrkdwn`` strings; posting is done by the existing
``slack.SlackClient.post_message``. Mirrors ``alerts.format_slack_message``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .automation_types import HealthVerdict, MergeOutcome

if TYPE_CHECKING:
    from .automerge import Decision, RunResult
    from .config import Config
    from .rollback import RollbackResult
    from .triage import TriageResult


def _pr_link(url: str, number: int | None) -> str:
    label = f"#{number}" if number is not None else "PR"
    return f"<{url}|{label}>"


def _group_escalations(decisions: list[Decision]) -> dict[str, list[Decision]]:
    grouped: dict[str, list[Decision]] = {}
    for decision in decisions:
        grouped.setdefault(decision.pr.group_name, []).append(decision)
    return grouped


def format_run_digest(result: RunResult, cfg: Config) -> str:
    """The daily digest posted after an auto-merge run."""
    mode = "DRY-RUN" if result.dry_run else "LIVE"
    verb = "Would merge" if result.dry_run else "Merged"
    parts: list[str] = [f"*Dependabot auto-merge — {cfg.organization}* [{mode}]"]

    merged = result.merged
    merged_lines = [f"*✅ {verb} {len(merged)} PR(s)*"]
    for d in sorted(merged, key=lambda x: (x.pr.repo or "", x.pr.group_name)):
        tag = "T1/health" if d.action == "merge+health" else "T0"
        merged_lines.append(
            f"• [{tag}] `{d.pr.repo}`: {d.pr.group_name} {_pr_link(d.pr.url, d.pr.number)}"
        )
    parts.append("\n".join(merged_lines))

    escalated = result.escalated
    esc_lines = [f"*🔍 {len(escalated)} need human review*"]
    for group, items in sorted(_group_escalations(escalated).items()):
        repos = ", ".join(
            f"`{d.pr.repo}` {_pr_link(d.pr.url, d.pr.number)}"
            for d in sorted(items, key=lambda x: x.pr.repo or "")
        )
        reason = "; ".join(items[0].classification.reasons) or "review"
        esc_lines.append(f"• *{group}* ({len(items)}): {repos} — _{reason}_")
    parts.append("\n".join(esc_lines))

    parts.append(f"_⏳ {len(result.skipped)} skipped (not yet eligible)_")
    return "\n\n".join(parts)


def format_tier0_batch(owner: str, merged: list[MergeOutcome]) -> str:
    lines = [f"*✅ Auto-merged {len(merged)} Tier-0 PR(s) in `{owner}` (no deploy)*"]
    for outcome in merged:
        lines.append(
            f"• `{outcome.repo}`: {outcome.pr.group_name} "
            f"{_pr_link(outcome.pr.url, outcome.pr.number)}"
        )
    lines.append("_No production deploy triggered (dev / lockfile / tooling only)._")
    return "\n".join(lines)


def _signal_lines(verdict: HealthVerdict) -> list[str]:
    lines: list[str] = []
    for signal in verdict.signals.values():
        icon = "✅" if signal.healthy else "❌"
        lines.append(f"  • {signal.source}: {signal.detail} {icon}")
    return lines


def format_tier1_health(outcome: MergeOutcome, verdict: HealthVerdict) -> str:
    status = "*HEALTHY ✅*" if verdict.healthy else "*UNHEALTHY ❌*"
    sha = (outcome.merge_commit_sha or "")[:7]
    lines = [
        f"*🚀 Tier-1 merged & deployed: `{outcome.owner}/{outcome.repo}`*",
        f"{_pr_link(outcome.pr.url, outcome.pr.number)} {outcome.pr.group_name} · merge `{sha}`",
        f"Health gate: {status}",
    ]
    lines.extend(_signal_lines(verdict))
    return "\n".join(lines)


def format_rollback(
    outcome: MergeOutcome,
    verdict: HealthVerdict,
    rollback: RollbackResult,
    triage: TriageResult | None,
) -> str:
    sha = (outcome.merge_commit_sha or "")[:7]
    lines = [
        f"*⛔ AUTO-ROLLBACK: `{outcome.owner}/{outcome.repo}`*",
        f"{_pr_link(outcome.pr.url, outcome.pr.number)} {outcome.pr.group_name} "
        f"deployed and degraded production.",
        "Health verdict: *UNHEALTHY*",
    ]
    lines.extend(_signal_lines(verdict))
    if rollback.dry_run or not rollback.performed:
        lines.append(f"Action: *not performed* — {rollback.reason}")
    elif rollback.revert_pr_url:
        lines.append(
            f"Action: reverted merge `{sha}` → <{rollback.revert_pr_url}|revert PR> ✅"
        )
    else:
        lines.append(f"Action: {rollback.reason}")
    if triage is not None:
        lines.append(
            f"*Claude triage:* {triage.summary} "
            f"(recommend: {triage.recommendation}, confidence: {triage.confidence})"
        )
    return "\n".join(lines)


def format_tier2_digest(
    owner: str, items: list[tuple[MergeOutcome, TriageResult | None]]
) -> str:
    lines = [f"*🔍 {len(items)} Dependabot PR(s) need human review in `{owner}`*", ""]
    for outcome, triage in items:
        lines.append(
            f"*{outcome.pr.group_name}* — `{outcome.repo}` "
            f"{_pr_link(outcome.pr.url, outcome.pr.number)}"
        )
        if triage is not None:
            lines.append(f"  _Claude:_ {triage.summary} ({triage.confidence})")
    return "\n".join(lines)
