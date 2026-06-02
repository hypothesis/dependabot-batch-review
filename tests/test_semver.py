import pytest

from dependabot_batch_review.review import DependencyUpdate
from dependabot_batch_review.semver import (
    BumpKind,
    classify_bump,
    parse_version,
    riskiest_bump,
)


@pytest.mark.parametrize(
    "from_version,to_version,expected",
    [
        ("9.0.1", "9.0.3", BumpKind.PATCH),
        ("2.5.0", "2.7.0", BumpKind.MINOR),
        ("11.0.1", "13.1.0", BumpKind.MAJOR),
        ("25.11.0", "26.5.1", BumpKind.MAJOR),
        ("0.1.32", "0.1.33", BumpKind.PATCH),
        ("0.1.0", "0.2.0", BumpKind.MAJOR),  # pre-1.0 minor is breaking
        ("v0.47.13", "v0.59.x", BumpKind.MAJOR),
        ("25.2-alpine", "26.2-alpine", BumpKind.MAJOR),
        ("1.2.3", "1.2.3", BumpKind.PATCH),
        (None, "1.2.3", BumpKind.UNKNOWN),
        ("1.2.3", None, BumpKind.UNKNOWN),
        ("garbage", "1.0.0", BumpKind.UNKNOWN),
    ],
)
def test_classify_bump(from_version, to_version, expected):
    assert classify_bump(from_version, to_version) is expected


def test_parse_version():
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("25.2-alpine") == (25, 2, 0)
    assert parse_version("26") == (26, 0, 0)
    assert parse_version("2024.10.3") == (2024, 10, 3)
    assert parse_version("garbage") is None
    assert parse_version(None) is None
    assert parse_version("") is None


def test_riskiest_bump_takes_max():
    updates = [
        DependencyUpdate("a", "1.0.0", "1.0.1", ""),  # patch
        DependencyUpdate("b", "1.0.0", "2.0.0", ""),  # major
    ]
    assert riskiest_bump(updates) is BumpKind.MAJOR


def test_riskiest_bump_empty():
    assert riskiest_bump([]) is BumpKind.UNKNOWN
