"""
Curses TUI "kickoff + watch" monitor for a Dependabot sweep.

Shows a live table of PRs (repo / package / tier / CI / state / health) plus a log
pane, driven by the same engine as ``automerge`` via a worker thread that feeds a
queue of events to the main (drawing) thread. Defaults to dry-run; ``--execute``
runs the real sweep (merge, then health-gate + rollback for Tier 1).

The non-curses parts — building rows and reducing events — are pure functions so
the state machine is testable without a TTY.
"""

from __future__ import annotations

import curses
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable

from .audit import AuditLog, default_audit_path
from .automation_types import MergeOutcome
from .automerge import (
    Decision,
    PreMergeCheckError,
    _merge_and_capture,
    gather_decisions,
    monitoring_configured,
)
from .config import Config, load_config
from .github_client import GitHubClient


class PRState(Enum):
    PENDING = "PENDING"
    MERGING = "MERGING"
    MERGED = "MERGED"
    DEPLOYING = "DEPLOYING"
    CHECKING = "CHECKING_HEALTH"
    HEALTHY = "HEALTHY"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    SKIPPED = "SKIPPED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"
    WOULD_MERGE = "WOULD_MERGE"


@dataclass
class RowState:
    repo: str
    package: str
    tier: int
    ci: str
    state: PRState
    health: str = ""
    action: str = ""
    url: str = ""


@dataclass
class MonitorEvent:
    kind: str  # "row" | "log" | "done"
    index: int = -1
    state: PRState | None = None
    health: str | None = None
    action: str | None = None
    message: str = ""


@dataclass
class MonitorModel:
    rows: list[RowState]
    logs: list[str] = field(default_factory=list)
    done: bool = False


def build_rows(decisions: list[Decision]) -> list[RowState]:
    """Map engine decisions to initial TUI rows."""
    initial = {
        "merge": PRState.PENDING,
        "merge+health": PRState.PENDING,
        "escalate": PRState.ESCALATED,
        "skip": PRState.SKIPPED,
    }
    rows: list[RowState] = []
    for d in decisions:
        rows.append(
            RowState(
                repo=d.pr.repo or "?",
                package=d.pr.group_name,
                tier=int(d.classification.tier),
                ci=d.pr.check_status.description,
                state=initial.get(d.action, PRState.PENDING),
                action=d.action,
                health="; ".join(d.classification.reasons)[:40],
                url=d.pr.url,
            )
        )
    return rows


def apply_event(model: MonitorModel, event: MonitorEvent) -> MonitorModel:
    """Pure reducer: apply one event to the model (used by the draw loop + tests)."""
    if event.kind == "log":
        model.logs.append(event.message)
    elif event.kind == "done":
        model.done = True
    elif event.kind == "row" and 0 <= event.index < len(model.rows):
        row = model.rows[event.index]
        if event.state is not None:
            row.state = event.state
        if event.health is not None:
            row.health = event.health
        if event.action is not None:
            row.action = event.action
    return model


# --------------------------------------------------------------------- worker

Emit = Callable[[MonitorEvent], None]


def dry_run_worker(
    decisions: list[Decision],
) -> Callable[[Emit, threading.Event], None]:
    """A worker that just animates the classified plan to its terminal state."""

    def work(emit: Emit, abort: threading.Event) -> None:
        final = {
            "merge": (PRState.WOULD_MERGE, "would merge (T0)"),
            "merge+health": (PRState.WOULD_MERGE, "would merge + health-gate (T1)"),
            "escalate": (PRState.ESCALATED, "needs human"),
            "skip": (PRState.SKIPPED, ""),
        }
        for index, d in enumerate(decisions):
            if abort.is_set():
                emit(MonitorEvent("log", message="aborted"))
                break
            state, note = final.get(d.action, (PRState.SKIPPED, ""))
            emit(MonitorEvent("row", index=index, state=state))
            if note:
                emit(
                    MonitorEvent(
                        "log", message=f"{d.pr.repo}/{d.pr.group_name}: {note}"
                    )
                )
        emit(MonitorEvent("done"))

    return work


