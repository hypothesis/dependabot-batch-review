from dependabot_batch_review.triage import triage_pr
from tests.helpers import make_pr


def test_degrades_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = triage_pr(make_pr("newrelic", "11.0.1", "13.1.0"))
    assert result.degraded is True
    # Major bump -> analyze_risk High -> manual-review.
    assert result.recommendation == "manual-review"


def test_degrades_when_anthropic_unavailable():
    # anthropic is not a hard dependency; a passed key still degrades gracefully
    # (ImportError or API failure both fall back to the deterministic heuristic).
    result = triage_pr(make_pr("ruff", "0.1.0", "0.1.1"), api_key="fake-key")
    assert result.degraded is True
    assert result.summary
