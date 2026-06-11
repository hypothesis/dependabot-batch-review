from datetime import datetime, timezone

import responses

from dependabot_batch_review.automation_types import MergeOutcome, Tier
from dependabot_batch_review.config import HealthConfig
from dependabot_batch_review.health import (
    NewRelicClient,
    SentryClient,
    check_health,
    sample_newrelic,
    sample_sentry,
    wait_for_deploy,
)
from tests.helpers import make_pr

SENTRY = "https://sentry.io/api/0"
NR = "https://api.newrelic.com/graphql"


def _outcome(sha="abc1234"):
    return MergeOutcome(
        pr=make_pr("cryptography", "44.0.1", "44.0.2"),
        owner="hypothesis",
        repo="bouncer",
        tier=Tier.TIER_1,
        merged=True,
        dry_run=False,
        merge_commit_sha=sha,
    )


def _nr(results):
    return {"data": {"actor": {"account": {"nrql": {"results": results}}}}}


@responses.activate
def test_sentry_healthy():
    responses.add(
        responses.GET,
        f"{SENTRY}/organizations/hypothesis/projects/",
        json=[{"slug": "bouncer", "id": "42"}],
    )
    responses.add(responses.GET, f"{SENTRY}/organizations/hypothesis/issues/", json=[])
    responses.add(
        responses.GET,
        f"{SENTRY}/organizations/hypothesis/sessions/",
        json={"groups": [{"totals": {"crash_free_rate(session)": 0.9999}}]},
    )
    cfg = HealthConfig(sentry_org="hypothesis", sentry_token="t")
    signal = sample_sentry(SentryClient("t", "hypothesis"), cfg, "bouncer")
    assert signal.healthy is True


@responses.activate
def test_sentry_new_issue_fails():
    responses.add(
        responses.GET,
        f"{SENTRY}/organizations/hypothesis/projects/",
        json=[{"slug": "bouncer", "id": "42"}],
    )
    responses.add(
        responses.GET,
        f"{SENTRY}/organizations/hypothesis/issues/",
        json=[{"id": "1", "title": "KeyError"}],
    )
    responses.add(
        responses.GET,
        f"{SENTRY}/organizations/hypothesis/sessions/",
        json={"groups": [{"totals": {"crash_free_rate(session)": 0.999}}]},
    )
    cfg = HealthConfig(sentry_org="hypothesis", sentry_token="t")
    signal = sample_sentry(SentryClient("t", "hypothesis"), cfg, "bouncer")
    assert signal.healthy is False


@responses.activate
def test_newrelic_error_spike_fails():
    responses.add(responses.POST, NR, json=_nr([{"rate": 10.0}]))  # post window
    responses.add(responses.POST, NR, json=_nr([{"rate": 0.3}]))  # baseline
    responses.add(responses.POST, NR, json=_nr([{"c": 50}]))  # error count
    cfg = HealthConfig(newrelic_token="k", newrelic_account_id="1")
    signal = sample_newrelic(NewRelicClient("k", 1), cfg, "bouncer")
    assert signal.healthy is False


@responses.activate
def test_newrelic_steady_state_ok():
    responses.add(responses.POST, NR, json=_nr([{"rate": 0.31}]))
    responses.add(responses.POST, NR, json=_nr([{"rate": 0.30}]))
    responses.add(responses.POST, NR, json=_nr([{"c": 2}]))
    cfg = HealthConfig(newrelic_token="k", newrelic_account_id="1")
    signal = sample_newrelic(NewRelicClient("k", 1), cfg, "bouncer")
    assert signal.healthy is True


class _FakeGH:
    def __init__(self, states):
        self.states = states
        self.token = "x"
        self._i = 0

    def query(self, query, variables=None, extra_headers=None):
        state = self.states[min(self._i, len(self.states) - 1)]
        self._i += 1
        return {
            "repository": {
                "object": {
                    "deployments": {
                        "nodes": [{"state": state, "latestStatus": {"logUrl": "u"}}]
                    }
                }
            }
        }


def _clock():
    counter = {"t": 1000.0}

    def now():
        return datetime.fromtimestamp(counter["t"], tz=timezone.utc)

    def sleep(seconds):
        counter["t"] += max(seconds, 1.0)

    return now, sleep


def test_wait_for_deploy_success():
    gh = _FakeGH(["IN_PROGRESS", "SUCCESS"])
    now, sleep = _clock()
    cfg = HealthConfig(deploy_poll_interval_s=1, deploy_wait_timeout_s=100)
    result = wait_for_deploy(gh, _outcome(), cfg, now, sleep)
    assert result.state == "success"


