# Hades Agent Fork — E2E Rename/Refactor Audit (2026-07-21)

Branch audited: `audit/phase-5-hygiene` @ `e26a4a3e39`  
Scope: end-to-end defect inventory after Hermes → Hades renames, dual-compat shims, and upstream merges.  
Companion: phases 0–5 security/reliability track in `docs/audits/2026-07-21-agent-audit-remediation.md` (mostly complete).

**Verdict: do not approve as “rename complete.”** Multiple security gates and packaging contracts are still single-spelling Hermes, while the live product folds paths and brands to Hades. That split is the root cause of most remaining glitches.

---

## Executive summary

| Severity | Count (approx) | Theme |
|----------|----------------|-------|
| **P0 – security / broken gates** | 5 clusters | Approval patterns, file_tools deny, threat_patterns, launchd label |
| **P0 – failing CI contracts** | 2 suites | `test_env_alias_guard` (72 sites), `test_hermes_compat_shims` packaging |
| **P1 – packaging / install** | 4 | `package-data` key, missing `hades_bootstrap`, wheel assets, extras name |
| **P1 – dual-home / container defaults** | 6+ | `/root/.hermes`, credential mounts, sandbox markers |
| **P2 – identity / branding drift** | many | i18n, desktop `hermesDesktop`, docker user `hermes`, User-Agents |
| **P3 – structural debt** | severe | God files 6k–23k LOC; dual package surface without single source of truth |

**Live test evidence (this worktree):**

```
FAILED  tests/test_env_alias_guard.py::test_no_new_single_spelling_env_access   (72 offenders)
FAILED  tests/test_hermes_compat_shims.py::test_packaging_declares_hermes_shims
FAILED  tests/tools/test_approval.py  (23 tests — Hades-named security paths not gated)
PASSED  tests/agent/test_file_safety_sandbox_mirror.py  (14)  # dual-name mirror fixed in phase-1
```

Runtime proof (approval detector):

| Command | Detected dangerous? |
|---------|---------------------|
| `sed -i … ~/.hermes/config.yaml` | **Yes** |
| `sed -i … ~/.hades/config.yaml` | **No** ← P0 |
| `echo forged >> ~/.hermes/approval_requests.json` | **Yes** |
| `echo forged >> ~/.hades/approval_requests.json` | **No** ← P0 |
| `launchctl kickstart …/ai.hermes.gateway` | **Yes** |
| `launchctl kickstart …/ai.hades.gateway` | **No** ← P0/P1 |

---

## Architecture diagnosis (code-judo, not polish)

The rename was implemented as **scattered string replacement + thin shims**, not as a single dual-name policy layer. Consequences:

1. **Two truths at once.** Canonical packages are `hades_*`, but large call graphs still import `hermes_*` aliases; some production code still only understands Hermes path basenames.
2. **Fold target ≠ pattern alphabet.** `tools/approval.py` rewrites absolute homes to `~/.hades`, then matches only `~/.hermes` / `$hermes_home`. Comments claim dual coverage; regex does not.
3. **Env dual-spelling is policy without enforcement.** `hades_constants.env_get/env_set` exist; `tests/test_env_alias_guard.py` fails with **72** direct `os.getenv("HADES_…")` / `HERMES_…` sites (and plugins/scripts are outside the scan entirely).
4. **God files absorbed the rename.** `gateway/run.py` (~23k), `hades_cli/web_server.py` (~19k), `tui_gateway/server.py` (~17k), `cli.py` (~16k), `hades_cli/main.py` (~15k). Further rename patches inside these files will keep producing incomplete dual-name bugs.
5. **Missed code-judo:** introduce one module (e.g. `agent/identity_paths.py` or extend `hades_constants`) that owns:
   - home basenames `{.hades,.hermes}`
   - env prefixes `{HADES_,HERMES_}`
   - service labels `{hades-gateway,ai.hermes.gateway,…}`
   - container bases `{/root/.hades,/root/.hermes}`
   - packaging keys  
   and **generate** approval/threat/file_tools regexes from it. Stop hand-maintaining parallel regexes.

---

## P0 — Security / correctness blockers

### R-SEC-01 — Terminal approval patterns are Hermes-only after Hades fold

