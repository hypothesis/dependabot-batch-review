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
# separately via check_status). Checked at merge time via _PREMERGE_QUERY — the
# org-wide search omits mergeStateStatus (it 502s at that volume), so the field
# is always None during classification.
_BLOCKED_MERGE_STATES = frozenset({"DIRTY", "BLOCKED", "BEHIND", "DRAFT"})

# Live re-verification of a single PR immediately before merging. The org-wide
# search snapshot can be minutes old; everything security-relevant is re-read
# here: head OID (pinned via expectedHeadOid), commit authorship + signature,
# CI rollup, merge state, and head-commit age.
_PREMERGE_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on PullRequest {
      state
      headRefOid
      mergeStateStatus
      mergeable
      commits(last: 1) {
        totalCount
        nodes {
          commit {
            committedDate
            statusCheckRollup { state }
            signature { isValid }
            author { email }
          }
        }
      }
    }
  }
}
"""

_DEPENDABOT_EMAIL_SUFFIX = "dependabot[bot]@users.noreply.github.com"


class PreMergeCheckError(Exception):
    """A PR failed live re-verification just before merging."""

    def __init__(self, reason: str, *, escalate: bool = True) -> None:
        super().__init__(reason)
        self.escalate = escalate


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


def _iso_age_days(timestamp: str | None, now: datetime) -> float | None:
    """Age in days of an ISO-8601 timestamp, or ``None`` if unparseable."""
    if not timestamp:
        return None
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - moment).total_seconds() / 86400.0


def age_days(pr: DependencyUpdatePR, now: datetime) -> float | None:
    """Age of the PR in days, or ``None`` if the creation time is unknown."""
    return _iso_age_days(pr.created_at, now)


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


def verify_premerge(
    gh: GitHubClient, pr: DependencyUpdatePR, cfg: Config, now: datetime
) -> str:
    """
    Re-verify a PR against live GitHub state and return its head OID.

    Raises :class:`PreMergeCheckError` unless the PR is open, consists of exactly
    one commit authored by Dependabot with a valid (GitHub-made) signature, has a
    passing CI rollup *now*, is in a mergeable state, and its head commit — not
    just the PR — satisfies the age floor (a force-pushed new version restarts
    the quarantine clock).
    """
    info = gh.query(_PREMERGE_QUERY, variables={"id": pr.id})
    node = (info or {}).get("node") or {}
    if node.get("state") != "OPEN":
        raise PreMergeCheckError(
            f"not open at merge time ({node.get('state') or 'missing'})",
            escalate=False,
        )

    commits = node.get("commits") or {}
    total = commits.get("totalCount") or 0
    nodes = commits.get("nodes") or []
    if total != 1 or not nodes:
        raise PreMergeCheckError(
            f"expected exactly one Dependabot commit, found {total} "
            "(manual commits on a Dependabot branch need human review)"
        )
    commit = nodes[0].get("commit") or {}

    email = ((commit.get("author") or {}).get("email") or "").lower()
    if not email.endswith(_DEPENDABOT_EMAIL_SUFFIX):
        raise PreMergeCheckError(
            f"head commit not authored by dependabot ({email or 'unknown author'})"
        )
    if not (commit.get("signature") or {}).get("isValid"):
        # Dependabot commits are GitHub-signed; a forged author email can't be.
        raise PreMergeCheckError("head commit signature missing or invalid")

    rollup = (commit.get("statusCheckRollup") or {}).get("state")
    if rollup != "SUCCESS":
        raise PreMergeCheckError(
            f"CI rollup {rollup or 'missing'} at merge time",
            escalate=rollup not in (None, "PENDING", "EXPECTED"),
        )

    if node.get("mergeStateStatus") in _BLOCKED_MERGE_STATES:
        raise PreMergeCheckError(
            f"merge state {node.get('mergeStateStatus')}", escalate=False
        )
    if node.get("mergeable") == "CONFLICTING":
        raise PreMergeCheckError("merge conflict")

    head_age = _iso_age_days(commit.get("committedDate"), now)
    if head_age is None:
        raise PreMergeCheckError("unknown head commit age")
    if head_age < cfg.min_age_days:
        raise PreMergeCheckError(
            f"head commit too new ({head_age:.1f}d < {cfg.min_age_days}d)",
            escalate=False,
        )

    head_oid = node.get("headRefOid")
    if not head_oid:
        raise PreMergeCheckError("missing head OID")
    return str(head_oid)


def monitoring_configured(cfg: Config) -> bool:
    """Is at least one health-gate signal (Sentry or New Relic) credentialed?"""
    health = cfg.health
    return bool(
        health.sentry_token or (health.newrelic_token and health.newrelic_account_id)
    )


def _merge_and_capture(
    gh: GitHubClient, decision: Decision, cfg: Config, now: datetime | None = None
) -> MergeOutcome:
    """Re-verify, merge pinned to the verified head, and capture the merge SHA."""
    pr = decision.pr
    head_oid = verify_premerge(gh, pr, cfg, now or datetime.now(timezone.utc))
    merged = merge_pr(
        gh, pr_id=pr.id, merge_method=pr.merge_method, expected_head_oid=head_oid
    )
    commit = merged.get("mergeCommit") or {}
    return MergeOutcome(
        pr=pr,
        owner=cfg.organization,
        repo=pr.repo or "",
        tier=decision.classification.tier,
        merged=True,
        dry_run=False,
        merge_commit_sha=commit.get("oid"),
        merged_at=merged.get("mergedAt"),
    )


def gather_decisions(
    gh: GitHubClient,
    cfg: Config,
    now: datetime,
    repos: list[str] | None = None,
) -> list[Decision]:
    """Fetch and classify the org's open Dependabot PRs (shared by all CLIs)."""
    prs = fetch_dependency_prs(gh, organization=cfg.organization, labels=cfg.labels)
    if repos:
        wanted = set(repos)
        prs = [p for p in prs if p.repo in wanted]
    cache = DeployModelCache(
        gh, cfg.organization, publish_on_merge_repos=cfg.publish_on_merge_repos
    )
    return [decide(pr, cache, cfg, now) for pr in prs]


