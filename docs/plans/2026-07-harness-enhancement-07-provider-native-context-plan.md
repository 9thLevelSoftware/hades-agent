# Provider-Native Context Management Implementation Plan

> For agentic workers: add provider-native context management behind explicit capability gating. Preserve the full local transcript as canonical; provider-specific edits/compaction items are request/runtime state and must never corrupt cross-provider resume.

**Goal:** Use Anthropic server-side tool/thinking edits and compaction, plus OpenAI Responses compaction items where supported, while retaining the existing `ContextCompressor` fallback and prompt-cache discipline.

**Architecture:** Add a capability policy beside `anthropic_prompt_cache_policy`. Phase 1 decorates eligible Anthropic requests with `context_management.edits` and records applied edits. Phase 2 adds stateful Anthropic `compact_20260112` handling and OpenAI `/responses/compact`/threshold handling through transport-specific adapters. `ContextEngine` remains the abstraction; `ContextCompressor` remains universal fallback. The SQLite/session transcript is never rewritten by a provider-native edit; provider-native state is keyed by provider/model/session and discarded/rebuilt on provider switch.

**Tech Stack:** Existing `ContextEngine`, `ContextCompressor`, Anthropic adapter/transport, Codex Responses adapter/transport, beta-header policy, `CanonicalUsage`, `conversation_compression`, `memory_manager.on_pre_compress`, SessionDB persistence, and existing codex app-server native-compaction precedent.

## Global Constraints

- Send `context_management` only to a verified supported native endpoint/API mode. Do not pass unknown kwargs to MiniMax, Zhipu, Bedrock, Vertex, LiteLLM, Nous Portal, or other Anthropic-compatible gateways until separately verified.
- `native|hermes|off` is the user-facing policy shape, mirroring `compression.codex_app_server_auto`; `native` requires capability, `hermes` uses current local compressor, `off` disables automatic compaction and preserves current failure behavior.
- Full local history remains canonical for `/resume`, `session_search`, memory extraction, learning, provider fallback, and cross-model migration.
- Server-side clear edits are stateless request decorations; they do not mutate local messages. Opaque compaction blocks/items are provider-scoped and must not be sent to another provider.
- The local compressor must not fire redundantly on a capable native provider based only on rough full-history estimates. Defer to provider-reported usage when native edits are active.
- Preserve Anthropic signed-thinking block order and encrypted-reasoning replay invariants. Never strip/reorder blocks merely to accommodate a native edit.
- Native compaction usage, applied edits, cache reads/writes, and compaction output tokens must enter existing usage/cost accounting.
- Provider-native failures fall back to local compression once, then use existing error/fallback handling; an unknown/changed API shape must disable native mode for that provider/session rather than loop.
- Beta headers/feature flags must be capability-gated and visible in diagnostic logs without exposing request content.
- Test direct/native and incompatible gateway matrices with real request kwargs captured by transport fakes; no external provider call is required.

## Current-State Review

- No `context_management`, `clear_tool_uses`, `clear_thinking`, `compact_20260112`, or OpenAI `/responses/compact` implementation exists.
- `agent/conversation_compression.py` already routes Codex app-server to `compact_thread()` and exposes `native|hermes|off` precedent.
- `ContextEngine` and `ContextCompressor` already own `should_compress`, `update_from_response`, failure cooldown, anti-thrash, and preflight deferral contracts.
- Anthropic transport/adapter already centralize kwargs, beta headers, response normalization, and screenshot eviction; the stale computer-use docs claim server-side editing but code only performs client screenshot eviction.
- Codex Responses is stateless/replay-based; `codex_responses_adapter.py` already handles provider-specific encrypted reasoning blocks and is the correct compaction-item adapter seam.
- `memory_manager.on_pre_compress` is available for `pause_after_compaction` state flush.

The plan skips a second context engine, local transcript mutation, and provider-specific logic in `run_conversation` branches.

## Release Order

1. Capability policy and Anthropic clear edits.
2. Usage/preflight feedback and fallback behavior.
3. Anthropic opaque compaction block persistence/replay.
4. OpenAI Responses compact item support.
5. Documentation, cross-provider tests, and stale-doc correction.

## File Map

