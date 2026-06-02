"""
One-shot backlog burn-down CLI.

Sweeps the *entire* open Dependabot backlog (not just the last N days) in waves —
Tier 0, then Tier 1, then a Tier 2 report — reusing the same classification and
eligibility engine as the daily auto-merger. Defaults to dry-run.

Usage:
    python -m dependabot_batch_review.bulk hypothesis --dry-run
    python -m dependabot_batch_review.bulk hypothesis --execute --tier 0 --max-per-wave 20
"""

from __future__ import annotations

import sys
import time
from argparse import ArgumentParser
from datetime import datetime, timezone

from .automerge import Decision, _merge_and_capture, decide
from .config import Config, load_config
from .deploy_model import DeployModelCache
from .github_client import GitHubClient
from .review import fetch_dependency_prs


def _select(decisions: list[Decision], tier: int) -> list[Decision]:
    return [
        d
        for d in decisions
        if int(d.classification.tier) == tier and d.action in ("merge", "merge+health")
    ]


def run_bulk(
    gh: GitHubClient,
    cfg: Config,
    *,
    tiers: list[int],
    max_per_wave: int,
    repos: list[str] | None,
    wave_pause_s: float,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(timezone.utc)
    prs = fetch_dependency_prs(gh, organization=cfg.organization, labels=cfg.labels)
    if repos:
        prs = [p for p in prs if p.repo in set(repos)]
    cache = DeployModelCache(gh, cfg.organization)
    decisions = [decide(pr, cache, cfg, now) for pr in prs]

    mode = "DRY-RUN" if cfg.dry_run else "LIVE"
    print(f"[{mode}] {len(prs)} open PRs in {cfg.organization}; waves={tiers}\n")

    total_merged = 0
    for tier in tiers:
        wave = _select(decisions, tier)
        label = {0: "Tier 0 (no deploy)", 1: "Tier 1 (health-gated)"}.get(
            tier, f"Tier {tier}"
        )
        print(f"== Wave {tier}: {label} — {len(wave)} eligible ==")
        for index, d in enumerate(wave, start=1):
            if index > max_per_wave:
                print(f"  … {len(wave) - max_per_wave} more held (--max-per-wave)")
                break
            bump = d.classification.bump.name.lower()
            head = f"  [{index}/{min(len(wave), max_per_wave)}] {d.pr.repo}: {d.pr.group_name} ({bump})"
            if cfg.dry_run:
                print(f"{head} -> would merge")
                continue
            try:
                _merge_and_capture(gh, d, cfg)
                total_merged += 1
                print(f"{head} -> merged ✓")
            except Exception as exc:  # noqa: BLE001
                print(f"{head} -> FAILED: {exc!r}")
        if wave_pause_s and not cfg.dry_run and tier != tiers[-1]:
            print(f"  pausing {wave_pause_s:.0f}s for Dependabot rebases…")
            time.sleep(wave_pause_s)
        print()

    escalations = [d for d in decisions if d.action == "escalate"]
    print(f"== Needs human review: {len(escalations)} ==")
    for d in sorted(escalations, key=lambda x: (x.pr.repo or "", x.pr.group_name)):
        why = "; ".join(d.classification.reasons) or d.skip_reason or "review"
        print(f"  {d.pr.repo}: {d.pr.group_name} ({why})")

    if not cfg.dry_run:
        print(f"\nMerged {total_merged} PR(s).")
    return 0


def main() -> int:
    parser = ArgumentParser(description="Bulk Dependabot backlog burn-down")
    parser.add_argument("organization", nargs="?", default=None)
    parser.add_argument("--config", default="automation.yml")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    parser.add_argument("--execute", dest="dry_run", action="store_false")
    parser.add_argument("--max-per-wave", type=int, default=20)
    parser.add_argument("--repo", action="append", help="restrict to repo(s)")
    parser.add_argument(
        "--tier", type=int, action="append", help="tier(s) to merge (default: 0)"
    )
    parser.add_argument("--ignore-age", action="store_true")
    parser.add_argument("--wave-pause", type=float, default=0.0)
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.organization:
        cfg.organization = args.organization
    cfg.dry_run = args.dry_run
    if args.ignore_age:
        cfg.min_age_days = 0
    tiers = sorted(set(args.tier)) if args.tier else [0]
    # Enable the requested tiers so eligibility doesn't filter them out.
    cfg.tiers_enabled = sorted(set(cfg.tiers_enabled) | set(tiers))

    gh = GitHubClient.init()
    return run_bulk(
        gh,
        cfg,
        tiers=tiers,
        max_per_wave=args.max_per_wave,
        repos=args.repo,
        wave_pause_s=args.wave_pause,
    )


if __name__ == "__main__":
    sys.exit(main())
