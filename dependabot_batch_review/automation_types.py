"""
Shared data types for the autonomous Dependabot automation layer.

Kept in a dedicated, dependency-light module so the engine (``automerge`` /
``bulk``), the health gate (``health``), the rollback path (``rollback``) and the
TUI (``monitor``) can share types without import cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from .review import DependencyUpdatePR


class Tier(IntEnum):
    """Auto-merge risk tier. Higher = more dangerous."""

    TIER_0 = 0  # never deploys to prod -> auto-merge on CI pass
    TIER_1 = 1  # deploys to prod (patch/minor) -> auto-merge behind health gate
    TIER_2 = 2  # major / security-sensitive / broken -> human review only


@dataclass
class MergeOutcome:
    """The result of merging one PR; the input to the post-deploy health gate."""

    pr: DependencyUpdatePR
    owner: str
    repo: str
    tier: Tier
    merged: bool
    dry_run: bool
    merge_commit_sha: str | None = None
    merged_at: str | None = None


@dataclass
class SignalResult:
    """One monitoring signal's contribution to a health verdict."""

    source: str  # "sentry" | "newrelic"
    healthy: bool
    metric: str
    observed: float | None = None
    baseline: float | None = None
    threshold: float | None = None
    detail: str = ""
    # The signal could not be sampled (API error, no data). Never counts as
    # healthy; whether it triggers rollback or escalation is decided above.
    unknown: bool = False


@dataclass
class HealthVerdict:
    """
    Aggregate verdict from all monitoring signals after a Tier-1 deploy.

    Three-state: ``healthy`` (verified good), degraded (``healthy=False,
    unknown=False`` — roll back), or ``unknown=True`` (could not verify —
    escalate to humans, never auto-pass and never auto-rollback).
    """

    healthy: bool
    signals: dict[str, SignalResult] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    unknown: bool = False