- Create: `agent/provider_context_policy.py` — capability detection and mode decisions.
- Modify: `agent/agent_runtime_helpers.py` — policy table and per-runtime capability checks.
- Modify: `agent/chat_completion_helpers.py` — request decoration/preflight suppression/fallback re-policy.
- Modify: `agent/transports/anthropic.py` — build kwargs, normalize applied edits/compaction blocks.
- Modify: `agent/anthropic_adapter.py` — `context_management` kwargs and beta headers.
- Modify: `agent/transports/codex.py` — Responses compaction request/response fields.
- Modify: `agent/codex_responses_adapter.py` — opaque compaction item normalization/replay.
- Modify: `agent/conversation_compression.py` — native compaction orchestration and fallback.
- Modify: `agent/context_engine.py`/`agent/context_compressor.py` — native-aware preflight/update contract only.
- Modify: `agent/conversation_loop.py` — usage/applied-edit feedback and no duplicate local trigger.
- Modify: `agent/memory_manager.py` — pause-after-compaction memory flush hook if not already sufficient.
- Modify: `agent/usage_pricing.py`, `agent/codex_runtime.py` — usage/cost fields.
- Modify: `hermes_cli/config.py`, `cli-config.yaml.example` — native context config.
- Modify: `website/docs/user-guide/features/computer-use.md` and related locale mirror — correct native support claim.
- Test: new `tests/agent/test_provider_context_policy.py`, `tests/agent/test_anthropic_context_management.py`, `tests/agent/test_responses_compaction.py`.
- Test: extend `tests/agent/test_anthropic_thinking_block_order.py`, codex transport/adapter/usage/compaction suites.

## Data Contracts

```python
@dataclass(frozen=True)
class ProviderContextCapability:
    mode: Literal["none", "anthropic_edits", "anthropic_compaction", "responses_compaction"]
    provider: str
    api_mode: str
    endpoint_host: str
    model: str
    beta_headers: tuple[str, ...]
    reason: str
```

```python
@dataclass(frozen=True)
class NativeContextState:
    session_id: str
    provider: str
    model: str
    mode: str
    opaque_items: tuple[dict[str, object], ...]
    last_applied_edits: tuple[str, ...]
    disabled_reason: str | None
```

`provider_native_context_policy(provider, base_url, api_mode, model, configured_mode) -> ProviderContextCapability` must return `none` for unknown gateway hosts. `build_context_management(capability, context_length, threshold_tokens) -> dict[str, object] | None` is pure and testable.

## Task 1: Capability Policy and Configuration

**Files:**
- Create: `agent/provider_context_policy.py`
- Modify: `agent/agent_runtime_helpers.py`
- Modify: `hermes_cli/config.py`
- Test: `tests/agent/test_provider_context_policy.py`

- [ ] Step 1: Add matrix tests.

```python
def test_only_native_anthropic_gets_clear_edits():
    direct = provider_native_context_policy("anthropic", "https://api.anthropic.com", "anthropic_messages", "claude-sonnet", "native")
    compatible = provider_native_context_policy("custom", "https://api.anthropic-compatible.example", "anthropic_messages", "claude-sonnet", "native")
    assert direct.mode == "anthropic_edits"
    assert compatible.mode == "none"


def test_off_and_hermes_modes_never_decorate_native_request():
    assert provider_native_context_policy("anthropic", "https://api.anthropic.com", "anthropic_messages", "claude", "off").mode == "none"
    assert provider_native_context_policy("anthropic", "https://api.anthropic.com", "anthropic_messages", "claude", "hermes").mode == "none"
```

- [ ] Step 2: Run policy tests against the current no-capability implementation.

```bash
python -m pytest tests/agent/test_provider_context_policy.py -q
```

- [ ] Step 3: Implement host/provider/api-mode/model gating. Treat OAuth/Claude-Code-auth, Bedrock, Vertex, and third-party gateways as `none` until their exact transport support is independently verified. Return a reason for diagnostics.

- [ ] Step 4: Add config defaults:

```yaml
compression:
  provider_native: hermes
  anthropic:
    edits: false
    compact: false
  openai_responses:
    compact: false
```

`provider_native` selects `native|hermes|off`; per-provider booleans are opt-in feature gates so a beta can be disabled without changing the global mode.

- [ ] Step 5: Add capability snapshot to runtime state. Recompute on primary/fallback switch and never carry Anthropic/OpenAI opaque items across providers.

- [ ] Step 6: Run policy/config tests and commit.