def run(gh: GitHubClient, cfg: Config, now: datetime | None = None) -> RunResult:
    """Fetch, classify, decide, and (unless dry-run) merge eligible PRs."""
    now = now or datetime.now(timezone.utc)
    decisions = gather_decisions(gh, cfg, now)

    # Drain safest + oldest first: Tier 0 before Tier 1, then oldest PR first.
    mergeable = [d for d in decisions if d.action in ("merge", "merge+health")]
    mergeable.sort(key=lambda d: (int(d.classification.tier), -_age_or(d.pr, now, 0.0)))

    can_watch_health = monitoring_configured(cfg)
    merged_count = 0
    for d in mergeable:
        if merged_count >= cfg.max_merges_per_run:
            d.action = "skip"
            d.skip_reason = f"max_merges_per_run cap ({cfg.max_merges_per_run}) reached"
            continue
        if d.action == "merge+health" and not cfg.dry_run and not can_watch_health:
            # Without Sentry/New Relic credentials the gate would be blind;
            # refuse the merge rather than deploy unverifiable code.
            d.action = "skip"
            d.skip_reason = (
                "tier 1 needs SENTRY_AUTH_TOKEN or NEW_RELIC_* credentials "
                "(health gate would have no signals)"
            )
            continue
        if cfg.dry_run:
            merged_count += 1  # would merge
            continue
        try:
            d.outcome = _merge_and_capture(gh, d, cfg, now)
            merged_count += 1
        except PreMergeCheckError as exc:
            d.action = "escalate" if exc.escalate else "skip"
            d.skip_reason = f"pre-merge verification: {exc}"
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
    # Imported here (not at module top) only to keep the engine unit-testable
    # without the health/rollback dependency graph; an import failure is a real
    # bug and must crash the run, never silently skip the gate.
    from .orchestrator import health_gate_outcomes

    health_gate_outcomes(gh, result.health_watch, cfg)


if __name__ == "__main__":
    sys.exit(main())
