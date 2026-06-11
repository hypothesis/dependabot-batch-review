"""
Configuration for the autonomous Dependabot automation layer.

Loads from an optional ``automation.yml`` file, then applies environment-variable
overrides (env always wins). The ``dry_run`` flag is fail-safe: only the explicit
strings ``false`` / ``0`` / ``no`` disable it, so a typo never turns merging on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _as_bool(value: str | None, default: bool) -> bool:
    # Empty counts as unset: the Action exports DBR_DRY_RUN="" on scheduled runs
    # so that automation.yml keeps the final say.
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"false", "0", "no", "off"}


def _as_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass
class Thresholds:
    """Health-gate pass/fail thresholds (all tunable per pilot)."""

    error_delta_pct: float = 50.0  # post-deploy error-rate increase that fails
    min_crash_free_pct: float = 99.0  # Sentry release-health floor
    new_issue_fail_count: int = 1  # >= this many brand-new issues fails
    nr_error_count_abs: int = 5  # absolute error floor when baseline traffic ~0


@dataclass
class HealthConfig:
    sentry_org: str = "hypothesis"
    sentry_token: str | None = None
    sentry_projects: dict[str, str] = field(default_factory=dict)
    newrelic_account_id: str | None = None
    newrelic_token: str | None = None
    newrelic_apps: dict[str, str] = field(default_factory=dict)
    deploy_wait_timeout_s: int = 1800  # 30 min — matches the EB pipeline length
    deploy_poll_interval_s: int = 20
    health_window_min: int = 15  # sampling window after deploy settles
    baseline_window_min: int = 60  # pre-deploy comparison window
    # Wait this long after the deploy settles before sampling, so the window
    # contains new-release traffic. None => health_window_min.
    post_deploy_soak_min: int | None = None
    thresholds: Thresholds = field(default_factory=Thresholds)

    def sentry_project_for(self, repo: str) -> str:
        return self.sentry_projects.get(repo, repo)

    def newrelic_app_for(self, repo: str) -> str:
        return self.newrelic_apps.get(repo, f"{repo} (prod)")


@dataclass
class Config:
    organization: str = "hypothesis"
    min_age_days: int = 3
    dry_run: bool = True  # SAFE DEFAULT — never merges unless explicitly disabled
    tiers_enabled: list[int] = field(default_factory=lambda: [0])
    repo_allow: list[str] = field(default_factory=list)  # empty => all repos
    repo_deny: list[str] = field(default_factory=list)
    max_merges_per_run: int = 10  # hard cap per invocation
    slack_channel: str | None = None
    labels: list[str] = field(default_factory=lambda: ["dependencies"])
    # npm-publishing frontend libs (no EB deploy.yml) whose npm bumps still ship.
    publish_on_merge_repos: list[str] = field(default_factory=list)
    health: HealthConfig = field(default_factory=HealthConfig)

    def tier_enabled(self, tier: int) -> bool:
        return tier in self.tiers_enabled

    def repo_permitted(self, repo: str) -> bool:
        if self.repo_allow and repo not in self.repo_allow:
            return False
        return repo not in self.repo_deny


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def load_config(path: str | None = "automation.yml") -> Config:
    """Build a :class:`Config` from YAML (if present) plus env overrides."""
    raw = _load_yaml(Path(path)) if path else {}

    health_raw = raw.get("health", {}) if isinstance(raw.get("health"), dict) else {}
    thresholds_raw = (
        health_raw.get("thresholds", {})
        if isinstance(health_raw.get("thresholds"), dict)
        else {}
    )
    thresholds = Thresholds(
        error_delta_pct=float(thresholds_raw.get("error_delta_pct", 50.0)),
        min_crash_free_pct=float(thresholds_raw.get("min_crash_free_pct", 99.0)),
        new_issue_fail_count=int(thresholds_raw.get("new_issue_fail_count", 1)),
        nr_error_count_abs=int(thresholds_raw.get("nr_error_count_abs", 5)),
    )
    health = HealthConfig(
        sentry_org=str(health_raw.get("sentry_org", "hypothesis")),
        sentry_token=os.environ.get("SENTRY_AUTH_TOKEN"),
        sentry_projects=dict(health_raw.get("sentry_projects", {})),
        newrelic_account_id=os.environ.get("NEW_RELIC_ACCOUNT_ID"),
        newrelic_token=os.environ.get("NEW_RELIC_API_KEY"),
        newrelic_apps=dict(health_raw.get("newrelic_apps", {})),
        deploy_wait_timeout_s=int(health_raw.get("deploy_wait_timeout_s", 1800)),
        deploy_poll_interval_s=int(health_raw.get("deploy_poll_interval_s", 20)),
        health_window_min=int(health_raw.get("health_window_min", 15)),
        baseline_window_min=int(health_raw.get("baseline_window_min", 60)),
        post_deploy_soak_min=(
            int(raw_soak)
            if (raw_soak := health_raw.get("post_deploy_soak_min")) is not None
            else None
        ),
        thresholds=thresholds,
    )
    if "SENTRY_ORG" in os.environ:
        health.sentry_org = os.environ["SENTRY_ORG"]

    config = Config(
        organization=str(raw.get("organization", "hypothesis")),
        min_age_days=int(raw.get("min_age_days", 3)),
        dry_run=bool(raw.get("dry_run", True)),
        tiers_enabled=[int(t) for t in raw.get("tiers_enabled", [0])],
        repo_allow=[str(r) for r in raw.get("repo_allow", [])],
        repo_deny=[str(r) for r in raw.get("repo_deny", [])],
        max_merges_per_run=int(raw.get("max_merges_per_run", 10)),
        slack_channel=raw.get("slack_channel"),
        labels=[str(label) for label in raw.get("labels", ["dependencies"])],
        publish_on_merge_repos=[str(r) for r in raw.get("publish_on_merge_repos", [])],
        health=health,
    )

    # Environment overrides (env always wins).
    config.organization = os.environ.get("DBR_ORG", config.organization)
    config.min_age_days = _as_int(
        os.environ.get("DBR_MIN_AGE_DAYS"), config.min_age_days
    )
    config.dry_run = _as_bool(os.environ.get("DBR_DRY_RUN"), config.dry_run)
    config.max_merges_per_run = _as_int(
        os.environ.get("DBR_MAX_MERGES"), config.max_merges_per_run
    )
    if os.environ.get("DBR_TIERS_ENABLED"):
        config.tiers_enabled = [
            int(t) for t in os.environ["DBR_TIERS_ENABLED"].split(",") if t.strip()
        ]
    config.slack_channel = os.environ.get("SLACK_CHANNEL", config.slack_channel)

    return config