**Files:** `tools/approval.py` (`_HERMES_ENV_PATH`, `_HERMES_CONFIG_PATH`, `_HERMES_APPROVAL_STATE_PATH`, `_rewrite_resolved_hermes_home`)  
**Evidence:** 23 failures in `tests/tools/test_approval.py` (`TestHermesApprovalStateWriteProtection`, `TestHermesConfigWriteProtection`, Windows fold, IFS sed).  
**Bug:** Fold rewrites to `~/.hades`; static patterns only match `.hermes` / `$hermes_home`.  
**Impact:** Agent can `sed -i` / redirect / forge `~/.hades/config.yaml` and `approval_requests.json` without approval — the exact bypass these patterns were built to stop.  
**Fix:**

```python
# Patterns must accept both basenames AND both env spellings, e.g.:
r'(?:~\/\.(?:hades|hermes)/|(?:\$home|\$\{home\})/\.(?:hades|hermes)/|(?:\$(?:hades|hermes)_home|\$\{(?:hades|hermes)_home\})/)'
```

Prefer generating from a shared dual-home path builder. Add regression tests that fail on *either* spelling.

### R-SEC-02 — `file_tools` approval-state deny only looks for `/.hermes/`

**File:** `tools/file_tools.py` ~706–712  
**Bug:** String ends-with / marker checks use `/.hermes/approval_requests.json` and `/.hermes/profiles/` only. Resolved `get_hades_home()` path is checked, but path-shape fallbacks miss pure `/.hades/` layouts and sandbox strings.  
**Fix:** Dual-name markers; share helper with approval module.

### R-SEC-03 — Threat patterns only mention `.hermes`

**File:** `tools/threat_patterns.py` lines 129–131  
**Bug:** `hermes_env` / `hermes_config_mod` miss `~/.hades/.env` and `.hades/(config.yaml|SOUL.md)`.  
**Fix:** Dual basename groups; rename pattern IDs to neutral `agent_env` / `agent_config_mod` or keep aliases.

### R-SEC-04 — Launchd kickstart guard is Hermes-only

**File:** `tools/approval.py` ~712  
**Pattern:** `(hermes|ai\.hermes)` — does **not** match `ai.hades.gateway` (runtime confirmed `danger=False`).  
**Fix:** Include `hades|ai\.hades|hades-gateway` (and whatever labels `hades_cli/gateway.py` actually installs).

### R-SEC-05 — Container / credential defaults still `/root/.hermes`

**Files:** `tools/credential_files.py` (defaults on ~10 APIs), `tools/file_tools.py` (`_get_container_mirror_prefix_for_task` → `/root/.hermes`), `tools/image_generation_tool.py` remote home → `.hermes`.  
**Impact:** Fresh Hades Docker/SSH sessions mount or translate to legacy path; host uses `.hades` → credential/cache miss or writes to wrong tree.  
**Fix:** Single `default_container_home_basename()` preferring active home name; accept both on read.

---

## P0 — Failing suite contracts (rename incomplete)

### R-CI-01 — Env alias guard: 72 single-spelling offenders

**Test:** `tests/test_env_alias_guard.py`  
**Top files:**

| Count | File |
|------:|------|
| 32 | `cli.py` |
| 20 | `tools/kanban_tools.py` |
| 7 | `run_agent.py` |
| 4 | `tui_gateway/compute_host.py` |
| 3 | `agent/conversation_compression.py` |
| 1 each | `gateway/mission_delivery.py`, `cron/scheduler.py`, `hades_cli/oneshot.py`, `hades_cli/workflows_dispatcher.py`, `agent/conversation_loop.py`, `agent/shell_hooks.py` |

Notable dual-spelling holes:

- `HERMES_PROFILE` only in `gateway/mission_delivery.py`, `hades_cli/workflows_dispatcher.py`
- `HERMES_INFERENCE_PROVIDER` only in `hades_cli/oneshot.py`
- `HERMES_MODEL` debug string in `cron/scheduler.py`
- `HERMES_GIT_BASH_PATH` in `agent/shell_hooks.py`
- Massive `HADES_*` direct reads in CLI/kanban (never see `HERMES_*` twin)

**Fix policy:** Convert every site to `env_get` / `env_set` / `env_pop` / `env_is_set`. Do **not** grow ALLOWLIST for production code. Optionally expand SCAN_DIRS to `plugins/`.

### R-CI-02 — Packaging: `package-data` keyed on shim package

**Test:** `tests/test_hermes_compat_shims.py::test_packaging_declares_hermes_shims`  
**File:** `pyproject.toml`

```toml
[tool.setuptools.package-data]
hermes_cli = ["web_dist/**/*", "tui_dist/**/*", "scripts/install.sh", "scripts/install.ps1"]
```

