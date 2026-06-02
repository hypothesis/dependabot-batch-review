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
