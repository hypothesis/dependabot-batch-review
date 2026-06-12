from dependabot_batch_review.automerge import Decision, RunResult
from dependabot_batch_review.automation_types import (
    HealthVerdict,
    MergeOutcome,
    SignalResult,
    Tier,
)
from dependabot_batch_review.config import Config
from dependabot_batch_review.risk import classify
from dependabot_batch_review.rollback import RollbackResult
from dependabot_batch_review.slack_messages import format_rollback, format_run_digest
from dependabot_batch_review.triage import TriageResult
from tests.helpers import library_model, make_pr, service_model


def _decision(pr, model, action):
    return Decision(
        pr=pr,
        classification=classify(pr, model),
        action=action,
        eligible=action.startswith("merge"),
    )


def test_run_digest_contains_sections_and_links():
    merged = _decision(
        make_pr("ruff", "0.1.0", "0.1.1", number=1), library_model(), "merge"
    )
    escalated = _decision(
        make_pr("newrelic", "11.0.1", "13.1.0", number=2), service_model(), "escalate"
    )
    result = RunResult(decisions=[merged, escalated], dry_run=True)
    message = format_run_digest(result, Config())

    assert "DRY-RUN" in message
    assert "Would merge" in message
    assert "newrelic" in message
    assert "<https://github.com/hypothesis/bouncer/pull/2|#2>" in message


def test_rollback_message_has_diagnosis_and_triage():
    outcome = MergeOutcome(
        pr=make_pr("cryptography", "44.0.1", "46.0.0", number=3),
        owner="hypothesis",
        repo="bouncer",
        tier=Tier.TIER_1,
        merged=True,
        dry_run=False,
        merge_commit_sha="abcdef1234567",
    )
    verdict = HealthVerdict(
        healthy=False,
        signals={
            "newrelic": SignalResult(
                "newrelic", False, "error rate", detail="error-rate 7% vs 0.3%"
            )
        },
        reasons=["newrelic"],
    )
    rollback = RollbackResult(
        performed=True,
        revert_pr_url="https://github.com/hypothesis/bouncer/pull/999",
        revert_commit_sha="r1",
        reason="reverted and merged",
        dry_run=False,
    )
    triage = TriageResult(
        summary="major bump with API removals",
        recommendation="manual-review",
        confidence="high",
    )
    message = format_rollback(outcome, verdict, rollback, triage)

    assert "AUTO-ROLLBACK" in message
    assert "revert PR" in message
    assert "Claude triage" in message
    assert "error-rate 7%" in message
