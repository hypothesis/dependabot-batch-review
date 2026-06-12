"""
Semantic-version bump classification for dependency updates.

Tolerant of the messy version strings Dependabot produces: ``v1.2.3``,
``25.2-alpine``, ``2024.10.3``, ``0.1.32``, ``4.17.21``. Used by the risk engine
to decide whether a bump is patch / minor / major, with a pre-1.0 rule that
treats a ``0.y`` change as breaking (major).
"""

from __future__ import annotations

import re
from enum import IntEnum

from .review import DependencyUpdate

# Leading dotted-numeric prefix, ignoring any 'v' prefix and trailing suffixes
# like '-alpine', 'rc1' or '.x'.
_VERSION_RE = re.compile(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


class BumpKind(IntEnum):
    """Ordered so that ``max(...)`` yields the riskiest bump in a group."""

    UNKNOWN = 0
    PATCH = 1
    MINOR = 2
    MAJOR = 3


def parse_version(version: str | None) -> tuple[int, int, int] | None:
    """
    Extract a ``(major, minor, patch)`` tuple from a version string.

    Returns ``None`` if no leading numeric component can be found. Missing minor
    or patch components default to ``0`` (e.g. ``"26"`` -> ``(26, 0, 0)``,
    ``"25.2-alpine"`` -> ``(25, 2, 0)``).
    """
    if not version:
        return None
    match = _VERSION_RE.match(version.strip())
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2)) if match.group(2) is not None else 0
    patch = int(match.group(3)) if match.group(3) is not None else 0
    return (major, minor, patch)


def classify_bump(from_version: str | None, to_version: str | None) -> BumpKind:
    """
    Classify a single version transition.

    A change in the major component is ``MAJOR``. For pre-1.0 versions
    (``major == 0``) a change in the minor component is also treated as ``MAJOR``,
    since ``0.y`` releases routinely ship breaking changes. Otherwise a
    minor-component change is ``MINOR`` and a patch-only change is ``PATCH``.
    Unparseable versions yield ``UNKNOWN`` (the risk engine escalates these to
    human review as a fail-safe).
    """
    parsed_from = parse_version(from_version)
    parsed_to = parse_version(to_version)
    if parsed_from is None or parsed_to is None:
        return BumpKind.UNKNOWN

    from_major, from_minor, _ = parsed_from
    to_major, to_minor, _ = parsed_to

    if from_major != to_major:
        return BumpKind.MAJOR
    if from_major == 0:
        # Pre-1.0: a minor change is potentially breaking.
        return BumpKind.MAJOR if from_minor != to_minor else BumpKind.PATCH
    if from_minor != to_minor:
        return BumpKind.MINOR
    return BumpKind.PATCH


def riskiest_bump(updates: list[DependencyUpdate]) -> BumpKind:
    """
    Return the riskiest bump across a (possibly grouped) PR's updates.

    A grouped PR is only as safe as its riskiest single update.
    """
    if not updates:
        return BumpKind.UNKNOWN
    bumps = [classify_bump(u.from_version, u.to_version) for u in updates]
    if BumpKind.UNKNOWN in bumps:
        # UNKNOWN sorts lowest, so max() alone would let a parseable sibling mask
        # an unparseable update; one unparseable member taints the whole group.
        return BumpKind.UNKNOWN
    return max(bumps)
