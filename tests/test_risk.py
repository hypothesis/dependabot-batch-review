import pytest

from dependabot_batch_review.automation_types import Tier
from dependabot_batch_review.review import DependencyUpdate
from dependabot_batch_review.risk import classify
from tests.helpers import library_model, make_pr, service_model

SVC = service_model()
LIB = library_model()


@pytest.mark.parametrize(
    "pr,model,expected_tier,expected_deploys",
    [
        (make_pr("newrelic", "11.0.1", "13.1.0"), SVC, Tier.TIER_2, True),
        (make_pr("pytest", "9.0.1", "9.0.3"), SVC, Tier.TIER_0, False),
        (make_pr("urllib3", "2.5.0", "2.7.0"), SVC, Tier.TIER_2, True),
        (make_pr("sentry-sdk", "2.58.0", "2.61.1"), SVC, Tier.TIER_1, True),
        (make_pr("black", "25.11.0", "26.5.1"), SVC, Tier.TIER_2, False),
        (make_pr("gunicorn", "23.0.0", "26.0.0"), SVC, Tier.TIER_2, True),
        (make_pr("requests", "2.32.0", "2.33.0"), SVC, Tier.TIER_1, True),
        (make_pr("requests", "2.0.0", "3.0.0"), SVC, Tier.TIER_2, True),
        (make_pr("cryptography", "44.0.2", "44.0.3"), SVC, Tier.TIER_2, True),
        (make_pr("ruff", "0.14.7", "0.14.10"), SVC, Tier.TIER_0, False),
        (
            make_pr("node", "25.2-alpine", "26.2-alpine", package_type="docker"),
            SVC,
            Tier.TIER_2,
            True,
        ),
        (
            make_pr("typescript", "5.9.3", "6.0.3", package_type="npm_and_yarn"),
            LIB,
            Tier.TIER_2,
            False,
        ),
        (
            make_pr("preact", "10.27.2", "10.28.4", package_type="npm_and_yarn"),
            LIB,
            Tier.TIER_0,
            False,
        ),
    ],
)
def test_classify_table(pr, model, expected_tier, expected_deploys):
    result = classify(pr, model)
    assert result.tier is expected_tier
    assert result.deploys_to_prod is expected_deploys


def test_grouped_pr_uses_riskiest():
    # ruff (patch, dev) + sqlalchemy (minor, runtime) -> Tier 1 on a service.
    grouped = make_pr(
        "backend",
        updates=[
            DependencyUpdate("ruff", "0.14.7", "0.14.10", ""),
            DependencyUpdate("sqlalchemy", "2.0.44", "2.1.0", ""),
        ],
    )
    assert classify(grouped, SVC).tier is Tier.TIER_1
    # Same group on a non-deploying library is Tier 0.
    assert classify(grouped, LIB).tier is Tier.TIER_0


def test_unparseable_is_fail_safe_tier2():
    pr = make_pr("weird", "garbage", "alsobad")
    assert classify(pr, SVC).tier is Tier.TIER_2
