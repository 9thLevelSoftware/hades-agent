# GitHub Actions hygiene (2026-07-22)

## Diagnosis

Recent Actions history on `9thLevelSoftware/hades-agent` was almost entirely red:

| Failure | Root cause |
|---------|------------|
| Python tests / e2e / uv.lock / desktop e2e | `uv.lock` still named package `hermes-agent` after rename to `hades-agent` → `uv sync --locked` fails |
| JS matrix (`web`, etc.) | `js-tests.yml` ran `npm run fix`, which most workspaces do not define |
| Skills Index / Freshness | Upstream Nous docs pipeline; probes `hermes-agent.nousresearch.com` |
| js-autofix | Org policy: “GitHub Actions is not permitted to create or approve pull requests” |
| Desktop / TUI typecheck | Stale types after partial merges (`message.interim`, park queue APIs, memory field fixtures) |

## Decisions

### Deleted (useless or harmful for this fork)

| Workflow | Why |
|----------|-----|
| `skills-index.yml` | Builds/crawls public skill hubs for Nous docs site |
| `skills-index-freshness.yml` | Probes Nous-hosted index; opens issues with App token |
| `deploy-site.yml` | Vercel + Pages deploy for hermes docs |
| `upload_to_pypi.yml` | PyPI publish not configured for this package |
| `js-autofix.yml` | Cannot open PRs under current org settings |
| `contributor-check.yml` | Nous contributor-attribution process |
| `history-check.yml` | Unrelated-history gate for large OSS community |
| `review-labels.yml` | `ci-reviewed` maintainer label gate |
| `label-rerun.yml` | Depends on review-labels |

### Kept and fixed

| Workflow | Role |
|----------|------|
| `ci.yml` | Lean orchestrator + single gate check |
| `tests.yml` | Python test slices |
| `lint.yml` | ruff / ty |
| `js-tests.yml` | Workspace typecheck + test (`check` only) |
| `uv-lockfile-check.yml` | Lockfile sync |
| `lockfile-diff.yml` | PR npm lock review status |
| `docker-lint.yml` | hadolint / shellcheck when Docker files change |
| `docs-site-checks.yml` | Docusaurus build when docs change |
| `supply-chain-audit.yml` | Dep bounds / action pin audit on relevant PRs |
| `osv-scanner.yml` | Vulnerability scan |
| `docker.yml` | Manual / release image build (not every PR) |
| `e2e-desktop.yml` | File kept; **not** required by CI gate (optional later) |

### Code fixes shipped with this change

- Refresh `uv.lock` for package name `hades-agent`
- Add `message.interim` to TUI `GatewayEvent`
- Implement `parkQueuedPrompts` / `unparkQueuedPrompts` + `queueParked`
- Fix memory provider test fixtures (`group`, `inline`, `docs_url`)
- Docker image name → `9thlevelsoftware/hades-agent`

## Branch protection

Require only: **All required checks pass**.

## Follow-up after first CI run (same PR)

### Critical product bug unblocked by workflow work

`cli.py` called `env_set(...)` **before** importing it (phase-7 bulk conversion
bug). That broke every test that imports `cli` at collection time.

### Remaining suite debt (not workflow inheritance)

Even with a lean CI and a correct lockfile, full Python CI still reports
dozens of real test failures from incomplete Hermes→Hades renames and
partial upstream merges (compressor floor tests, streaming, plugins, etc.).
Those are **codebase** defects, not Actions config — track separately.

### JS follow-ups shipped on the PR

- QueuePanel `onResume` / `parked` props
- topupCommand billing copy assertion
- bulk test rename of `hermes_time` / `reload(hades_constants)` leftovers
