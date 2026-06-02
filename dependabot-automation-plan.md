# Dependabot Auto-Merge Automation — Findings & Architecture

**Org:** hypothesis · **Date:** 2026-06-02 · **Author:** investigation by Claude (23-repo fan-out)

> Goal: stop the team from hand-merging Dependabot PRs and babysitting 30-minute
> pipelines. Make the safe ones merge themselves daily, gate the risky ones to a
> human, verify production health after deploy via Sentry/New Relic, and roll back
> automatically with an alert when something breaks.

---

## 1. Executive summary

- **203 open Dependabot PRs across 23 repos** right now (real number is higher; org
  search caps at 200). Risk mix: **116 low / 36 medium / 51 high**. CI: **188 passing
  / 11 failing / 4 none**. **50 PRs are >30 days stale.**
- **~60 PRs are safely auto-mergeable today** (CI-passing + low-risk + ≥3 days old):
  dev tooling (ruff/mypy/black/pytest/coverage/pylint), lockfile-only bumps,
  `pip`/`pip-tools`/`wheel`, and patch bumps. These never touch production runtime.
- **Merging to `main` auto-deploys to production.** 12 of 23 repos are deployed
  services where `push:main` → Docker Hub build → Elastic Beanstalk **staging** →
  **production**. So "auto-merge" literally means "auto-deploy to prod" for runtime
  dependency bumps. This is the central fact of the design.
- **⚠ Branch protection is mostly absent or unverifiable.** Multiple repos have **no
  required status checks and no required approvals** on `main`, and `allow_auto_merge`
  is enabled. The CI gate you'd assume exists at the GitHub level largely does not —
  **the auto-merger must enforce CI-pass itself**, and we should separately harden
  branch protection.
- **Monitoring is real and usable:** Sentry in 12 repos, New Relic in 10 (the deployed
  services have both — `sentry-sdk`+`h-pyramid-sentry`, `newrelic-admin run-program
  gunicorn`). This makes a post-deploy health gate feasible.
- **There's already a strong rollback primitive:** the shared deploy workflow supports
  `operation: redeploy` and every service has a `redeploy.yml` — we can roll back to the
  previous EB version without a code revert.

## 2. The deploy-coupling model (why this is high-stakes)

Representative `deploy.yml` (lms, h, bouncer, …):

```
on:
  push: { branches: [main], paths-ignore: [ requirements/*, '!requirements/prod.txt', docs/*, '*.md', tox.ini, tests/* ... ] }
jobs: docker_hub -> staging (EB) -> production (EB)   # prod needs staging to pass
```

Consequence — a merged Dependabot PR's blast radius depends on **what file it touches**:

| Bump kind | Touches | Deploys to prod? |
|---|---|---|
| Dev tool (ruff, mypy, black, pytest, coverage, pylint, isort) | `requirements/dev.txt` | **No** (path-ignored) |
| Prod Python dep | `requirements/prod.txt` / `requirements.txt` | **Yes** (un-ignored) |
| Docker base image | `Dockerfile` | **Yes** |
| Frontend/npm (service repos) | `package.json`/lockfile | **Yes** (not ignored) |
| `pip`/`pip-tools`/`wheel` lockfile tooling | lock files only | **No** |

This is exactly why risk-tiering must distinguish *deploying* bumps from *non-deploying*
ones, and why the Sentry/New Relic gate + auto-rollback are mandatory for the deploying tier.

## 3. Fleet inventory

| repo | kind | prod-deploy | open PRs | auto-mergeable | Sentry | NewRelic |
|---|---|:--:|:--:|:--:|:--:|:--:|
| bouncer | service | ✅ | 18 | 5 | ✅ | ✅ |
| h | service | ✅ | 16 | 7 | ✅ | ✅ |
| viahtml | service | ✅ | 15 | 6 | ✅ | ✅ |
| checkmate | service | ✅ | 14 | 3 | ✅ | ✅ |
| lms | service | ✅ | 14 | 5 | ✅ | ✅ |
| via | service | ✅ | 13 | 5 | ✅ | ✅ |
| h-periodic | service | ✅ | 12 | 4 | ✅ | ✅ |
| frontend-shared | frontend-lib | ✅* | 11 | 8 | – | – |
| browser-extension | frontend-lib | – | 11 | 6 | ✅ | – |
| client | frontend-lib | ✅* | 11 | 7 | ✅ | – |
| exam-notes | service | ✅ | 11 | 1 | ✅ | ✅ |
| test-pyapp | tooling | – | 11 | 2 | – | – |
| annotation-ui | frontend-lib | – | 10 | 0 | – | – |
| report | service | ✅ | 10 | 2 | ✅ | ✅ |
| frontend-build | frontend-lib | – | 9 | 3 | – | – |
| test-pyramid-app | service | ✅ | 4 | 1 | ✅ | ✅ |
| frontend-testing | frontend-lib | – | 3 | 0 | – | – |
| biotome | infra | – | 3 | 0 | – | – |
| dependabot-batch-review | tooling | – | 2 | 0 | – | – |
| commando | library | – | 2 | 0 | ✅ | – |
| websocket-tester | tooling | – | 1 | 0 | ✅ | – |
| cookiecutters | tooling | – | 1 | 0 | – | – |
| workflows | infra | – | 1 | 0 | – | – |

\* frontend libs publish to npm; "prod-deploy" there means a release/publish job, not EB.

**30 high-risk + CI-passing + prod-touching PRs** (the human-review pile) — e.g.
`newrelic 11→13`, `gunicorn 23→26`, `cryptography 44→46`, `marshmallow 3→4`,
`urllib3 2.5→2.7`, `requests`, `pyjwt`, `node 25→26-alpine`, `zope-sqlalchemy 3→4`.

