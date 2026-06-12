"""
Deterministic auto-merge tier classification.

Replaces the per-repo heuristics in ``review.analyze_risk`` for the autonomous
layer with a single, testable function. ``classify`` answers *"how risky is this
bump"* independent of CI/merge state (those are handled by eligibility and
escalation routing in ``automerge``), so a dry-run report can show the tier of a
PR even when its CI is failing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .automation_types import Tier
from .deploy_model import DeployModel
from .review import DependencyUpdatePR
from .semver import BumpKind, classify_bump, riskiest_bump

# Tooling / dev-only dependencies — assumed not to ship to production and never a
# Tier-2 escalation on their own (a *major* bump still escalates, see classify).
DEV_TOOLS = frozenset(
    {
        "ruff",
        "mypy",
        "black",
        "pytest",
        "pytest-cov",
        "pytest-factoryboy",
        "pylint",
        "isort",
        "flake8",
        "tox",
        "pre-commit",
        "pyflakes",
        "pycodestyle",
        "coverage",
        "ipython",
        "factory-boy",
        "faker",
        "sphinx",
        "sphinx-autobuild",
        "supervisor",
        "cookiecutter",
    }
)
LOCKFILE_TOOLING = frozenset({"pip", "pip-tools", "wheel", "setuptools", "poetry"})

# Security-sensitive runtime libraries. Any bump escalates to human review.
SECURITY_ANY_BUMP = frozenset(
    {
        "cryptography",
        "gunicorn",
        "pyjwt",
        "certifi",
        "urllib3",
        "pyopenssl",
        "joserfc",
        "oauthlib",
        "pyasn1",
    }
)
# Security-sensitive runtime libraries where only a *major* bump escalates.
SECURITY_MAJOR_ONLY = frozenset(
    {
        "requests",
        "sqlalchemy",
        "marshmallow",
        "zope-sqlalchemy",
    }
)


@dataclass(frozen=True)
class Classification:
    tier: Tier
    deploys_to_prod: bool
    bump: BumpKind
    reasons: list[str] = field(default_factory=list)


def normalize_name(name: str) -> str:
    """PEP 503-ish normalization for matching against the package sets."""
    normalized = name.strip().lower()
    if "[" in normalized:  # drop extras, e.g. "coverage[toml]"
        normalized = normalized.split("[", 1)[0]
    return normalized.replace("_", "-").replace(".", "-")


def is_dev_tool(name: str) -> bool:
    normalized = normalize_name(name)
    return (
        normalized in DEV_TOOLS
        or normalized in LOCKFILE_TOOLING
        or normalized.startswith("types-")
    )


def classify(pr: DependencyUpdatePR, model: DeployModel) -> Classification:
    """
    Classify a Dependabot PR into an auto-merge tier.

    Tier 2 (human) if: any major bump, an unparseable version (fail-safe), a
    security-sensitive runtime lib (any bump), or one of the major-only sensitive
    libs bumped at major. Otherwise Tier 1 if it deploys to production, else
    Tier 0.
    """
    bump = riskiest_bump(pr.updates)
    names = [normalize_name(u.name) for u in pr.updates]
    all_dev_tools = bool(pr.updates) and all(is_dev_tool(u.name) for u in pr.updates)
    deploys = model.deploys_to_prod(pr, is_dev_tool=all_dev_tools)

    escalations: list[str] = []
    if bump == BumpKind.MAJOR:
        escalations.append("major version bump")
    if bump == BumpKind.UNKNOWN:
        escalations.append("could not parse version (fail-safe)")
    if any(name in SECURITY_ANY_BUMP for name in names):
        escalations.append("security-sensitive runtime library")
    for update in pr.updates:
        if (
            normalize_name(update.name) in SECURITY_MAJOR_ONLY
            and classify_bump(update.from_version, update.to_version) == BumpKind.MAJOR
        ):
            escalations.append(f"{update.name} major bump (security-sensitive)")

    if escalations:
        return Classification(
            tier=Tier.TIER_2,
            deploys_to_prod=deploys,
            bump=bump,
            reasons=escalations,
        )

    if not deploys:
        reasons = ["does not deploy to production"]
        if all_dev_tools:
            reasons.append("dev/tooling dependency")
        return Classification(
            tier=Tier.TIER_0, deploys_to_prod=False, bump=bump, reasons=reasons
        )

    return Classification(
        tier=Tier.TIER_1,
        deploys_to_prod=True,
        bump=bump,
        reasons=["patch/minor production dependency (health-gated)"],
    )
