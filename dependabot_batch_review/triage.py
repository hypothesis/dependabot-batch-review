"""
Claude-powered risk triage for escalated / rolled-back PRs.

Produces a terse risk summary + recommended action for Slack. Uses the Anthropic
Messages API for a single-shot summarization. Degrades gracefully to the
deterministic ``review.analyze_risk`` when ``anthropic`` is not installed or no
``ANTHROPIC_API_KEY`` is configured, so callers always get a usable result.

To enable live triage: ``poetry add anthropic`` and set ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .review import DependencyUpdatePR, analyze_risk

_DEFAULT_MODEL = "claude-opus-4-8"

_SYSTEM_PROMPT = (
    "You are a release-safety reviewer for Python/npm dependency bumps that "
    "auto-deploy to production. Be terse and concrete. Respond with strict JSON: "
    '{"summary": str, "recommendation": "merge"|"hold"|"manual-review", '
    '"confidence": "high"|"medium"|"low"}. Flag major bumps, breaking changes, and '
    "security fixes."
)

_REC_FROM_LEVEL = {"High": "manual-review", "Medium": "hold", "Low": "merge"}


@dataclass
class TriageResult:
    summary: str
    recommendation: str  # "merge" | "hold" | "manual-review"
    confidence: str  # "high" | "medium" | "low"
    degraded: bool = False


def _degraded(pr: DependencyUpdatePR) -> TriageResult:
    """Fallback summary derived from the deterministic risk heuristic."""
    risk = analyze_risk(pr)
    return TriageResult(
        summary="; ".join(risk.reasons) or "no specific risk factors identified",
        recommendation=_REC_FROM_LEVEL.get(risk.level, "manual-review"),
        confidence="low",
        degraded=True,
    )


def _build_user_prompt(pr: DependencyUpdatePR, package_diff: str | None) -> str:
    lines = [f"Package group: {pr.group_name}", f"Ecosystem: {pr.package_type}"]
    for update in pr.updates:
        lines.append(f"- {update.name}: {update.from_version} -> {update.to_version}")
    if pr.advisory_summary:
        lines.append(f"Security advisory: {pr.advisory_summary} ({pr.ghsa_id})")
    notes = "\n".join(u.notes for u in pr.updates if u.notes)[:6000]
    if notes:
        lines.append(f"\nChangelog / release notes:\n{notes}")
    if package_diff:
        lines.append(f"\nPackage diff (truncated):\n{package_diff[:8000]}")
    return "\n".join(lines)


def _extract_json(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if start != -1 and end != -1 else text


def triage_pr(
    pr: DependencyUpdatePR,
    package_diff: str | None = None,
    model: str = _DEFAULT_MODEL,
    api_key: str | None = None,
) -> TriageResult:
    """Summarize a PR's risk, falling back to the deterministic heuristic."""
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return _degraded(pr)
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:
        return _degraded(pr)

    try:
        client = anthropic.Anthropic(api_key=key)
        response = client.messages.create(
            model=model,
            max_tokens=400,
            system=_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": _build_user_prompt(pr, package_diff)}
            ],
        )
        text = "".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        )
        data = json.loads(_extract_json(text))
        return TriageResult(
            summary=str(data.get("summary", "")).strip() or "(no summary)",
            recommendation=str(data.get("recommendation", "manual-review")),
            confidence=str(data.get("confidence", "low")),
            degraded=False,
        )
    except Exception:  # noqa: BLE001 - any SDK/parse failure -> deterministic fallback
        return _degraded(pr)