**11 CI-failing** (do not merge; triage): `h#10112 pyjwt`, `lms#7367 types-xmltodict`,
eslint-group failures across `frontend-shared/client/annotation-ui/frontend-build`,
`annotation-ui#181 typescript 5→6`, `browser-extension#1907 babel-plugin-istanbul`,
`checkmate#1083/1084 pylint/black`.

**Caveat — risk model consistency:** these tiers came from 23 independent agents and are
~90% consistent but not identical (e.g. a dev-tool major like `black 25→26` was "high" in
one repo, "low" in another). The real tool must compute risk from **one deterministic
function**, not per-repo heuristics.

## 4. Proposed architecture

A **centralized orchestrator** (extends this `dependabot-batch-review` package) that
operates across the org via the GitHub API — no per-repo workflow changes required. Five
components:

### A. Daily auto-merger (`automerge.py` + scheduled GitHub Action)
- Runs on `schedule: cron` daily + `workflow_dispatch`.
- Config (env / `automerge.yml`): `min_age_days` (default **3**), per-tier enable flags,
  repo allow/deny lists, per-ecosystem rules, `dry_run`.
- **Eligibility engine** (deterministic): a PR is auto-merge-eligible iff
  CI **passing** AND age ≥ `min_age_days` AND `mergeStateStatus` is clean (no conflict)
  AND its **risk tier** is enabled.
- **Risk tiers:**
  - **Tier 0 — never deploys** (dev deps, lockfile/tooling, patch non-prod): auto-merge.
  - **Tier 1 — deploys, low blast** (patch/minor prod dep, CI green): auto-merge **with
    health gate** (component B).
  - **Tier 2 — needs human** (major bumps, security-sensitive runtime libs, Docker/node
    base major, CI-failing, merge-conflicted): never auto-merge → escalate (component C).
- Cross-repo grouping (reuse existing group logic) so the same bump across N repos is one
  decision/one report line.

### B. Post-deploy health gate (`health.py`)
- Only for Tier 1 (deploying) merges. After merge, wait for the EB deploy to settle
  (poll the GitHub Environments/deploy run), then query:
  - **Sentry:** new unresolved issues / error-count delta for the project since the
    release, and/or release-health crash-free rate. (`GET /api/0/organizations/{org}/
    issues/?query=is:unresolved&statsPeriod=...` or sessions API.)
  - **New Relic:** NRQL via GraphQL — `SELECT count(*) FROM TransactionError WHERE
    appName='<app>' SINCE 10 minutes ago` vs a baseline.
- Verdict: healthy → done; degraded → trigger component C.

### C. Rollback + alert (`rollback.py` + `slack.py`)
- **Rollback options (pick policy):**
  1. **EB redeploy previous version** via the existing `redeploy.yml` / shared
     `operation: redeploy` — fastest, no code change. *(recommended for prod incidents)*
  2. **`git revert` the merge commit** on `main` — re-deploys clean code, self-documenting.
  3. **Alert-only** — page a human, no automated action.
- Alerts go to **Slack** (reuse `slack.py`): the merge, the health verdict, the rollback
  action taken, and a link. Tier-2 escalations post a digest of PRs needing human review.
- Optional **Claude Agent SDK** triage: summarize a risky PR's changelog/diff + advisory
  and post a recommended action, so humans decide in seconds not minutes.

### D. Bulk backlog tool (`bulk.py` CLI)
- One-shot sweep of the *entire* remaining backlog (not just last-N-days): same eligibility
  engine, batched by wave (low → medium → high), `--dry-run`, `--max-per-wave`, `--repo`,
  `--tier`. This is how we burn down the current 203 → ~baseline.

### E. Local TUI monitor (`monitor.py`, curses)
- A live "kickoff + watch" dashboard for running the sweep locally: per-PR rows with
  live CI status, merge state, health verdict, rollbacks; keybinds to approve/skip/abort.
  Complements (doesn't replace) the existing web dashboard + the GitHub Actions logs.

### Reuse map (what already exists)
- `github_client.py` (GraphQL) · `review.py::fetch_dependency_prs/analyze_risk/merge_pr`
  · grouping · `slack.py` · web dashboard (`server.py`). New code is the *autonomous
  layer*, not a rewrite.

## 5. Phased rollout (de-risked)

1. **Phase 0 — observe (dry-run):** daily Action runs the eligibility engine, posts "would
   merge / would escalate" to Slack. No merges. Validate the risk model against reality for ~1 week.
2. **Phase 1 — Tier 0 only:** auto-merge non-deploying bumps (dev/tooling/lockfile). Zero
   prod blast radius. Burn down ~60 candidates with the bulk tool.
3. **Phase 2 — Tier 1 + health gate:** enable patch/minor prod bumps with Sentry/New Relic
   post-deploy verification + auto-rollback on a 1–2 repo pilot (e.g. bouncer, viahtml),
   then widen.
4. **Phase 3 — escalation polish:** Claude-SDK triage digests for Tier 2; tune thresholds.
5. **Parallel hardening:** turn on branch protection (require CI + restrict who can merge)
   so GitHub enforces the gate the tool relies on.

## 6. Open decisions (need owner input)

1. **Blast-radius / auto-merge scope** — start at Tier 0 only, or go to Tier 1 (deploying
   bumps behind the health gate) once piloted?
2. **Rollback policy** — EB `redeploy` previous version, `git revert`, or alert-only?
3. **Monitoring tokens** — can we provision a Sentry API token + New Relic API key (user
   key + account/app IDs) to CI secrets? Which is the primary health signal?
4. **Alerts / escalation channel** — which Slack channel; do we want Claude-SDK PR-risk
   triage in the digest?
