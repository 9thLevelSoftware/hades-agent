# Agent E2E Audit Remediation Map (2026-07-21)

Safety-preserving remediation of the 2026-07-21 `agent/` audit. Live installs
that pin `HADES_HOME=~/.hermes` stay supported; this track does **not** migrate
homes or kill skill/memory review.

## Branch policy

| Phase | Branch (intended) | Scope |
|-------|-------------------|--------|
| 0 | `audit/phase-0-baseline` | Docs + test inventory only |
| 1 | `audit/phase-1-p0-security` | file_safety, codex kanban path, ACP denylist |
| 2 | `audit/phase-2-core-reliability` | retry ceiling, review flight, skill budget/redact |
| 3 | `audit/phase-3-transport` | codex queues, MCP instructions, elicitation soft |
| 4 | `audit/phase-4-memory` | prefetch/sync hardening |
| 5 | `audit/phase-5-hygiene` | trace/ssl/inline-shell/azure/effects/session lock |

Each phase is completed fully before its PR is opened.

## Finding → phase map

| ID | Summary | Phase | Status |
|----|---------|-------|--------|
| L3-01 | `file_safety` returns bare `True` for session state | 1 | done (phase-1) |
| L3-02 | Sandbox mirror only matches `.hades`, not `.hermes` | 1 | done (phase-1) |
| L2-01 | Codex kanban writable_roots string injection | 1 | done (phase-1) |
| L2-05 | ACP file attach skips read denylist | 1 | done (phase-1) |
| L1-02 | Turn retry budget resets on failover | 2 | done (phase-2) |
| L4-01 | Background review `in_flight` + aggressive prompts | 2 | done (phase-2) |
| L4-02 | Full skill inject without token budget | 2 | done (phase-2) |
| L4-06 | Skill config values may inject secrets | 2 | done (phase-2) |
| L2-03 | Unbounded codex notification queues | 3 | done (phase-3) |
| L2-04 / L4-05 | hermes-tools MCP over-advertises tools | 3 | done (phase-3) |
| L2-02 (soft) | hermes-tools elicitation auto-accept | 3 | done (phase-3) |
| L1-03 | External memory prefetch blocks hot path | 4 | pending |
| L1-04 | Sync executor falls back to inline I/O | 4 | pending |
| L3-06 (partial) | Trace upload exception scrubbing | 5 | pending |
| L3-08 (partial) | Global env prefix secret drift | 5 | pending |
| L3-04 (partial) | `raise_if_read_blocked` fail-open | 5 | pending |
| L3-07 / L3-05 | SSL skip log naming; hub inline-shell refuse | 5 | pending |
| L2-06 | Azure token provider marker | 5 | pending |
| L4-03 | Effects `NotImplemented` surface | 5 | pending |
| L2-07 | Codex session lifecycle race | 5 | pending |
| L1-05 (partial) | Nudge/review bookkeeping | 2/5 | pending |

## Explicitly deferred (not in this track)

| Item | Reason |
|------|--------|
| L1-06 conversation_loop modularization | Large behavior risk |
| L1-01 full async loop | Out of scope; keep interruptible sleep |
| L2-02 hard codex security_mode on thread/start | Bricks without codex permissions table |
| L3-03 encrypt secret disk cache | Storage format change |
| L3-04 terminal as hard security boundary | Separate product track |
| L4-01 default skill quarantine | Opt-in only if added later |
| Dual-home `~/.hades` vs `~/.hermes` consolidation | Operator deferred |

## Test inventory (baseline suites)

### Phase 1 — file safety / ACP / codex spawn

- `tests/agent/test_file_safety.py`
- `tests/agent/test_file_safety_session_state.py`
- `tests/agent/test_file_safety_sandbox_mirror.py`
- `tests/agent/test_file_safety_container_mirror.py`
- `tests/agent/test_file_safety_credentials.py`
- `tests/agent/test_file_safety_cross_profile.py`
- `tests/acp/test_server.py` (+ related `tests/acp/*`, `tests/acp_adapter/*`)
- `tests/agent/transports/test_codex_app_server_runtime.py`
- `tests/agent/transports/test_codex_app_server_session.py`

### Phase 2 — core reliability / skills / review

- `tests/agent/test_turn_retry_state.py`
- `tests/agent/test_reflection_triggers.py`
- `tests/run_agent/test_background_review*.py`
- `tests/test_background_review_*.py`
- `tests/tools/test_skill_*.py` / `tests/tools/test_skills_*.py`
- `tests/agent/test_memory_skill_scaffolding.py`
- `tests/run_agent/test_memory_nudge_counter_hydration.py`

### Phase 3 — codex transport / MCP

- `tests/agent/test_codex_app_server_*.py`
- `tests/agent/transports/test_codex_app_server_*.py`
- `tests/run_agent/test_codex_app_server_*.py`

### Phase 4 — memory manager

- `tests/agent/test_memory_async_sync.py`
- `tests/agent/test_memory_provider.py`
- `tests/agent/test_memory_session_switch.py`
- `tests/agent/test_memory_boundary_commit.py`
- `tests/run_agent/test_memory_sync_interrupted.py`

### Phase 5 — hygiene

- Existing secret_scope / redact / ssl_guard / azure_identity tests under `tests/`
- Effects coordinator tests if present under `tests/agent/`

## Baseline commands

```bash
scripts/run_tests.sh tests/agent/test_file_safety*.py -q
scripts/run_tests.sh tests/agent/test_turn_retry_state.py -q
scripts/run_tests.sh tests/agent/test_reflection_triggers.py -q
```

## Phase 0 baseline run (2026-07-21)

Command (subset):

```bash
scripts/run_tests.sh \
  tests/agent/test_file_safety.py \
  tests/agent/test_file_safety_session_state.py \
  tests/agent/test_file_safety_sandbox_mirror.py \
  tests/agent/test_turn_retry_state.py \
  tests/agent/test_reflection_triggers.py -q
```

| Suite | Result |
|-------|--------|
| `test_file_safety.py` | pass (22) |
| `test_file_safety_session_state.py` | pass (5) |
| `test_turn_retry_state.py` | pass (4) |
| `test_reflection_triggers.py` | pass (14) |
| `test_file_safety_sandbox_mirror.py` | **2 fail / 11 pass** |

### Known baseline failure (maps to L3-02)

`test_file_safety_sandbox_mirror.py` constructs paths under `home/.hades` but asserts
the mirror root string contains `home/.hermes`. Production detector matches
`.hades` only; legacy remotes still use `.hermes`. Phase 1 dual-name fix +
test alignment is the intended resolution — **not** a Phase 0 code change.

Failing tests:

- `TestClassifySandboxMirrorTarget.test_docker_mirror_soul_md_classified`
- `TestGetSandboxMirrorWarning.test_mirror_warning_names_mirror_root_and_inner_path`

### Live install note

Operator gateway uses `HADES_HOME=~/.hermes` with install at
`~/.hermes/hermes-agent` (commit parity with `origin/main` plus local dirty
edits on `model_switch.py` / `run_agent.py`). Remediation branches start from
clean `origin/main` and must not clobber those uncommitted install edits.

## Safety principles (recap)

1. No mid-conversation toolset/system-prompt rebuilds.
2. No message role-alternation breakage.
3. Dual-accept `.hades` / `.hermes` path shapes where relevant.
4. Defaults preserve current UX; harden behind high thresholds or opt-in.
5. Use `scripts/run_tests.sh`; real temp `HADES_HOME`; no change-detector snapshots.
6. Do not rewrite operator `HADES_HOME=~/.hermes` gateway env.
