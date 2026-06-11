"""
Post-deploy health gate for Tier-1 (production-deploying) merges.

After a Tier-1 PR merges and the production deploy settles, this samples two
independent monitoring signals — Sentry (new unresolved issues + release-health
crash-free rate) and New Relic (error rate vs a pre-deploy baseline). Per the
configured policy, **either** signal degrading marks the deploy unhealthy, which
triggers an auto-rollback.

External I/O is via ``requests`` (already a dependency). ``now`` and ``sleep`` are
injectable so the deploy-wait loop is deterministic under test.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from .automation_types import HealthVerdict, MergeOutcome, SignalResult
from .config import HealthConfig
from .github_client import GitHubClient

_NowFn = Callable[[], datetime]
_SleepFn = Callable[[float], None]

_DEPLOY_QUERY = """
query($owner: String!, $name: String!, $sha: GitObjectID!) {
  repository(owner: $owner, name: $name) {
    object(oid: $sha) {
      ... on Commit {
        deployments(first: 10, environments: ["Production", "production", "prod"]) {
          nodes { environment state latestStatus { state logUrl } }
        }
      }
    }
  }
}
"""

_DEPLOY_SUCCESS = frozenset({"SUCCESS", "ACTIVE"})
_DEPLOY_FAILURE = frozenset({"FAILURE", "ERROR"})


@dataclass
class DeployResult:
    state: str  # "success" | "failure" | "timeout" | "none"
    log_url: str | None = None


# --------------------------------------------------------------------------- Sentry


class SentryClient:
    def __init__(
        self, token: str, org: str, base_url: str = "https://sentry.io/api/0"
    ) -> None:
        self._token = token
        self._org = org
        self._base = base_url.rstrip("/")
        self._project_ids: dict[str, str] = {}

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        response = requests.get(
            f"{self._base}{path}",
            headers={"Authorization": f"Bearer {self._token}"},
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def resolve_project_id(self, slug: str) -> str:
        if slug not in self._project_ids:
            projects = self._get(f"/organizations/{self._org}/projects/", {})
            for project in projects:
                self._project_ids[str(project["slug"])] = str(project["id"])
        return self._project_ids.get(slug, slug)

    def new_issues(self, project_id: str, since_min: int) -> int:
        issues = self._get(
            f"/organizations/{self._org}/issues/",
            {
                "project": project_id,
                "query": f"is:unresolved firstSeen:-{since_min}m",
                "statsPeriod": f"{since_min}m",
            },
        )
        return len(issues) if isinstance(issues, list) else 0

    def crash_free_rate(self, project_id: str, window_min: int) -> float | None:
        data = self._get(
            f"/organizations/{self._org}/sessions/",
            {
                "project": project_id,
                "field": "crash_free_rate(session)",
                "statsPeriod": f"{window_min}m",
                "interval": "1m",
            },
        )
        groups = data.get("groups") if isinstance(data, dict) else None
        if not groups:
            return None
        totals = groups[0].get("totals", {})
        rate = totals.get("crash_free_rate(session)")
        return float(rate) * 100.0 if rate is not None else None


def sample_sentry(
    client: SentryClient, config: HealthConfig, repo: str
) -> SignalResult:
    window = config.health_window_min
    thresholds = config.thresholds
    try:
        project = client.resolve_project_id(config.sentry_project_for(repo))
        new_issues = client.new_issues(project, window)
        crash_free = client.crash_free_rate(project, window)
    except requests.RequestException as exc:
        # Fail closed: an unreachable monitor is an UNVERIFIED deploy, not a
        # healthy one (expired token, rate limit, Sentry outage).
        return SignalResult(
            "sentry", False, "sentry", detail=f"query failed: {exc}", unknown=True
        )

    issues_ok = new_issues < thresholds.new_issue_fail_count
    crash_ok = crash_free is None or crash_free >= thresholds.min_crash_free_pct
    healthy = issues_ok and crash_ok
    crash_str = f"{crash_free:.2f}%" if crash_free is not None else "n/a"
    return SignalResult(
        source="sentry",
        healthy=healthy,
        metric="new issues / crash-free",
        observed=float(new_issues),
        threshold=float(thresholds.new_issue_fail_count),
        detail=f"crash-free {crash_str}, {new_issues} new issue(s)",
    )


# ------------------------------------------------------------------------ New Relic


class NewRelicClient:
    def __init__(
        self,
        token: str,
        account_id: int,
        endpoint: str = "https://api.newrelic.com/graphql",
    ) -> None:
        self._token = token
        self._account_id = account_id
        self._endpoint = endpoint

    def nrql(self, query: str) -> list[dict[str, Any]]:
        graphql = (
            "query($id: Int!, $q: Nrql!) { actor { account(id: $id) { "
            "nrql(query: $q) { results } } } }"
        )
        response = requests.post(
            self._endpoint,
            headers={"API-Key": self._token, "Content-Type": "application/json"},
            json={"query": graphql, "variables": {"id": self._account_id, "q": query}},
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        results = (
            body.get("data", {})
            .get("actor", {})
            .get("account", {})
            .get("nrql", {})
            .get("results")
        )
        return results if isinstance(results, list) else []

    def error_rate(self, app: str, window_min: int, until_min: int = 0) -> float | None:
        since = window_min + until_min
        query = (
            "SELECT percentage(count(*), WHERE error IS true) AS rate "
            f"FROM Transaction WHERE appName = '{app}' "
            f"SINCE {since} minutes ago UNTIL {until_min} minutes ago"
        )
        results = self.nrql(query)
        if not results:
            return None
        rate = results[0].get("rate")
        return float(rate) if rate is not None else None

    def error_count(self, app: str, window_min: int) -> int:
        query = (
            "SELECT count(*) AS c FROM TransactionError "
            f"WHERE appName = '{app}' SINCE {window_min} minutes ago"
        )
        results = self.nrql(query)
        if not results:
            return 0
        return int(results[0].get("c", 0))


def sample_newrelic(
    client: NewRelicClient, config: HealthConfig, repo: str
) -> SignalResult:
    app = config.newrelic_app_for(repo)
    window = config.health_window_min
    thresholds = config.thresholds
    try:
        post_rate = client.error_rate(app, window, until_min=0)
        baseline_rate = client.error_rate(
            app, config.baseline_window_min, until_min=window
        )
        post_count = client.error_count(app, window)
    except requests.RequestException as exc:
        return SignalResult(
            "newrelic", False, "newrelic", detail=f"query failed: {exc}", unknown=True
        )

    if post_rate is None:
        # No transaction data for the app name almost always means a config
        # mismatch, not a healthy idle service — treat as unverifiable.
        return SignalResult(
            "newrelic", False, "newrelic", detail="no data", unknown=True
        )

    baseline = baseline_rate or 0.0
    ceiling = baseline * (1.0 + thresholds.error_delta_pct / 100.0)
    spiked = post_rate > ceiling and post_rate > 0.0
    cold_start = baseline <= 0.01 and post_count >= thresholds.nr_error_count_abs
    healthy = not (spiked or cold_start)
    return SignalResult(
        source="newrelic",
        healthy=healthy,
        metric="error rate",
        observed=post_rate,
        baseline=baseline,
        threshold=ceiling,
        detail=f"error-rate {post_rate:.2f}% vs {baseline:.2f}% baseline, {post_count} errors",
    )


# ------------------------------------------------------------------------- deploy wait


def wait_for_deploy(
    gh: GitHubClient,
    outcome: MergeOutcome,
    config: HealthConfig,
    now: _NowFn,
    sleep: _SleepFn,
) -> DeployResult:
    """Poll GitHub Deployments for the Production env until the merge SHA settles."""
    if not outcome.merge_commit_sha:
        return DeployResult(state="none")
    deadline = now().timestamp() + config.deploy_wait_timeout_s
    while now().timestamp() < deadline:
        try:
            result = gh.query(
                _DEPLOY_QUERY,
                variables={
                    "owner": outcome.owner,
                    "name": outcome.repo,
                    "sha": outcome.merge_commit_sha,
                },
            )
        except Exception:  # noqa: BLE001 - transient; keep polling until deadline
            sleep(config.deploy_poll_interval_s)
            continue
        obj = (result or {}).get("repository", {}).get("object") or {}
        nodes = (obj.get("deployments") or {}).get("nodes") or []
        for node in nodes:
            state = str(node.get("state", "")).upper()
            log_url = (node.get("latestStatus") or {}).get("logUrl")
            if state in _DEPLOY_SUCCESS:
                return DeployResult(state="success", log_url=log_url)
            if state in _DEPLOY_FAILURE:
                return DeployResult(state="failure", log_url=log_url)
        sleep(config.deploy_poll_interval_s)
    return DeployResult(state="timeout")


# ------------------------------------------------------------------------- the gate


def check_health(
    gh: GitHubClient,
    outcome: MergeOutcome,
    config: HealthConfig,
    now: _NowFn | None = None,
    sleep: _SleepFn | None = None,
) -> HealthVerdict:
    """Wait for the deploy, sample Sentry + New Relic, and combine into a verdict."""
    now = now or (lambda: datetime.now(timezone.utc))
    sleep = sleep or time.sleep

    deploy = wait_for_deploy(gh, outcome, config, now, sleep)
    if deploy.state == "failure":
        return HealthVerdict(
            healthy=False,
            reasons=[f"production deploy failed ({deploy.log_url or 'no log'})"],
        )
    if deploy.state in ("timeout", "none"):
        # We never saw the new code go live; sampling now would measure the OLD
        # release and pass vacuously. Cannot verify -> escalate, don't guess.
        why = (
            "deploy did not settle within timeout"
            if deploy.state == "timeout"
            else "no merge commit SHA / no Production deployment found"
        )
        return HealthVerdict(
            healthy=False, reasons=[f"{why}; health not verifiable"], unknown=True
        )

    has_sentry = bool(config.sentry_token)
    has_newrelic = bool(config.newrelic_token and config.newrelic_account_id)
    if not (has_sentry or has_newrelic):
        return HealthVerdict(
            healthy=False,
            reasons=["no monitoring signals configured; health not verifiable"],
            unknown=True,
        )

    # Soak so the sampling window contains post-deploy traffic; without this the
    # backwards-looking windows would mostly measure the previous release.
    soak_min = (
        config.health_window_min
        if config.post_deploy_soak_min is None
        else config.post_deploy_soak_min
    )
    if soak_min > 0:
        sleep(soak_min * 60)

    signals: dict[str, SignalResult] = {}
    if has_sentry and config.sentry_token:
        signals["sentry"] = sample_sentry(
            SentryClient(config.sentry_token, config.sentry_org), config, outcome.repo
        )
    if has_newrelic and config.newrelic_token and config.newrelic_account_id:
        signals["newrelic"] = sample_newrelic(
            NewRelicClient(config.newrelic_token, int(config.newrelic_account_id)),
            config,
            outcome.repo,
        )

    reasons = [
        f"{signal.source}: {signal.detail}"
        for signal in signals.values()
        if not signal.healthy
    ]
    # Hard evidence of degradation outranks an unverifiable sibling signal;
    # otherwise any unknown signal makes the whole verdict unknown.
    degraded = any(not s.healthy and not s.unknown for s in signals.values())
    unknown = not degraded and any(s.unknown for s in signals.values())
    healthy = all(signal.healthy for signal in signals.values())
    return HealthVerdict(
        healthy=healthy, signals=signals, reasons=reasons, unknown=unknown
    )
