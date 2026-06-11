from datetime import datetime, timezone

from dependabot_batch_review.automerge import decide
from dependabot_batch_review.config import Config
from dependabot_batch_review.monitor import (
    PRState,
    build_rows,
    collect_events,
    dry_run_worker,
    reduce_events,
)
from tests.helpers import make_pr, service_model

NOW = datetime(2026, 6, 2, tzinfo=timezone.utc)


class FakeCache:
    def __init__(self, model):
        self._model = model

    def get(self, repo):
        return self._model


def _decisions():
    prs = [
        make_pr("ruff", "0.1.0", "0.1.1", number=1),
        make_pr("newrelic", "11.0.1", "13.1.0", number=2),
    ]
    cfg = Config(tiers_enabled=[0])
    cache = FakeCache(service_model())
    return [decide(pr, cache, cfg, NOW) for pr in prs]


def test_build_rows():
    rows = build_rows(_decisions())
    assert len(rows) == 2
    assert {r.package for r in rows} == {"ruff", "newrelic"}


def test_worker_drives_state_machine_to_terminal():
    decisions = _decisions()
    rows = build_rows(decisions)
    events = collect_events(dry_run_worker(decisions))
    model = reduce_events(rows, events)

    assert model.done is True
    states = {r.package: r.state for r in model.rows}
    assert states["ruff"] is PRState.WOULD_MERGE  # Tier 0
    assert states["newrelic"] is PRState.ESCALATED  # Tier 2


def test_live_worker_merges_audits_and_skips_unwatchable_tier1(monkeypatch, tmp_path):
    import json

    from dependabot_batch_review import monitor
    from dependabot_batch_review.audit import AuditLog
    from dependabot_batch_review.automation_types import MergeOutcome, Tier
    from dependabot_batch_review.monitor import live_worker

    t0 = make_pr("ruff", "0.1.0", "0.1.1", number=1, repo="lib")
    t1 = make_pr("sentry-sdk", "2.58.0", "2.58.1", number=2, repo="bouncer")
    cfg = Config(tiers_enabled=[0, 1], dry_run=False)
    cache = FakeCache(service_model())
    decisions = [decide(t0, cache, cfg, NOW) for t0 in [t0]] + [
        decide(t1, cache, cfg, NOW)
    ]
    # Make the first decision a plain Tier-0 merge regardless of the fake model.
    decisions[0].action = "merge"

    merged = []

    def fake_merge(gh, d, c, now=None):
        merged.append(d.pr.group_name)
        return MergeOutcome(
            pr=d.pr,
            owner="hypothesis",
            repo=d.pr.repo or "",
            tier=Tier(int(d.classification.tier)),
            merged=True,
            dry_run=False,
            merge_commit_sha="abc1234",
        )

    monkeypatch.setattr(monitor, "_merge_and_capture", fake_merge)

    audit = AuditLog(tmp_path / "audit.jsonl")
    # No SENTRY/NEW_RELIC credentials => the Tier-1 PR must be held, not merged.
    events = collect_events(live_worker(None, cfg, decisions, audit=audit))

    states = [e.state for e in events if e.kind == "row"]
    assert PRState.MERGED in states
    assert PRState.SKIPPED in states
    assert merged == ["ruff"]

    entries = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]
    assert any(e["event"] == "merged" and e["package"] == "ruff" for e in entries)
    assert any(
        e["event"] == "skipped" and e["reason"] == "no monitoring credentials"
        for e in entries
    )