```bash
python -m pytest tests/agent/test_provider_context_policy.py tests/hermes_cli/test_config.py -q
 git add agent/provider_context_policy.py agent/agent_runtime_helpers.py hermes_cli/config.py tests/agent/test_provider_context_policy.py
 git diff --cached --check
git commit -m "feat(context): add native capability policy"
```

## Task 2: Anthropic Stateless Context Edits

**Files:**
- Modify: `agent/chat_completion_helpers.py`
- Modify: `agent/transports/anthropic.py`
- Modify: `agent/anthropic_adapter.py`
- Modify: `agent/context_engine.py`
- Modify: `agent/context_compressor.py`
- Modify: `agent/conversation_loop.py`
- Test: `tests/agent/test_anthropic_context_management.py`
- Test: `tests/agent/test_anthropic_thinking_block_order.py`

- [ ] Step 1: Add transport tests that inspect kwargs/headers without network.

```python
def test_anthropic_edit_kwargs_are_added_only_for_direct_native_runtime():
    capability = direct_anthropic_capability("anthropic_messages")
    kwargs = build_anthropic_kwargs(messages, context_management={"edits": [{"type": "clear_tool_uses_20250919"}]}, capability=capability)
    assert kwargs["context_management"]["edits"]
    assert "context-management-2025-06-27" in kwargs["extra_headers"]["anthropic-beta"]


def test_incompatible_gateway_does_not_receive_unknown_context_kwarg():
    kwargs = build_anthropic_kwargs(messages, context_management=None, capability=none_capability())
    assert "context_management" not in kwargs
```

- [ ] Step 2: Run tests and confirm current adapter drops the new parameter.

```bash
python -m pytest tests/agent/test_anthropic_context_management.py -q
```

- [ ] Step 3: Build `clear_tool_uses_20250919` and optional `clear_thinking_20251015` edits from context length/threshold. Keep `exclude_tools`, `clear_at_least`, and `clear_tool_inputs` configurable only through validated config; default to conservative trigger/keep-last values.

- [ ] Step 4: Pass the dict through `build_api_kwargs` → `AnthropicTransport.build_kwargs` → `build_anthropic_kwargs`; append the beta through `_common_betas_for_base_url` without adding a global header to incompatible hosts.

- [ ] Step 5: Normalize `context_management.applied_edits`/server usage into `provider_data` and `CanonicalUsage`. If the API omits the field, record no edits rather than guessing.

- [ ] Step 6: Extend `ContextEngine.update_from_response` with native-applied-edits information and suppress rough preflight on native-capable requests. The next provider-reported prompt count remains the authoritative anti-thrash input.

- [ ] Step 7: Add signed-thinking tests with clear-thinking enabled/disabled; assert exact block ordering and no accidental modification of canonical history.

```bash
python -m pytest \
  tests/agent/test_anthropic_context_management.py \
  tests/agent/test_anthropic_thinking_block_order.py \
  tests/agent/test_context_compressor.py -q
```

- [ ] Step 8: Commit stateless edits.

```bash
git add agent/chat_completion_helpers.py agent/transports/anthropic.py agent/anthropic_adapter.py agent/context_engine.py agent/context_compressor.py agent/conversation_loop.py tests/agent/test_anthropic_context_management.py tests/agent/test_anthropic_thinking_block_order.py
git diff --cached --check
git commit -m "feat(context): use Anthropic server-side edits"
```

## Task 3: Fallback, Usage, and Memory Flush Semantics

**Files:**
- Modify: `agent/conversation_compression.py`
- Modify: `agent/memory_manager.py`
- Modify: `agent/usage_pricing.py`
- Modify: `agent/chat_completion_helpers.py`
- Modify: `agent/conversation_loop.py`
- Test: new native/fallback tests and existing compression/usage suites.

- [ ] Step 1: Add tests for native request failure, local fallback once, fallback-provider switch, and native mode disablement.

```python
def test_native_context_400_disables_native_and_falls_back_to_hermes(monkeypatch):
    state = NativeContextState("s", "anthropic", "claude", "anthropic_edits", (), (), None)
    result = handle_native_context_error(state, HTTPError(400, "unknown context_management"))
    assert result.mode == "hermes"
    assert result.disabled_reason


def test_native_provider_switch_discards_opaque_state():
    assert switch_provider(native_state, "openai").opaque_items == ()
```

- [ ] Step 2: Add native-applied-edit usage fields to `CanonicalUsage`/session deltas without changing existing cost source ranking. Compaction output/iterations are billed output tokens where provider reports them.

