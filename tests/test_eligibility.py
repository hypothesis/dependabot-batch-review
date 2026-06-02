from datetime import datetime, timezone

from dependabot_batch_review import automerge
from dependabot_batch_review.automerge import decide, is_eligible, run
from dependabot_batch_review.config import Config
from dependabot_batch_review.review import CheckStatus
from dependabot_batch_review.risk import classify
from tests.helpers import library_model, make_pr, service_model

NOW = datetime(2026, 6, 2, tzinfo=timezone.utc)


class FakeCache:
    def __init__(self, model):
        self._model = model

    def get(self, repo):
        return self._model


def test_eligible_low_risk_old_passing():
    pr = make_pr("ruff", "0.1.0", "0.1.1", created_at="2026-05-01T00:00:00Z")
    cfg = Config(min_age_days=3, tiers_enabled=[0])
    ok, reason = is_eligible(pr, classify(pr, library_model()), cfg, NOW)
    assert ok and reason is None


def test_not_eligible_ci_failing():
    pr = make_pr("ruff", "0.1.0", "0.1.1", check=CheckStatus.FAILED)
    cfg = Config(tiers_enabled=[0])
    ok, reason = is_eligible(pr, classify(pr, library_model()), cfg, NOW)
    assert not ok and reason is not None and "CI" in reason


def test_not_eligible_too_new():
    pr = make_pr("ruff", "0.1.0", "0.1.1", created_at="2026-06-01T00:00:00Z")
    cfg = Config(min_age_days=3, tiers_enabled=[0])
    ok, reason = is_eligible(pr, classify(pr, library_model()), cfg, NOW)
    assert not ok and reason is not None and "too new" in reason


def test_not_eligible_tier_disabled():
    pr = make_pr("sentry-sdk", "2.58.0", "2.61.1")  # Tier 1 on a service
    cfg = Config(tiers_enabled=[0])
    ok, reason = is_eligible(pr, classify(pr, service_model()), cfg, NOW)
    assert not ok and reason is not None and "tier 1 not enabled" in reason


def test_not_eligible_merge_conflict():
    pr = make_pr("ruff", "0.1.0", "0.1.1", merge_state="DIRTY")
    cfg = Config(tiers_enabled=[0])
    ok, _ = is_eligible(pr, classify(pr, library_model()), cfg, NOW)
    assert not ok


def test_decide_escalates_failing_tier0():
    pr = make_pr("ruff", "0.1.0", "0.1.1", check=CheckStatus.FAILED)
    decision = decide(pr, FakeCache(library_model()), Config(tiers_enabled=[0]), NOW)
    assert decision.action == "escalate"


def test_run_dry_run_performs_no_merges(monkeypatch):
    prs = [
        make_pr("ruff", "0.1.0", "0.1.1", repo="bouncer", number=1),
        make_pr("newrelic", "11.0.1", "13.1.0", repo="bouncer", number=2),
    ]
    monkeypatch.setattr(
        automerge, "fetch_dependency_prs", lambda gh, organization, labels: prs
    )
    monkeypatch.setattr(
        automerge, "DeployModelCache", lambda gh, org: FakeCache(service_model())
    )

    def must_not_merge(*args, **kwargs):
        raise AssertionError("merge_pr must not be called during a dry run")

    monkeypatch.setattr(automerge, "merge_pr", must_not_merge)

    cfg = Config(dry_run=True, tiers_enabled=[0, 1], min_age_days=3)
    result = run(None, cfg, now=NOW)

    assert result.dry_run is True
    assert len(result.merged) == 1  # ruff (Tier 0)
    assert len(result.escalated) == 1  # newrelic major (Tier 2)


def test_run_respects_max_merges(monkeypatch):
    prs = [make_pr("ruff", "0.1.0", "0.1.1", repo="lib", number=i) for i in range(5)]
    monkeypatch.setattr(
        automerge, "fetch_dependency_prs", lambda gh, organization, labels: prs
    )
    monkeypatch.setattr(
        automerge, "DeployModelCache", lambda gh, org: FakeCache(library_model())
    )
    cfg = Config(dry_run=True, tiers_enabled=[0], max_merges_per_run=2)
    result = run(None, cfg, now=NOW)

    assert len(result.merged) == 2
    capped = [d for d in result.skipped if d.skip_reason and "cap" in d.skip_reason]
    assert len(capped) == 3
