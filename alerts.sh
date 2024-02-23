#!/bin/sh

poetry run python -m dependabot_batch_review.alerts "$@"
