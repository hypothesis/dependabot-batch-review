from types import SimpleNamespace

import pytest

from dependabot_batch_review import rollback
from dependabot_batch_review.rollback import _redact, _run, revert_merge


class FakeGH:
    def __init__(self, existing_pr=None):
        self.token = "tkn"
        self.existing = existing_pr
        self.calls: list[str] = []

    def query(self, query, variables=None, extra_headers=None):
        self.calls.append(query)
        if "pullRequests(headRefName" in query:
            nodes = [self.existing] if self.existing else []
            return {
                "repository": {
                    "id": "REPO1",
                    "defaultBranchRef": {"name": "main"},
                    "pullRequests": {"nodes": nodes},
                }
            }
        if "createPullRequest" in query:
            return {
                "createPullRequest": {
                    "pullRequest": {
                        "id": "REVERTPR",
                        "url": "https://github.com/hypothesis/bouncer/pull/999",
                    }
                }
            }
        if "mergePullRequest" in query:
            return {"mergePullRequest": {"pullRequest": {"merged": True, "url": "x"}}}
        return {}


def _fake_run(cmd, cwd=None, capture_output=True, text=True):
    out = ""
    if "rev-list" in cmd:
        out = "mergesha parentsha"  # 2 tokens => single-parent (squash) commit
    elif "rev-parse" in cmd:
        out = "revertsha123"
    return SimpleNamespace(returncode=0, stdout=out, stderr="")


def test_dry_run_does_nothing(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("subprocess must not run in dry-run")

    monkeypatch.setattr(rollback.subprocess, "run", boom)
    result = revert_merge(FakeGH(), "hypothesis", "bouncer", "abc1234", dry_run=True)
    assert result.performed is False
    assert result.dry_run is True
    assert "would `git revert`" in result.reason


def test_idempotent_when_revert_exists():
    existing = {"url": "https://github.com/hypothesis/bouncer/pull/500", "merged": True}
    result = revert_merge(FakeGH(existing), "hypothesis", "bouncer", "abc1234")
    assert result.performed is True
    assert result.revert_pr_url.endswith("/500")
    assert "already exists" in result.reason


def test_full_revert_opens_and_merges_pr(monkeypatch):
    monkeypatch.setattr(rollback.subprocess, "run", _fake_run)
    gh = FakeGH()
    result = revert_merge(
        gh,
        "hypothesis",
        "bouncer",
        "abc1234",
        original_title="cryptography 44 -> 46",
        original_pr_url="https://github.com/hypothesis/bouncer/pull/1",
    )
    assert result.performed is True
    assert result.revert_pr_url.endswith("/999")
    assert any("mergePullRequest" in call for call in gh.calls)


def test_revert_handles_git_failure(monkeypatch):
    def failing_run(cmd, cwd=None, capture_output=True, text=True):
        return SimpleNamespace(returncode=1, stdout="", stderr="conflict")

    monkeypatch.setattr(rollback.subprocess, "run", failing_run)
    result = revert_merge(FakeGH(), "hypothesis", "bouncer", "abc1234")
    assert result.performed is False
    assert "git revert -m 1" in result.reason  # manual fallback instructions


def test_redact_strips_clone_token():
    url = "https://x-access-token:ghp_SECRET123@github.com/hypothesis/bouncer.git"
    redacted = _redact(f"git clone {url} .")
    assert "ghp_SECRET123" not in redacted
    assert "x-access-token:***@" in redacted


def test_run_error_does_not_leak_token(monkeypatch):
    def failing_run(cmd, cwd=None, capture_output=True, text=True):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="fatal: https://x-access-token:ghp_SECRET@github.com/x/y.git denied",
        )

    monkeypatch.setattr(rollback.subprocess, "run", failing_run)
    with pytest.raises(RuntimeError) as excinfo:
        _run(
            [
                "git",
                "clone",
                "https://x-access-token:ghp_SECRET@github.com/x/y.git",
                ".",
            ],
            "/tmp",
        )
    assert "ghp_SECRET" not in str(excinfo.value)