def test_wait_for_deploy_failure():
    gh = _FakeGH(["FAILURE"])
    now, sleep = _clock()
    cfg = HealthConfig(deploy_poll_interval_s=1, deploy_wait_timeout_s=100)
    result = wait_for_deploy(gh, _outcome(), cfg, now, sleep)
    assert result.state == "failure"


def test_wait_for_deploy_timeout():
    gh = _FakeGH(["IN_PROGRESS"])
    now, sleep = _clock()
    cfg = HealthConfig(deploy_poll_interval_s=1, deploy_wait_timeout_s=3)
    result = wait_for_deploy(gh, _outcome(), cfg, now, sleep)
    assert result.state == "timeout"


def test_check_health_deploy_failure_is_unhealthy():
    gh = _FakeGH(["FAILURE"])
    now, sleep = _clock()
    cfg = HealthConfig(deploy_poll_interval_s=1, deploy_wait_timeout_s=10)
    verdict = check_health(gh, _outcome(), cfg, now=now, sleep=sleep)
    assert verdict.healthy is False


def test_check_health_no_signals_configured_is_unknown():
    gh = _FakeGH(["SUCCESS"])
    now, sleep = _clock()
    cfg = HealthConfig(deploy_poll_interval_s=1, deploy_wait_timeout_s=10)
    verdict = check_health(gh, _outcome(), cfg, now=now, sleep=sleep)
    # No tokens => no signals => the gate cannot verify anything. Fail closed:
    # not healthy, flagged unknown (escalate to humans, no auto-rollback).
    assert verdict.healthy is False
    assert verdict.unknown is True
    assert any("no monitoring signals" in r for r in verdict.reasons)


@responses.activate
def test_sentry_api_error_is_unknown_not_healthy():
    responses.add(
        responses.GET, f"{SENTRY}/organizations/hypothesis/projects/", status=401
    )
    cfg = HealthConfig(sentry_org="hypothesis", sentry_token="expired")
    signal = sample_sentry(SentryClient("expired", "hypothesis"), cfg, "bouncer")
    assert signal.healthy is False
    assert signal.unknown is True


@responses.activate
def test_newrelic_api_error_is_unknown_not_healthy():
    responses.add(responses.POST, NR, status=500)
    cfg = HealthConfig(newrelic_token="t", newrelic_account_id="1")
    signal = sample_newrelic(NewRelicClient("t", 1), cfg, "bouncer")
    assert signal.healthy is False
    assert signal.unknown is True


@responses.activate
def test_newrelic_no_data_is_unknown():
    responses.add(responses.POST, NR, json=_nr([]))
    cfg = HealthConfig(newrelic_token="t", newrelic_account_id="1")
    signal = sample_newrelic(NewRelicClient("t", 1), cfg, "bouncer")
    assert signal.healthy is False
    assert signal.unknown is True


def test_check_health_deploy_timeout_is_unknown_without_sampling():
    gh = _FakeGH(["IN_PROGRESS"])
    now, sleep = _clock()
    cfg = HealthConfig(
        deploy_poll_interval_s=1, deploy_wait_timeout_s=3, sentry_token="t"
    )
    verdict = check_health(gh, _outcome(), cfg, now=now, sleep=sleep)
    # Old release would be measured; the gate must not claim healthy.
    assert verdict.healthy is False
    assert verdict.unknown is True
    assert verdict.signals == {}


@responses.activate
def test_check_health_soaks_before_sampling():
    responses.add(
        responses.GET,
        f"{SENTRY}/organizations/hypothesis/projects/",
        json=[{"slug": "bouncer", "id": "42"}],
    )
    responses.add(responses.GET, f"{SENTRY}/organizations/hypothesis/issues/", json=[])
    responses.add(
        responses.GET,
        f"{SENTRY}/organizations/hypothesis/sessions/",
        json={"groups": [{"totals": {"crash_free_rate(session)": 1.0}}]},
    )
    gh = _FakeGH(["SUCCESS"])
    now, _ = _clock()
    sleeps: list[float] = []
    cfg = HealthConfig(
        deploy_poll_interval_s=1,
        deploy_wait_timeout_s=10,
        sentry_token="t",
        post_deploy_soak_min=2,
    )
    verdict = check_health(gh, _outcome(), cfg, now=now, sleep=sleeps.append)
    assert verdict.healthy is True
    assert 120 in sleeps  # soaked 2 minutes after the deploy settled