- [ ] Step 3: For Anthropic `pause_after_compaction`, call `memory_manager.on_pre_compress`/session-memory flush before continuing, but do not run `archive_and_compact` or rewrite local messages. The local transcript remains complete.

- [ ] Step 4: Add a runtime circuit breaker: after one schema/400 failure in a session, mark native feature disabled for that capability snapshot and route to local compressor. Do not retry the same invalid kwargs across fallback entries.

- [ ] Step 5: Ensure rough preflight suppression is limited to native-capable requests. Other providers retain all four local trigger sites.

- [ ] Step 6: Run compression/usage/fallback tests.

```bash
python -m pytest \
  tests/agent/test_conversation_compression.py \
  tests/agent/test_context_compressor.py \
  tests/agent/test_usage_pricing.py \
  tests/agent/test_anthropic_context_management.py -q
```

- [ ] Step 7: Commit fallback/usage semantics.

```bash
git add agent/conversation_compression.py agent/memory_manager.py agent/usage_pricing.py agent/chat_completion_helpers.py agent/conversation_loop.py tests/agent/test_conversation_compression.py tests/agent/test_context_compressor.py tests/agent/test_usage_pricing.py
 git diff --cached --check
git commit -m "feat(context): account for native edit fallback"
```

## Task 4: Anthropic Server-Side Compaction Blocks

**Files:**
- Modify: `agent/transports/anthropic.py`
- Modify: `agent/anthropic_adapter.py`
- Modify: `agent/conversation_compression.py`
- Modify: `agent/context_engine.py`
- Modify: `hermes_state.py` only if provider-state persistence is necessary; prefer session runtime state.
- Test: `tests/agent/test_anthropic_context_management.py`

- [ ] Step 1: Add fixtures for a response with `stop_reason="compaction"` and an opaque `compaction` content block. Assert canonical local messages are unchanged and runtime state captures the block.

```python
def test_anthropic_compaction_block_is_provider_scoped():
    response = anthropic_compaction_response("opaque-1")
    normalized = AnthropicTransport().normalize_response(response)
    assert normalized.provider_data["compaction_block"]["id"] == "opaque-1"
    assert normalized.messages == response.messages
```

- [ ] Step 2: Add opt-in compact request configuration with trigger threshold ≥ provider minimum and `pause_after_compaction`. Use custom instructions only from a static validated config string; do not inject untrusted event/memory text into it.

- [ ] Step 3: On compaction stop, persist the provider-scoped block in runtime state and continue with a fresh API call carrying the block plus new user/tool messages. The block is not written to canonical SessionDB as a normal assistant message; store a redacted diagnostic/reference if persistence is required.

- [ ] Step 4: On provider switch/resume without matching provider/model capability, discard the block and reconstruct from canonical messages using local compression/native capability of the new provider.

- [ ] Step 5: Add tests for multi-iteration compaction, pause/resume, failure after block, and cross-provider fallback.

```bash
python -m pytest tests/agent/test_anthropic_context_management.py tests/agent/test_provider_failover.py -q
```

- [ ] Step 6: Commit Anthropic compact support.

```bash
git add agent/transports/anthropic.py agent/anthropic_adapter.py agent/conversation_compression.py agent/context_engine.py hermes_state.py tests/agent/test_anthropic_context_management.py
 git diff --cached --check
git commit -m "feat(context): support Anthropic compaction blocks"
```

## Task 5: OpenAI Responses Compaction Items

**Files:**
- Modify: `agent/transports/codex.py`
- Modify: `agent/codex_responses_adapter.py`
- Modify: `agent/chat_completion_helpers.py`
- Modify: `agent/conversation_compression.py`
- Modify: `agent/codex_runtime.py`
- Test: new `tests/agent/test_responses_compaction.py`
- Test: existing Codex Responses adapter/transport/usage suites.

- [ ] Step 1: Add response/request fixtures for `context_management.compact_threshold`, `/responses/compact`, opaque compaction items, and unsupported endpoints.

```python
def test_responses_compaction_item_round_trips_without_cross_provider_replay():
    item = {"type": "compaction", "encrypted_content": "opaque"}
    normalized = normalize_responses_output({"output": [item]})
    assert normalized.provider_data["compaction_items"] == [item]
    assert responses_input_for_provider(normalized, provider="openai")[0] == item
    assert responses_input_for_provider(normalized, provider="anthropic") == []
```

