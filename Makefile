.PHONY: qa
qa: checkformat typecheck lint test

PYTHON_SRCS=dependabot_batch_review

.PHONY: test
test:
	poetry run pytest -q

.PHONY: checkformat
checkformat:
	poetry run ruff format --check $(PYTHON_SRCS)

.PHONY: format
format:
	poetry run ruff format $(PYTHON_SRCS)

.PHONY: lint
lint:
	poetry run ruff $(PYTHON_SRCS) --ignore E501

.PHONY: typecheck
typecheck:
	poetry run mypy --strict $(PYTHON_SRCS)
