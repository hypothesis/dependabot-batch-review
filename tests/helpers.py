"""Shared test helpers: builders for PRs and deploy models."""

from __future__ import annotations

from dependabot_batch_review.deploy_model import DeployModel
from dependabot_batch_review.review import (
    CheckStatus,
    DependencyUpdate,
    DependencyUpdatePR,
)

# A deploying service whose deploy.yml ignores requirements/* except prod.txt.
SERVICE_PATHS_IGNORE = ["requirements/*", "!requirements/prod.txt", "docs/*", "*.md"]


def service_model(repo: str = "bouncer") -> DeployModel:
    return DeployModel(
        repo=repo,
        is_deploying_service=True,
        deploy_branches=["main"],
        paths_ignore=list(SERVICE_PATHS_IGNORE),
        raw_present=True,
    )


def library_model(repo: str = "annotation-ui") -> DeployModel:
    return DeployModel(repo=repo, is_deploying_service=False, raw_present=True)


def make_pr(
    group: str = "pkg",
    from_version: str = "1.0.0",
    to_version: str = "1.0.1",
    *,
    updates: list[DependencyUpdate] | None = None,
    package_type: str = "pip",
    check: CheckStatus = CheckStatus.SUCCESS,
    created_at: str = "2026-05-01T00:00:00Z",
    head_ref: str | None = None,
    merge_state: str = "CLEAN",
    mergeable: str = "MERGEABLE",
    repo: str = "bouncer",
    number: int = 1,
    merge_method: str = "SQUASH",
    url: str | None = None,
) -> DependencyUpdatePR:
    if updates is None:
        updates = [DependencyUpdate(group, from_version, to_version, "")]
    return DependencyUpdatePR(
        id=f"PR_{repo}_{number}",
        package_type=package_type,
        is_group=len(updates) > 1,
        group_name=group,
        updates=updates,
        url=url or f"https://github.com/hypothesis/{repo}/pull/{number}",
        approved=False,
        check_status=check,
        merge_method=merge_method,
        created_at=created_at,
        merge_state_status=merge_state,
        mergeable=mergeable,
        head_ref_name=head_ref or f"dependabot/{package_type}/{group}-{to_version}",
        number=number,
        repo=repo,
    )
