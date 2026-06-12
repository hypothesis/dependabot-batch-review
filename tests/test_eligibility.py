from datetime import datetime, timezone

import pytest

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
        automerge, "DeployModelCache", lambda gh, org, **kw: FakeCache(service_model())
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
        automerge, "DeployModelCache", lambda gh, org, **kw: FakeCache(library_model())
    )
    cfg = Config(dry_run=True, tiers_enabled=[0], max_merges_per_run=2)
    result = run(None, cfg, now=NOW)

    assert len(result.merged) == 2
    capped = [d for d in result.skipped if d.skip_reason and "cap" in d.skip_reason]
    assert len(capped) == 3


# ----------------------------------------------------------- pre-merge checks

DEPENDABOT_EMAIL = "49699333+dependabot[bot]@users.noreply.github.com"


class PremergeGH:
    def __init__(self, node):
        self.node = node

    def query(self, query, variables=None, extra_headers=None):
        return {"node": self.node}


def premerge_node(**overrides):
    commit = {
        "committedDate": "2026-05-01T00:00:00Z",
        "statusCheckRollup": {"state": "SUCCESS"},
        "signature": {"isValid": True},
        "author": {"email": DEPENDABOT_EMAIL},
    }
    commit.update(overrides.pop("commit", {}))
    node = {
        "state": "OPEN",
        "headRefOid": "deadbeef",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "commits": {"totalCount": 1, "nodes": [{"commit": commit}]},
    }
    node.update(overrides)
    return node


def test_verify_premerge_ok():
    from dependabot_batch_review.automerge import verify_premerge

    pr = make_pr("ruff", "0.1.0", "0.1.1")
    oid = verify_premerge(PremergeGH(premerge_node()), pr, Config(), NOW)
    assert oid == "deadbeef"


def test_verify_premerge_rejects_foreign_author():
    from dependabot_batch_review.automerge import PreMergeCheckError, verify_premerge

    node = premerge_node(commit={"author": {"email": "attacker@example.com"}})
    pr = make_pr("ruff", "0.1.0", "0.1.1")
    with pytest.raises(PreMergeCheckError) as exc:
        verify_premerge(PremergeGH(node), pr, Config(), NOW)
    assert exc.value.escalate is True


def test_verify_premerge_rejects_extra_commits():
    from dependabot_batch_review.automerge import PreMergeCheckError, verify_premerge

    node = premerge_node()
    node["commits"]["totalCount"] = 2
    pr = make_pr("ruff", "0.1.0", "0.1.1")
    with pytest.raises(PreMergeCheckError, match="exactly one"):
        verify_premerge(PremergeGH(node), pr, Config(), NOW)


def test_verify_premerge_rejects_invalid_signature():
    from dependabot_batch_review.automerge import PreMergeCheckError, verify_premerge

    node = premerge_node(commit={"signature": {"isValid": False}})
    pr = make_pr("ruff", "0.1.0", "0.1.1")
    with pytest.raises(PreMergeCheckError, match="signature"):
        verify_premerge(PremergeGH(node), pr, Config(), NOW)


def test_verify_premerge_rejects_stale_ci():
    from dependabot_batch_review.automerge import PreMergeCheckError, verify_premerge

    node = premerge_node(commit={"statusCheckRollup": {"state": "FAILURE"}})
    pr = make_pr("ruff", "0.1.0", "0.1.1")
    with pytest.raises(PreMergeCheckError) as exc:
        verify_premerge(PremergeGH(node), pr, Config(), NOW)
    assert exc.value.escalate is True


def test_verify_premerge_fresh_head_commit_restarts_quarantine():
    from dependabot_batch_review.automerge import PreMergeCheckError, verify_premerge

    # PR is old, but the head commit (force-pushed new version) is brand new.
    node = premerge_node(commit={"committedDate": "2026-06-01T20:00:00Z"})
    pr = make_pr("ruff", "0.1.0", "0.1.1", created_at="2026-05-01T00:00:00Z")
    with pytest.raises(PreMergeCheckError) as exc:
        verify_premerge(PremergeGH(node), pr, Config(min_age_days=3), NOW)
    assert exc.value.escalate is False  # just wait; not suspicious by itself


def test_run_live_skips_tier1_without_monitoring(monkeypatch):
    pr = make_pr("sentry-sdk", "2.58.0", "2.61.1", repo="bouncer", number=9)
    monkeypatch.setattr(
        automerge, "fetch_dependency_prs", lambda gh, organization, labels: [pr]
    )
    monkeypatch.setattr(
        automerge, "DeployModelCache", lambda gh, org, **kw: FakeCache(service_model())
    )

    def must_not_merge(*args, **kwargs):
        raise AssertionError("merge must not be attempted without a health gate")

    monkeypatch.setattr(automerge, "merge_pr", must_not_merge)
    monkeypatch.setattr(automerge, "verify_premerge", must_not_merge)

    cfg = Config(dry_run=False, tiers_enabled=[0, 1], min_age_days=3)
    result = run(None, cfg, now=NOW)

    assert result.merged == []
    skipped = [d for d in result.decisions if d.action == "skip"]
    assert any("health gate" in (d.skip_reason or "") for d in skipped)
