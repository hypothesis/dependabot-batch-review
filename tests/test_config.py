from pathlib import Path

from dependabot_batch_review.config import load_config

YAML = """
organization: hypothesis
min_age_days: 5
dry_run: true
tiers_enabled: [0, 1]
max_merges_per_run: 7
repo_deny: [workflows]
health:
  sentry_org: hypothesis
  health_window_min: 20
  thresholds:
    min_crash_free_pct: 98.5
"""


def _write(tmp_path: Path) -> str:
    path = tmp_path / "automation.yml"
    path.write_text(YAML)
    return str(path)


def test_yaml_load(tmp_path):
    cfg = load_config(_write(tmp_path))
    assert cfg.min_age_days == 5
    assert cfg.tiers_enabled == [0, 1]
    assert cfg.max_merges_per_run == 7
    assert cfg.repo_permitted("bouncer") is True
    assert cfg.repo_permitted("workflows") is False
    assert cfg.health.health_window_min == 20
    assert cfg.health.thresholds.min_crash_free_pct == 98.5


def test_env_overrides_win(tmp_path, monkeypatch):
    monkeypatch.setenv("DBR_MIN_AGE_DAYS", "1")
    monkeypatch.setenv("DBR_TIERS_ENABLED", "0")
    monkeypatch.setenv("DBR_MAX_MERGES", "3")
    cfg = load_config(_write(tmp_path))
    assert cfg.min_age_days == 1
    assert cfg.tiers_enabled == [0]
    assert cfg.max_merges_per_run == 3


def test_dry_run_is_fail_safe(tmp_path, monkeypatch):
    # Garbage value must NOT disable dry-run.
    monkeypatch.setenv("DBR_DRY_RUN", "maybe")
    assert load_config(_write(tmp_path)).dry_run is True
    # Only explicit falses disable it.
    monkeypatch.setenv("DBR_DRY_RUN", "false")
    assert load_config(_write(tmp_path)).dry_run is False


def test_health_defaults_for_unmapped_repo(tmp_path):
    cfg = load_config(_write(tmp_path))
    assert cfg.health.sentry_project_for("bouncer") == "bouncer"
    assert cfg.health.newrelic_app_for("bouncer") == "bouncer (prod)"


def test_missing_file_uses_defaults():
    cfg = load_config("does-not-exist.yml")
    assert cfg.dry_run is True
    assert cfg.min_age_days == 3
    assert cfg.tiers_enabled == [0]