**Bug:** Real package is `hades_cli`. Wheel packaging attaches package-data to the **real** package name; keying `hermes_cli` **silently drops** `web_dist` / `tui_dist` from wheels → empty dashboard SPA / missing TUI assets on pip installs.  
**Fix:**

```toml
hades_cli = ["web_dist/**/*", "tui_dist/**/*", "scripts/install.sh", "scripts/install.ps1"]
# optional: also list hermes_cli only if a real package tree is shipped (it is not — only shims)
```

### R-CI-03 — Approval suite red (23)

Covered under R-SEC-01. Treat as release blocker for any security-sensitive tag.

---

## P1 — Packaging / entry points / bootstrap

### R-PKG-01 — `hades_bootstrap` missing from `py-modules`

**File:** `pyproject.toml` `[tool.setuptools] py-modules`  
Lists `hermes_bootstrap` and `hades_constants` etc., but **not** `hades_bootstrap`.  
`hermes_bootstrap.py` does `import hades_bootstrap` → sealed wheel may `ModuleNotFoundError` on bootstrap import path.

### R-PKG-02 — Project extras still named `hermes-agent[...]`

`pyproject.toml` optional-deps self-references `"hermes-agent[cron]"`, etc., while `[project] name = "hades-agent"`.  
Install of `hades-agent[all]`-style extras may not resolve self-deps correctly depending on installer.

### R-PKG-03 — Dual console scripts OK; docs/i18n still teach `hermes` only

Scripts: both `hermes` and `hades` entry points exist (good).  
`locales/en.yaml` still: “Hermes Commands”, `hermes gateway restart`, `hermes update`, key `hermes_cmd_not_found`. User-facing mixed identity.

### R-PKG-04 — Docker still Hermes-shaped

- User: `useradd … hermes`
- Paths: `/opt/hades` code + data often `.hermes`
- Env: `HERMES_WEB_DIST`, `HERMES_TUI_DIR`, `HERMES_GIT_SHA`, cont-init `01-hermes-setup`
- Exec shim: `/opt/hades/bin/hermes` only (no `hades` bin in Dockerfile snippet)

Adopt-in-place is fine; **document** and dual-wire `hades` binary + `HADES_*` env mirrors.

---

## P1 — Dual-home / path policy incompleteness

### R-PATH-01 — Home adoption exists; consumers don’t share it

`hades_constants._default_home_candidates()` correctly prefers `.hades` with legacy `.hermes` adopt-in-place.  
Many tools still hardcode one spelling (credential_files, image tools, approval, threat_patterns, file_ops temp `.hermes-tmp`, build_info `.hermes_build_sha`).

### R-PATH-02 — Windows docs drift

`get_hades_home` docstring still says `%LOCALAPPDATA%\\hermes` in places; implementation returns `Local\\hades` with legacy `Local\\hermes` adoption. Node install path comments still say `\\hermes\\node`.

### R-PATH-03 — `load_hermes_dotenv` never renamed

Canonical loader is still `load_hermes_dotenv(hermes_home=…)` in `hades_cli/env_loader.py`. Works, but every new contributor will reintroduce Hermes-only APIs. Add `load_hades_dotenv = load_hermes_dotenv` and prefer the Hades name at call sites.

### R-PATH-04 — `hermes_tools.py` sandbox module name frozen

`tools/code_execution_tool.py` ships `hermes_tools.py` into sandboxes (`from hermes_tools import …`). Freezing the import name is OK for skill compatibility **if documented**; otherwise generate `hades_tools.py` with re-export alias.

### R-PATH-05 — Desktop bridge remains `window.hermesDesktop`

`apps/desktop` extensively uses `window.hermesDesktop`, `HermesGateway`, etc., while package name is `hades`. Preload/bridge rename incomplete → any new `hadesDesktop` API will be dead until dual-bound.

### R-PATH-06 — TUI package name mix

- `ui-tui/package.json` name: `hermes-tui`
- Depends on `@hades/ink` from directory `packages/hermes-ink`
- `externalCli.ts`: `HERMES_BIN` / default `hermes`
- Heapdump paths: `hermes-…` under `~/.hades`

Cosmetic + ops confusion; pin one public CLI name in docs (`hades` preferred, `hermes` alias).

---

## P2 — Identity, plugins, incomplete twins

### R-ID-01 — Missing Hades twins for helper functions