def live_worker(
    gh: GitHubClient,
    cfg: Config,
    decisions: list[Decision],
    audit: AuditLog | None = None,
) -> Callable[[Emit, threading.Event], None]:
    """
    The real sweep: merge eligible PRs, then health-gate Tier-1 merges and roll
    back degraded deploys — emitting row/log events as each PR advances.
    """

    def work(emit: Emit, abort: threading.Event) -> None:
        from .health import check_health
        from .rollback import revert_merge

        def record(event: str, d: Decision, **fields: object) -> None:
            if audit is not None:
                audit.record(
                    event,
                    repo=d.pr.repo,
                    package=d.pr.group_name,
                    tier=int(d.classification.tier),
                    url=d.pr.url,
                    **fields,
                )

        def log(message: str) -> None:
            emit(MonitorEvent("log", message=message))

        def row(index: int, state: PRState, health: str | None = None) -> None:
            emit(MonitorEvent("row", index=index, state=state, health=health))

        now = datetime.now(timezone.utc)
        can_watch = monitoring_configured(cfg)
        watch: list[tuple[int, MergeOutcome]] = []
        merged_count = 0
        for index, d in enumerate(decisions):
            if abort.is_set():
                log("aborted")
                break
            if d.action not in ("merge", "merge+health"):
                continue
            if merged_count >= cfg.max_merges_per_run:
                row(index, PRState.SKIPPED, "max_merges_per_run cap")
                record("skipped", d, reason="max_merges_per_run cap")
                continue
            if d.action == "merge+health" and not can_watch:
                row(index, PRState.SKIPPED, "no monitoring credentials")
                log(f"{d.pr.repo}/{d.pr.group_name}: held — health gate has no signals")
                record("skipped", d, reason="no monitoring credentials")
                continue
            row(index, PRState.MERGING)
            try:
                outcome = _merge_and_capture(gh, d, cfg, now)
                merged_count += 1
            except PreMergeCheckError as exc:
                row(index, PRState.ESCALATED, str(exc)[:40])
                log(f"{d.pr.repo}/{d.pr.group_name}: held — {exc}")
                record("held", d, reason=str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - show and continue the sweep
                row(index, PRState.FAILED, str(exc)[:40])
                log(f"{d.pr.repo}/{d.pr.group_name}: merge failed: {exc!r}")
                record("merge_failed", d, reason=repr(exc))
                continue
            row(index, PRState.MERGED)
            log(f"{d.pr.repo}/{d.pr.group_name}: merged")
            record("merged", d, merge_commit_sha=outcome.merge_commit_sha)
            if d.action == "merge+health":
                watch.append((index, outcome))

        for index, outcome in watch:
            if abort.is_set():
                log("aborted before health gate completed")
                break
            row(index, PRState.CHECKING)
            verdict = check_health(gh, outcome, cfg.health)
            record(
                "health",
                decisions[index],
                healthy=verdict.healthy,
                unknown=verdict.unknown,
                reasons=verdict.reasons,
            )
            if verdict.healthy:
                row(index, PRState.HEALTHY)
                continue
            if verdict.unknown:
                row(index, PRState.ESCALATED, "health unverified")
                log(f"{outcome.repo}: health not verifiable — check manually")
                continue
            row(index, PRState.ROLLING_BACK, "; ".join(verdict.reasons)[:40])
            if not outcome.merge_commit_sha:
                row(index, PRState.FAILED, "no merge SHA; manual rollback")
                record(
                    "rollback", decisions[index], performed=False, reason="no merge SHA"
                )
                continue
            rollback = revert_merge(
                gh,
                outcome.owner,
                outcome.repo,
                outcome.merge_commit_sha,
                original_title=outcome.pr.group_name,
                original_pr_url=outcome.pr.url,
                dry_run=cfg.dry_run,
                merge_method=outcome.pr.merge_method,
            )
            record(
                "rollback",
                decisions[index],
                performed=rollback.performed,
                revert_pr_url=rollback.revert_pr_url,
                reason=rollback.reason,
            )
            if rollback.performed:
                row(index, PRState.ROLLED_BACK, (rollback.revert_pr_url or "")[:40])
                log(f"{outcome.repo}: rolled back -> {rollback.revert_pr_url}")
            else:
                row(index, PRState.FAILED, (rollback.reason or "rollback failed")[:40])
                log(f"{outcome.repo}: rollback NOT performed: {rollback.reason}")
        emit(MonitorEvent("done"))

    return work


# ----------------------------------------------------------------------- curses


_TIER_COLOR = {0: 2, 1: 3, 2: 1}  # green / yellow / red (init below)


def _init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_RED, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_CYAN, -1)