- [ ] Step 2: Add a capability gate for the verified Responses endpoint/api mode only; do not send compaction kwargs to standard chat completions or custom OpenAI-compatible gateways.

- [ ] Step 3: Implement threshold decoration and standalone compact call through the existing Codex Responses transport. Keep local messages canonical and store opaque items in `NativeContextState`.

- [ ] Step 4: Attribute compaction usage/cost and expose a status diagnostic. A compaction item that cannot be replayed causes provider-native disablement and local fallback, not repeated compaction calls.

- [ ] Step 5: Run Codex transport/adapter/compaction tests.

```bash
python -m pytest \
  tests/agent/test_responses_compaction.py \
  tests/agent/transports/test_codex_responses_adapter.py \
  tests/agent/test_codex_app_server_compaction.py \
  tests/agent/test_usage_pricing.py -q
```

- [ ] Step 6: Commit Responses support.

```bash
git add agent/transports/codex.py agent/codex_responses_adapter.py agent/chat_completion_helpers.py agent/conversation_compression.py agent/codex_runtime.py tests/agent/test_responses_compaction.py tests/agent/transports/test_codex_responses_adapter.py
 git diff --cached --check
git commit -m "feat(context): support Responses compaction items"
```

## Task 6: Documentation and Cross-Provider Verification

**Files:**
- Modify: `cli-config.yaml.example`
- Modify: `website/docs/user-guide/features/computer-use.md` and its locale mirror.
- Modify: context/compression user guide if present.
- Test: new matrix/e2e tests and existing compression/provider suites.

- [ ] Update the stale computer-use documentation to say native server-side editing is opt-in, direct-Anthropic-only in Phase 1, and otherwise screenshot/tool eviction is local.
- [ ] Document `native|hermes|off`, provider gates, beta behavior, canonical local history, cross-provider fallback, usage/cost reporting, and failure disablement.
- [ ] Run a fake-provider matrix: direct Anthropic edits, direct Anthropic compact, OpenAI Responses compact, third-party Anthropic-compatible gateway, standard chat completions, Codex app-server native, and local fallback.
- [ ] Run a signed-thinking/block-order regression and assert no canonical transcript mutation.
- [ ] Run a long-turn fake stream where native applied edits reduce provider-reported input tokens; verify local compressor does not fire a duplicate preflight.
- [ ] Run a native 400/schema failure and assert one fallback, disabled capability, and no retry loop.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/agent/test_provider_context_policy.py \
  tests/agent/test_anthropic_context_management.py \
  tests/agent/test_anthropic_thinking_block_order.py \
  tests/agent/test_responses_compaction.py \
  tests/agent/test_conversation_compression.py \
  tests/agent/test_context_compressor.py \
  tests/agent/test_usage_pricing.py \
  tests/agent/transports/test_codex_responses_adapter.py -q
python3 -m compileall -q agent/provider_context_policy.py agent/transports/anthropic.py agent/transports/codex.py
 git diff --check
```

- [ ] Commit final documentation/evidence.

```bash
git add cli-config.yaml.example website/docs/user-guide/features/computer-use.md docs tests/agent
git diff --cached --check
git commit -m "docs(context): document provider-native compaction"
```

## Acceptance Checklist

- [ ] Native kwargs/header are gated to known compatible endpoints and modes.
- [ ] Anthropic clear edits are request decorations with applied-edit usage feedback.
- [ ] Native capability suppresses duplicate rough preflight while local fallback remains intact.
- [ ] Anthropic compaction blocks and OpenAI Responses items are provider-scoped opaque state.
- [ ] Canonical SessionDB history remains complete and portable.
- [ ] Memory flush/continuation behavior is safe at pause-after-compaction.
- [ ] Usage/cost reporting includes native edit/compaction fields.
- [ ] Provider-native failure disables the feature for the session and falls back once.
- [ ] Signed-thinking, fallback, cache, and cross-provider tests pass.
- [ ] Stale documentation no longer claims unsupported server-side editing.

## Deliberate Simplifications

- Skipped third-party gateway rollout in the first phase; unknown kwargs are worse than a missing optimization.
- Skipped provider-agnostic opaque compaction storage; those items are encrypted/model-specific and cannot be portable.
- Skipped deleting the 3,200-line local compressor; it remains the cross-provider fallback until measured adoption proves otherwise.
- Skipped automatic native-mode enablement; beta APIs require explicit opt-in and a clear escape hatch.