| Hermes name | Location | Notes |
|-------------|----------|--------|
| `hermes_subprocess_env` | `tools/environments/local.py` | Used widely; add `hades_subprocess_env = …` |
| `hermes_xai_user_agent` | `tools/xai_http.py` | UA string likely still “Hermes” |
| `hermes_client_tag` | `agent/portal_tags.py` | |
| `hermes_lsp_session_dir` / `hermes_lsp_bin_dir` | `agent/lsp/*` | |
| `hermes_home()` | optional-skills unbroker | |

### R-ID-02 — Effects adapter still `hermes_state.py` / `Hermes*Adapter`

`agent/effects/adapters/hermes_state.py` + `HermesConfigStateAdapter` etc. Phase-5 marked “effects NotImplemented surface” partial. ABCs still raise `NotImplementedError` by design in `registry.py` — OK if abstract; ensure concrete adapters registered in production paths.

### R-ID-03 — Auto-routing plugin imports only Hermes surface

`plugins/auto_routing/**` imports `hermes_constants.get_hermes_home`, `hermes_cli.*` exclusively. Works via shims; blocks any future hard cutover of shim removal.

### R-ID-04 — ACP provenance `_meta.hermes`

`acp_adapter/provenance.py` documents Hermes extension namespace. Dual-publish `_meta.hades` or document wire-compat forever.

### R-ID-05 — Skill metadata `metadata.hermes` vs `metadata.hades`

`utils.py` / blueprints prefer `metadata.hermes` with hades fallback (or vice versa). Audit that **write** path emits the preferred key consistently; dual-read both.

### R-ID-06 — User-Agent strings

`gateway/platforms/base.py`, `hades_cli/models.py`: `HermesAgent/1.0`. Low severity; update for support triage.

---

## P3 — Structural / maintainability (strict review bar)

These are not “nits”; they are why rename bugs keep regenerating.

### R-STR-01 — God files past any healthy bound

| File | ~LOC |
|------|-----:|
| `gateway/run.py` | 22 939 |
| `hades_cli/web_server.py` | 18 920 |
| `tui_gateway/server.py` | 16 906 |
| `cli.py` | 16 157 |
| `hades_cli/main.py` | 15 073 |
| `hades_state.py` | 11 290 |
| `plugins/auto_routing/.../storage.py` | 11 089 |
| `plugins/auto_routing/.../service.py` | 10 909 |
| `hades_cli/config.py` | 9 307 |

**Rule:** no more feature/rename patches that grow these without extraction. Priority extractions: gateway slash dispatch, dashboard routes, TUI RPC catalog, CLI command mixins (partially started).

### R-STR-02 — Dual package surface without ownership

- Real: `hades_cli/` (~164 modules)
- Shim: `hermes_cli/` (~17 thin modules + alias loader)
- Top-level: parallel `hades_*.py` / `hermes_*.py` shims

The alias package is correct **compat**, but production code should import **only** `hades_*`. Grep shows many non-test `from hermes_*` imports still in core (`cron/scheduler.py`, `tui_gateway/server.py`, `acp_adapter/*`, plugins).  
**Policy:** ratchet test: new code in `agent/`, `gateway/`, `tools/`, `hades_cli/` must not import `hermes_*` (allowlist plugins temporarily).

### R-STR-03 — `hermes_cli` package-data / comments still lie

Comments in `pyproject.toml` and MANIFEST still say `hermes_cli/plugins.py`, `hermes_cli/mcp_catalog.py`. Misleading for dual-name debugging.

### R-STR-04 — Deferred from prior audit (still open)

From `2026-07-21-agent-audit-remediation.md`:

| Item | Status |
|------|--------|
| L1-06 conversation_loop modularization | deferred |
| L1-01 full async loop | out of scope |
| L2-02 hard codex security_mode | deferred |
| L3-03 encrypt secret disk cache | deferred |
| L3-04 terminal as hard security boundary | deferred |
| Dual-home `~/.hades` vs `~/.hermes` consolidation | **operator deferred** — but dual-**accept** is not optional for security (see P0) |

### R-STR-05 — Rename incomplete in operator-facing copy

- Banner/skins: mostly “Hades Agent” (good)
- i18n: still Hermes-majority
- AGENTS.md: mixed Hades product / Hermes history (acceptable if deliberate)
- `hades_cli/main.py` still prints `Requires: Hermes {version}` for plugin requires

---

## Recommended remediation phases (follow-on track)

