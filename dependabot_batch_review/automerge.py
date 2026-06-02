"""
The autonomous auto-merge engine.

Fetches open Dependabot PRs, classifies each into a tier (``risk.classify``),
decides an action under the configured policy, and — unless ``dry_run`` — merges
the eligible ones. Tier-1 (production-deploying) merges are returned as
``health_watch`` outcomes for the post-deploy health gate to verify.

Run as the daily GitHub Action entry point:
``python -m dependabot_batch_review.automerge hypothesis``.
"""

from __future__ import annotations

import os
import sys
from argparse import ArgumentParser
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .automation_types import MergeOutcome, Tier
from .config import Config, load_config
from .deploy_model import DeployModelCache
from .github_client import GitHubClient
from .review import CheckStatus, DependencyUpdatePR, fetch_dependency_prs, merge_pr
from .risk import Classification, classify

# Merge states that mean "not mergeable right now" (conflict, branch protection,
# behind base, or draft). CLEAN / UNSTABLE / UNKNOWN / null pass (CI is gated
# separately via check_status).
_BLOCKED_MERGE_STATES = frozenset({"DIRTY", "BLOCKED", "BEHIND", "DRAFT"})

_MERGE_INFO_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on PullRequest { merged mergedAt mergeCommit { oid } }
  }
}
"""

Action = str  # "merge" | "merge+health" | "escalate" | "skip"


@dataclass
class Decision:
    pr: DependencyUpdatePR
    classification: Classification
    action: Action
    eligible: bool
    skip_reason: str | None = None
    outcome: MergeOutcome | None = None


@dataclass
class RunResult:
    decisions: list[Decision] = field(default_factory=list)
    dry_run: bool = True

    @property
    def merged(self) -> list[Decision]:
        return [d for d in self.decisions if d.action in ("merge", "merge+health")]

    @property
    def health_watch(self) -> list[MergeOutcome]:
        return [
            d.outcome
            for d in self.decisions
            if d.action == "merge+health" and d.outcome is not None
        ]

    @property
    def escalated(self) -> list[Decision]:
        return [d for d in self.decisions if d.action == "escalate"]

    @property
    def skipped(self) -> list[Decision]:
        return [d for d in self.decisions if d.action == "skip"]


def age_days(pr: DependencyUpdatePR, now: datetime) -> float | None:
    """Age of the PR in days, or ``None`` if the creation time is unknown."""
    if not pr.created_at:
        return None
    try:
        created = datetime.fromisoformat(pr.created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - created).total_seconds() / 86400.0


def _age_or(pr: DependencyUpdatePR, now: datetime, default: float) -> float:
    age = age_days(pr, now)
    return default if age is None else age


def is_eligible(
    pr: DependencyUpdatePR, cls: Classification, cfg: Config, now: datetime
) -> tuple[bool, str | None]:
    """
    Decide whether a PR may be auto-merged under the configured policy.

    The tool enforces the CI-passing gate itself (branch protection is largely
    absent across the fleet), plus an age floor, a clean merge state, and the
    tier/repo allow-lists.
    """
    if pr.check_status != CheckStatus.SUCCESS:
        return False, f"CI {pr.check_status.description}"
    if not cfg.tier_enabled(int(cls.tier)):
        return False, f"tier {int(cls.tier)} not enabled"
    if not cfg.repo_permitted(pr.repo or ""):
        return False, "repo not permitted by allow/deny list"
    age = age_days(pr, now)
    if age is None:
        return False, "unknown PR age"
    if age < cfg.min_age_days:
        return False, f"too new ({age:.1f}d < {cfg.min_age_days}d)"
    if pr.merge_state_status in _BLOCKED_MERGE_STATES:
        return False, f"merge state {pr.merge_state_status}"
    if pr.mergeable == "CONFLICTING":
        return False, "merge conflict"
    return True, None


def decide(
    pr: DependencyUpdatePR, cache: DeployModelCache, cfg: Config, now: datetime
) -> Decision:
    """Classify a PR and choose an action."""
    model = cache.get(pr.repo or "")
    cls = classify(pr, model)
    eligible, reason = is_eligible(pr, cls, cfg, now)

    if cls.tier == Tier.TIER_2:
        return Decision(pr, cls, "escalate", eligible=False)
    if eligible:
        action: Action = "merge+health" if cls.tier == Tier.TIER_1 else "merge"
        return Decision(pr, cls, action, eligible=True)
    # Not eligible and not Tier 2: a failing/conflicted PR needs a human; anything
    # else (too new, behind, tier disabled) is just skipped until a later run.
    if pr.check_status == CheckStatus.FAILED or pr.mergeable == "CONFLICTING":
        return Decision(pr, cls, "escalate", eligible=False, skip_reason=reason)
    return Decision(pr, cls, "skip", eligible=False, skip_reason=reason)


def _merge_and_capture(
    gh: GitHubClient, decision: Decision, cfg: Config
) -> MergeOutcome:
    """Merge a PR and capture the merge commit SHA for the health/rollback path."""
    pr = decision.pr
    merge_pr(gh, pr_id=pr.id, merge_method=pr.merge_method)
    info = gh.query(_MERGE_INFO_QUERY, variables={"id": pr.id})
    node = (info or {}).get("node") or {}
    commit = node.get("mergeCommit") or {}
    return MergeOutcome(
        pr=pr,
        owner=cfg.organization,
        repo=pr.repo or "",
        tier=decision.classification.tier,
        merged=True,
        dry_run=False,
        merge_commit_sha=commit.get("oid"),
        merged_at=node.get("mergedAt"),
    )


def run(gh: GitHubClient, cfg: Config, now: datetime | None = None) -> RunResult:
    """Fetch, classify, decide, and (unless dry-run) merge eligible PRs."""
    now = now or datetime.now(timezone.utc)
    prs = fetch_dependency_prs(gh, organization=cfg.organization, labels=cfg.labels)
    cache = DeployModelCache(gh, cfg.organization)
    decisions = [decide(pr, cache, cfg, now) for pr in prs]

    # Drain safest + oldest first: Tier 0 before Tier 1, then oldest PR first.
    mergeable = [d for d in decisions if d.action in ("merge", "merge+health")]
    mergeable.sort(key=lambda d: (int(d.classification.tier), -_age_or(d.pr, now, 0.0)))

    merged_count = 0
    for d in mergeable:
        if merged_count >= cfg.max_merges_per_run:
            d.action = "skip"
            d.skip_reason = f"max_merges_per_run cap ({cfg.max_merges_per_run}) reached"
            continue
        if cfg.dry_run:
            merged_count += 1  # would merge
            continue
        try:
            d.outcome = _merge_and_capture(gh, d, cfg)
            merged_count += 1
        except Exception as exc:  # noqa: BLE001 - report and continue the batch
            d.action = "skip"
            d.skip_reason = f"merge failed: {exc!r}"

    return RunResult(decisions=decisions, dry_run=cfg.dry_run)


def format_summary(result: RunResult, cfg: Config) -> str:
    """Human-readable run summary for Action logs / stdout."""
    verb = "Would merge" if result.dry_run else "Merged"
    lines: list[str] = []
    mode = "DRY-RUN" if result.dry_run else "LIVE"
    lines.append(
        f"Dependabot auto-merge [{mode}] org={cfg.organization} "
        f"tiers={cfg.tiers_enabled} min_age={cfg.min_age_days}d"
    )
    merged = result.merged
    lines.append(f"\n{verb} {len(merged)} PR(s):")
    for d in sorted(merged, key=lambda x: (x.pr.repo or "", x.pr.group_name)):
        tag = "T1/health" if d.action == "merge+health" else "T0"
        lines.append(f"  [{tag}] {d.pr.repo}: {d.pr.group_name} -> {d.pr.url}")

    escalated = result.escalated
    lines.append(f"\nEscalate to humans ({len(escalated)}):")
    for d in sorted(escalated, key=lambda x: (x.pr.repo or "", x.pr.group_name)):
        why = "; ".join(d.classification.reasons) or d.skip_reason or "needs review"
        lines.append(f"  {d.pr.repo}: {d.pr.group_name} ({why}) -> {d.pr.url}")

    skipped = result.skipped
    lines.append(f"\nSkipped ({len(skipped)}): not yet eligible")
    for d in sorted(skipped, key=lambda x: (x.pr.repo or "", x.pr.group_name)):
        lines.append(f"  {d.pr.repo}: {d.pr.group_name} ({d.skip_reason})")
    return "\n".join(lines)


def main() -> int:
    parser = ArgumentParser(description="Daily Dependabot auto-merger")
    parser.add_argument("organization", nargs="?", default=None)
    parser.add_argument("--config", default="automation.yml")
    parser.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        default=None,
        help="Actually merge (default: dry-run). Requires a merge-capable token.",
    )
    parser.add_argument("--max-merges", type=int, default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.organization:
        cfg.organization = args.organization
    if args.dry_run is False:
        cfg.dry_run = False
    if args.max_merges is not None:
        cfg.max_merges_per_run = args.max_merges

    gh = GitHubClient.init()
    result = run(gh, cfg)
    print(format_summary(result, cfg))

    _maybe_post_slack(result, cfg)
    _maybe_run_health_gate(gh, result, cfg)
    return 0


def _maybe_post_slack(result: RunResult, cfg: Config) -> None:
    token = os.environ.get("SLACK_TOKEN")
    if not (token and cfg.slack_channel):
        return
    try:
        from .slack import SlackClient
        from .slack_messages import format_run_digest

        SlackClient(token).post_message(
            cfg.slack_channel, format_run_digest(result, cfg)
        )
    except Exception as exc:  # noqa: BLE001 - Slack failures must not fail the run
        print(f"Slack post failed: {exc!r}", file=sys.stderr)


def _maybe_run_health_gate(gh: GitHubClient, result: RunResult, cfg: Config) -> None:
    """For live Tier-1 merges, verify post-deploy health and roll back on failure."""
    if result.dry_run or not result.health_watch:
        return
    try:
        from .orchestrator import health_gate_outcomes
    except ImportError:
        return
    health_gate_outcomes(gh, result.health_watch, cfg)


if __name__ == "__main__":
    sys.exit(main())
