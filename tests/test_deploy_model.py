import pytest

from dependabot_batch_review.deploy_model import parse_deploy_yaml
from tests.helpers import make_pr

DEPLOY_YAML = """
name: Deploy
on:
  push:
    branches:
      - main
    paths-ignore:
      - 'requirements/*'
      - '!requirements/prod.txt'
      - 'docs/*'
      - '*.md'
      - 'tests/*'
jobs:
  production:
    uses: hypothesis/workflows/.github/workflows/deploy.yml@main
"""


@pytest.fixture
def model():
    return parse_deploy_yaml("lms", DEPLOY_YAML)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("requirements/dev.txt", False),  # ignored
        ("requirements/prod.txt", True),  # ignored then re-included by negation
        ("Dockerfile", True),  # not ignored
        ("package.json", True),  # not ignored
        ("tests/test_foo.py", False),  # ignored
        ("README.md", False),  # *.md ignored
    ],
)
def test_path_triggers_deploy(model, path, expected):
    assert model.path_triggers_deploy(path) is expected


def test_yaml_on_key_parsed_despite_boolean_quirk(model):
    # YAML 1.1 parses bare `on:` as True; parser must still find the push trigger.
    assert model.is_deploying_service is True
    assert "main" in model.deploy_branches


def test_no_deploy_file_means_non_deploying():
    model = parse_deploy_yaml("annotation-ui", None)
    assert model.is_deploying_service is False
    assert model.path_triggers_deploy("requirements/prod.txt") is False


def test_unparseable_deploy_yaml_fails_safe():
    model = parse_deploy_yaml("x", "::: not yaml :::\n  - [")
    assert model.is_deploying_service is True  # conservative


def test_infer_changed_paths(model):
    runtime = make_pr("urllib3", "2.5.0", "2.7.0", package_type="pip")
    assert model.infer_changed_paths(runtime, is_dev_tool=False) == [
        "requirements/prod.txt"
    ]
    dev = make_pr("ruff", "0.1.0", "0.1.1", package_type="pip")
    assert model.infer_changed_paths(dev, is_dev_tool=True) == ["requirements/dev.txt"]
    docker = make_pr("node", "25", "25.1", package_type="docker")
    assert model.infer_changed_paths(docker, is_dev_tool=False) == ["Dockerfile"]
    npm = make_pr("preact", "10.0.0", "10.0.1", package_type="npm_and_yarn")
    assert model.infer_changed_paths(npm, is_dev_tool=False) == ["package.json"]


@pytest.mark.parametrize(
    "text",
    [
        "name: D\non: push\njobs: {}",
        "name: D\non: [push]\njobs: {}",
        "name: D\non:\n  push:\njobs: {}",
        "name: D\non:\n  push:\n    paths-ignore: ['docs/**']\njobs: {}",
    ],
)
def test_unrestricted_push_triggers_are_deploying(text):
    # GitHub semantics: a push trigger without a branches filter fires on every
    # branch, including main — these forms must classify as deploying.
    assert parse_deploy_yaml("svc", text).is_deploying_service is True


def test_real_changed_files_override_inference(model):
    # A dev-tool pip bump would be *inferred* to touch requirements/dev.txt
    # (ignored), but the PR's real diff touches the prod lockfile.
    pr = make_pr("coverage", "7.0.0", "7.0.1")
    pr.changed_files = ["requirements/prod.txt"]
    assert model.deploys_to_prod(pr, is_dev_tool=True) is True


def test_real_changed_files_all_ignored_is_not_deploying(model):
    pr = make_pr("ruff", "0.1.0", "0.1.1")
    pr.changed_files = ["requirements/dev.txt", "docs/notes.md"]
    assert model.deploys_to_prod(pr, is_dev_tool=False) is False


def test_publish_on_merge_repo_is_deploying():
    from dependabot_batch_review.deploy_model import DeployModelCache

    cache = DeployModelCache(None, "hypothesis", publish_on_merge_repos=["client"])
    assert cache.get("client").is_deploying_service is True