| Phase | Branch suggestion | Scope | Exit criteria |
|-------|-------------------|-------|---------------|
| **6** | `audit/phase-6-dual-path-security` | R-SEC-01…05, approval tests green, threat_patterns, file_tools markers | `test_approval.py` full green; dual-path unit matrix |
| **7** | `audit/phase-7-env-alias-completion` | All 72 env guard offenders + plugins scan expansion | `test_env_alias_guard` green; zero new allowlist production entries |
| **8** | `audit/phase-8-packaging` | package-data key, py-modules `hades_bootstrap`, extras self-name, wheel smoke | `test_hermes_compat_shims` green; install wheel → web_dist present |
| **9** | `audit/phase-9-identity-layer` | Extract dual-name policy module; migrate approval/threat/credential defaults to it; import ratchet | Single source of truth; hermes imports banned in core |
| **10** | `audit/phase-10-surface-hygiene` | i18n, Docker dual bin, desktop bridge dual bind, UA strings, docs | User-visible “Hades” with Hermes alias only where intentional |
| **11** | `audit/phase-11-godfile-extraction` | Split gateway/cli/tui/main (behavior-neutral) | No file touched for features is >1k growth without extract |

**Do not** attempt phases 6–10 as drive-by edits inside 15k-line files without tests; each phase should land with focused tests already failing red on main.

---

## Minimal dual-path test matrix (add once, keep forever)

```text
For each of {config.yaml, .env, approval_requests.json, SOUL.md}:
  paths: ~/.hades/X, ~/.hermes/X, $HADES_HOME/X, $HERMES_HOME/X,
         $hades_home/X, $hermes_home/X, <abs get_hades_home()>/X
  ops:   sed -i, tee, >>, cp, mv, ln -s, perl -i, ruby -i
  expect: dangerous=True (approval) / denied (file_tools)

For env:
  set only HERMES_FOO → env_get("HADES_FOO") works
  set only HADES_FOO → env_get("HERMES_FOO") works  # if dual-write policy
  env_set writes both spellings

For packaging:
  wheel contains hades_cli/web_dist/**
  import hades_bootstrap succeeds from installed wheel
```

---

## What is already in good shape

- `hades_constants` adopt-in-place for default home (`.hades` vs `.hermes`)
- Module-object identity shims (`sys.modules[hermes_*] = hades_*`) — correct compat design
- `hermes_cli` alias package loader — correct approach for plugins
- Dual console scripts `hermes` + `hades`
- Phase 1–5 security/reliability items largely marked done (file_safety mirror dual-name **tests pass**)
- Skin branding defaults to “Hades Agent”
- `HadesCLI` with `HermesCLI = HadesCLI` alias

The problem is **not** “missing shims at the root.” The problem is **consumers that never learned the dual alphabet** after the fold target flipped to Hades.

---

## Approval bar (this audit)

**Not approved** for “rename complete” or release hygiene.

Presumptive blockers before any further feature work on this fork:

1. R-SEC-01 approval dual-path (23 red tests)
2. R-CI-01 env alias guard (72 sites)
3. R-CI-02 package-data `hades_cli` key
4. R-PKG-01 `hades_bootstrap` in py-modules

Everything else can sequence behind those four.

---

## Appendix A — Env alias offenders (by file)

See live failure output of:

```bash
scripts/run_tests.sh tests/test_env_alias_guard.py -q --tb=line
```

Primary clusters: `cli.py` (32), `tools/kanban_tools.py` (20), `run_agent.py` (7), `tui_gateway/compute_host.py` (4), plus single-site HERMES-only reads listed under R-CI-01.

## Appendix B — Related docs

- `docs/audits/2026-07-21-agent-audit-remediation.md` — agent/ P0–P5 security track
- `tests/test_hermes_compat_shims.py` — packaging + shim identity contracts
- `tests/test_env_alias_guard.py` — dual-spelling ratchet
- `Agents.md` — dual env policy (`env_get` / `env_set`)

## Appendix C — Reproduction commands

```bash
scripts/run_tests.sh tests/test_env_alias_guard.py -q --tb=line
scripts/run_tests.sh tests/test_hermes_compat_shims.py -q --tb=short
scripts/run_tests.sh tests/tools/test_approval.py -q --tb=line
scripts/run_tests.sh tests/agent/test_file_safety_sandbox_mirror.py -q

python3 - <<'PY'
from tools.approval import detect_dangerous_command
for c in [
  "sed -i 's/x/y/' ~/.hades/config.yaml",
  "sed -i 's/x/y/' ~/.hermes/config.yaml",
  "echo forged >> ~/.hades/approval_requests.json",
  "echo forged >> ~/.hermes/approval_requests.json",
]:
    print(c, "->", detect_dangerous_command(c)[0])
PY
```