def _draw(stdscr: Any, model: MonitorModel, scroll: int, dry_run: bool) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    mode = "DRY-RUN" if dry_run else "LIVE"
    clock = datetime.now(timezone.utc).strftime("%H:%M:%S")
    header = f" Dependabot sweep  [{mode}]  {len(model.rows)} PRs  {clock} "
    stdscr.addnstr(0, 0, header.ljust(width), width, curses.A_REVERSE)

    cols = f" {'REPO':18}{'PACKAGE':26}{'TIER':5}{'CI':9}{'STATE':16}{'NOTE'}"
    stdscr.addnstr(1, 0, cols.ljust(width), width, curses.A_BOLD)

    table_height = max(3, height - 7)
    visible = model.rows[scroll : scroll + table_height]
    for offset, row in enumerate(visible):
        color = curses.color_pair(_TIER_COLOR.get(row.tier, 0))
        line = (
            f" {row.repo[:17]:18}{row.package[:25]:26}T{row.tier:<4}"
            f"{row.ci[:8]:9}{row.state.value:16}{row.health[:30]}"
        )
        stdscr.addnstr(2 + offset, 0, line.ljust(width), width, color)

    log_top = 2 + table_height
    stdscr.addnstr(log_top, 0, " log ".ljust(width, "─"), width, curses.A_DIM)
    for offset, message in enumerate(model.logs[-(height - log_top - 2) :]):
        stdscr.addnstr(log_top + 1 + offset, 0, f" {message}", width)

    status = " [q]uit  [A]bort  [j/k] scroll "
    if model.done:
        status += " — done"
    stdscr.addnstr(height - 1, 0, status.ljust(width), width, curses.A_REVERSE)
    stdscr.refresh()


def run_monitor(
    stdscr: Any,
    model: MonitorModel,
    worker: Callable[[Emit, threading.Event], None],
    dry_run: bool,
) -> None:
    """Main draw loop: drains worker events, handles keys, redraws."""
    _init_colors()
    curses.curs_set(0)
    stdscr.nodelay(True)

    events: queue.Queue[MonitorEvent] = queue.Queue()
    abort = threading.Event()
    thread = threading.Thread(target=worker, args=(events.put, abort), daemon=True)
    thread.start()

    scroll = 0
    while True:
        while True:
            try:
                apply_event(model, events.get_nowait())
            except queue.Empty:
                break
        _draw(stdscr, model, scroll, dry_run)

        key = stdscr.getch()
        if key in (ord("q"), 27):
            abort.set()
            break
        if key == ord("A"):
            abort.set()
        elif key in (ord("j"), curses.KEY_DOWN):
            scroll = min(scroll + 1, max(0, len(model.rows) - 1))
        elif key in (ord("k"), curses.KEY_UP):
            scroll = max(0, scroll - 1)
        if model.done and not thread.is_alive() and events.empty():
            # Keep the final view until the user quits.
            pass
        time.sleep(0.05)


def main() -> int:
    from argparse import ArgumentParser

    parser = ArgumentParser(description="Curses monitor for a Dependabot sweep")
    parser.add_argument("organization", nargs="?", default=None)
    parser.add_argument("--config", default="automation.yml")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the real sweep: merge, health-gate Tier 1, roll back on failure "
        "(default: dry-run animation of the plan).",
    )
    parser.add_argument(
        "--audit-log",
        default=None,
        help="Path for the JSONL audit trail (default: sweep-audit-<timestamp>.jsonl)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.organization:
        cfg.organization = args.organization
    # The TUI goes live only with the explicit flag — config/env alone can't.
    cfg.dry_run = not args.execute

    from pathlib import Path

    audit = AuditLog(Path(args.audit_log) if args.audit_log else default_audit_path())

    gh = GitHubClient.init()
    decisions = gather_decisions(gh, cfg, datetime.now(timezone.utc))
    audit.record(
        "sweep_start",
        organization=cfg.organization,
        dry_run=cfg.dry_run,
        prs=len(decisions),
    )
    for d in decisions:
        audit.record(
            "plan",
            repo=d.pr.repo,
            package=d.pr.group_name,
            tier=int(d.classification.tier),
            action=d.action,
            ci=d.pr.check_status.description,
            reasons=d.classification.reasons,
            skip_reason=d.skip_reason,
            url=d.pr.url,
        )

    model = MonitorModel(rows=build_rows(decisions))
    if cfg.dry_run:
        worker = dry_run_worker(decisions)
    else:
        worker = live_worker(gh, cfg, decisions, audit=audit)
    curses.wrapper(
        lambda stdscr: run_monitor(stdscr, model, worker, dry_run=cfg.dry_run)
    )
    audit.record("sweep_end", dry_run=cfg.dry_run)
    print(f"Audit log: {audit.path}")
    return 0


def collect_events(
    worker: Callable[[Emit, threading.Event], None],
) -> list[MonitorEvent]:
    """Run a worker synchronously and collect its events (test helper)."""
    collected: list[MonitorEvent] = []
    worker(collected.append, threading.Event())
    return collected


def reduce_events(rows: list[RowState], events: Iterable[MonitorEvent]) -> MonitorModel:
    """Apply a sequence of events to fresh rows (test helper)."""
    model = MonitorModel(rows=rows)
    for event in events:
        apply_event(model, event)
    return model


if __name__ == "__main__":
    import sys

    sys.exit(main())
