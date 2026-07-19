# Hermes Auto Routing Foundation and Read-Only Advisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an opt-in, profile-local Auto Routing plugin that can safely inventory executable runtimes, preserve catalog provenance, propose and validate named route profiles through chat/CLI, and atomically apply approved configuration without changing runtime model selection.

**Architecture:** A thin plugin registration shim composes a Hermes-independent Pydantic domain, profile-local SQLite store, inventory/catalog adapters, read-only advisor, and native CLI. User YAML is immutable authority; the database stores observations and revisions. This stage installs no runtime routing wrapper and leaves activation at `shadow`, making it a useful advisor with zero change to ordinary agent calls.

**Tech Stack:** Python `>=3.11,<3.14`, Pydantic `2.13.4`, SQLite/WAL, `ruamel.yaml 0.18.17`, `psutil 7.2.2`, `packaging 26.0`, Hermes `PluginContext`/`PluginLlm`, and pytest `9.0.2`.

## Global Constraints

- The plugin lives under `plugins/auto_routing` and uses relative imports so it can later move to a standalone repository.
- Installation/enabling alone never changes a model choice. Stage 1 accepts only `off` or `shadow`; attempting `active` fails validation until the Stage 2 adapter reports healthy.
- Keep MoA out of inventory and all recommendations.
- Only `verified` runtimes may appear as a proposed primary or fallback. Other states remain visible with a precise reason.
- A model accessed through different provider/auth/API-mode paths is a distinct `RuntimeKey`; do not collapse subscription and metered access.
- Do not store or print API keys, OAuth tokens, credential-pool contents, or raw custom-endpoint credentials.
- Objective policy is explicit: every profile declares quality, reliability, latency, and cost weights; omitted dimensions are validation errors, not defaults.
- `max_estimated_task_cost_usd` and `max_estimated_latency_seconds` are selection gates, not execution guarantees.
- `allowed_licenses: []` means no extra allowlist; open-weight and hardware checks still apply to local models.
- Catalog evidence never grants eligibility. It retains source URL, retrieval/publication dates, version, domain, metric direction/scale, sample/confidence, and normalization method.
- No automatic model download or paid access probe, outbound telemetry, analytics identifier, or task content in catalog requests. A paid access check may occur only through the explicit `verify-runtime` preview/apply flow while `policy.allow_paid_access_probes` is true; it is never an advisor, routing, or optimizer side effect.
- YAML/control-plane CLI operations preview by default and require `--apply --expected-config-sha SHA256_FROM_PREVIEW` under a cross-process lock with backup and atomic replacement. The inventory-state-only but billable/quota-consuming `verify-runtime` operation requires its own `--apply --expect-hash PREVIEW_HASH --ack-billable` precondition. Non-billable `inventory --refresh` and catalog refresh are labeled `append_only_observation`: they may append deduplicated content-free observations transactionally without preview, but cannot change authority, activation, an active revision, or a projected route.
- The explicit-load advisor skill is `auto-routing:auto-routing`; no model-visible core tool or plugin slash-command injection is added.
- Real-path tests use temporary `HERMES_HOME`; mocks stop at live network/provider boundaries.
- Each task finishes with focused tests, relevant regressions, `git diff --check`, and one conventional commit.

---

## File Map

### New plugin files

- `plugins/auto_routing/plugin.yaml` — opt-in standalone manifest and observer-hook metadata.
- `plugins/auto_routing/__init__.py` — construct one `AutoRoutingService`, register CLI and explicit skill; no routing patch.
- `plugins/auto_routing/README.md` — enablement, shadow-only Stage 1 behavior, privacy, and extraction boundary.
- `plugins/auto_routing/skills/auto-routing/SKILL.md` — conversational setup/edit interview using native CLI JSON and preview/apply flow.
- `plugins/auto_routing/auto_routing/__init__.py` — package version and stable public exports.
- `plugins/auto_routing/auto_routing/models.py` — immutable Pydantic domain/config records.
- `plugins/auto_routing/auto_routing/config.py` — plugin-subtree parsing, cross-field validation, normalization, authority hash.
- `plugins/auto_routing/auto_routing/config_io.py` — round-trip YAML preview, lock, precondition, backup, atomic apply.
- `plugins/auto_routing/auto_routing/storage.py` — profile-local SQLite connections, declarative schema, migrations, revisions, budgets.
- `plugins/auto_routing/auto_routing/inventory.py` — four-state executable inventory over a Hermes adapter protocol.
- `plugins/auto_routing/auto_routing/catalog.py` — normalized catalog records and Hermes/models.dev/file sources.
- `plugins/auto_routing/auto_routing/scoring.py` — pure normalized objective utility and access-economics functions shared by advisor/runtime selection.
- `plugins/auto_routing/auto_routing/advisor.py` — profile proposal, ranking explanation, representative-prompt dry run, validation.
- `plugins/auto_routing/auto_routing/cli.py` — Stage 1 `hermes auto-routing` parser/handlers.
- `plugins/auto_routing/auto_routing/service.py` — composition root used by CLI and later runtime adapter.
- `plugins/auto_routing/auto_routing/adapters/__init__.py` — adapter exports.
- `plugins/auto_routing/auto_routing/adapters/base.py` — `HermesAdapter` and clock/network protocols.
- `plugins/auto_routing/auto_routing/adapters/hermes_0_18.py` — read-only inventory/runtime-resolution adapter for the current Hermes 0.18.2 fork.

### Generic Hermes files modified

- `agent/plugin_llm.py:683-1008` — add a trust-gated `reasoning_config` pass-through to all four public completion methods and both invocation paths.
- `tests/agent/test_plugin_llm.py:536-760` — prove reasoning forwarding without weakening provider/model trust.
- `agent/reasoning_support.py` — content-free exact generic-effort resolver shared by inventory and request-translation metadata.
- `agent/models_dev.py` and `hermes_cli/inventory.py` — retain/surface provider-scoped `reasoning_options` instead of collapsing them to a boolean.
- `hermes_cli/models.py` and `hermes_cli/model_switch.py` — preserve per-model authenticated-live/contract/static/configured discovery provenance through caches and provider rows while keeping legacy picker lists compatible.
- `tests/agent/test_reasoning_support.py` — prove exact provider/model options, aliases/clamps, and fail-closed unknown support.
- `tests/hermes_cli/test_provider_live_curated_merge.py` — prove a static or stale fallback remains visible but never masquerades as a successful authenticated listing.
- `pyproject.toml:338-354` — include plugin skill Markdown in wheel package data.
- `MANIFEST.in:9` — include plugin skill Markdown in sdist.
- `tests/test_packaging_metadata.py:113-156` — prove wheel/sdist globs retain plugin skills.

### New tests

- `tests/plugins/auto_routing/conftest.py`
- `tests/plugins/auto_routing/test_plugin_registration.py`
- `tests/plugins/auto_routing/test_models_config.py`
- `tests/plugins/auto_routing/test_config_io.py`
- `tests/plugins/auto_routing/test_storage.py`
- `tests/plugins/auto_routing/test_budget_ledger.py`
- `tests/plugins/auto_routing/test_storage_concurrency.py`
- `tests/plugins/auto_routing/test_profile_isolation.py`
- `tests/agent/test_reasoning_support.py`
- `tests/plugins/auto_routing/test_inventory.py`
- `tests/plugins/auto_routing/test_catalog.py`
- `tests/plugins/auto_routing/test_advisor_cli.py`
- `tests/plugins/auto_routing/test_foundation_e2e.py`

---

### Task 1: Add Generic Plugin-LLM Reasoning and Package Plugin Skills

**Files:**
- Modify: `agent/plugin_llm.py:683-1008`
- Modify: `tests/agent/test_plugin_llm.py:536-760`
- Modify: `pyproject.toml:338-354`
- Modify: `MANIFEST.in:9`
- Modify: `tests/test_packaging_metadata.py:113-156`

**Interfaces:**
- Consumes: `agent.auxiliary_client.call_llm(..., reasoning_config: Optional[dict])`.
- Produces: `PluginLlm.complete`, `complete_structured`, `acomplete`, and `acomplete_structured` accepting `reasoning_config: Optional[Dict[str, Any]] = None` and forwarding a defensive copy.

- [ ] **Step 1: Write failing facade and packaging tests**

```python
def test_complete_structured_forwards_reasoning_config(self):
    captured = {}
    requested = {"effort": "low"}

    def fake_caller(**kwargs):
        captured.update(kwargs)
        return "openai-codex", "gpt-5.4", _fake_response('{"complexity": 0.5}')

    llm = make_plugin_llm_for_test(
        plugin_id="auto-routing",
        policy=_TrustPolicy(plugin_id="auto-routing"),
        sync_caller=fake_caller,
    )
    llm.complete_structured(
        instructions="Classify",
        input=[PluginLlmTextInput(text="fix a flaky test")],
        json_mode=True,
        reasoning_config=requested,
    )
    assert captured["reasoning_config"] == {"effort": "low"}
    assert captured["reasoning_config"] is not requested


@pytest.mark.parametrize(
    "method_name",
    ["complete", "complete_structured", "acomplete", "acomplete_structured"],
)
def test_every_public_facade_method_accepts_reasoning_config(self, method_name):
    parameter = inspect.signature(getattr(PluginLlm, method_name)).parameters[
        "reasoning_config"
    ]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None
    assert PLUGIN_LLM_REASONING_CONTRACT_VERSION == 1


def test_distribution_metadata_includes_plugin_skills():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = data["tool"]["setuptools"]["package-data"]
    assert "**/skills/*/SKILL.md" in package_data["plugins"]
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include plugins SKILL.md" in manifest
```

Place the first test in the existing `TestPluginLlmFacade` class so it reuses
that file's `_fake_response`, `_TrustPolicy`, and `make_plugin_llm_for_test`
helpers, and add `import inspect` beside that file's standard-library imports.
Place the packaging test beside the existing bundled-plugin packaging test,
which already defines `tomllib` and `REPO_ROOT`.

- [ ] **Step 2: Run RED**

Run:

```bash
uv run --extra dev python -m pytest -q \
  tests/agent/test_plugin_llm.py::TestPluginLlmFacade::test_complete_structured_forwards_reasoning_config \
  tests/agent/test_plugin_llm.py::TestPluginLlmFacade::test_every_public_facade_method_accepts_reasoning_config \
  tests/test_packaging_metadata.py::test_distribution_metadata_includes_plugin_skills
```

Expected: the first test fails with an unexpected keyword argument and the second fails because the skill globs are absent.

- [ ] **Step 3: Extend the existing facade without a new trust bypass**

Define `PLUGIN_LLM_REASONING_CONTRACT_VERSION = 1`. Add the same keyword-only parameter to all sync/async public methods and `_invoke_sync`/`_invoke_async`, then forward it only to the already trusted `call_llm` path:

```python
reasoning_config: Optional[Dict[str, Any]] = None

# Pass a defensive copy from every public method into _invoke_sync/_invoke_async.
# Inside the injected-caller branch of each private method:
return self._sync_caller(
    messages=messages,
    provider_override=provider_override,
    model_override=model_override,
    profile_override=profile_override,
    temperature=temperature,
    max_tokens=max_tokens,
    timeout=timeout,
    extra_body=extra_body,
    reasoning_config=dict(reasoning_config) if reasoning_config else None,
)

# Inside the existing host-owned call path, preserve merged_extra:
response = call_llm(
    task=None,
    provider=provider,
    model=model,
    messages=messages,
    temperature=temperature,
    max_tokens=max_tokens,
    timeout=timeout,
    extra_body=merged_extra or None,
    reasoning_config=dict(reasoning_config) if reasoning_config else None,
)
```

Apply the identical keyword/copy to the awaited injected caller and
`async_call_llm` branch. Do not remove `extra_body`, auth-profile metadata, or
any existing attribution/audit field.

Add `"**/skills/*/SKILL.md"` to the existing `plugins` wheel data and `recursive-include plugins SKILL.md` to `MANIFEST.in`. Do not add a dependency or relax `_check_overrides`.

- [ ] **Step 4: Run GREEN and trust regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/agent/test_plugin_llm.py \
  tests/test_packaging_metadata.py
git diff --check
```

Expected: all tests pass and the diff check is silent.

- [ ] **Step 5: Commit**

```bash
git add agent/plugin_llm.py tests/agent/test_plugin_llm.py pyproject.toml MANIFEST.in tests/test_packaging_metadata.py
git diff --cached --check
git commit -m "feat: support reasoning in plugin llm calls"
```

---

### Task 2: Scaffold the Opt-In Plugin and Explicit Advisor Skill

**Files:**
- Create: `plugins/auto_routing/plugin.yaml`
- Create: `plugins/auto_routing/__init__.py`
- Create: `plugins/auto_routing/README.md`
- Create: `plugins/auto_routing/skills/auto-routing/SKILL.md`
- Create: `plugins/auto_routing/auto_routing/__init__.py`
- Create: `plugins/auto_routing/auto_routing/service.py`
- Create: `plugins/auto_routing/auto_routing/cli.py`
- Create: `tests/plugins/auto_routing/conftest.py`
- Create: `tests/plugins/auto_routing/test_plugin_registration.py`

**Interfaces:**
- Consumes: `PluginContext.register_cli_command()` and `PluginContext.register_skill()`.
- Produces: `register(ctx)`, `build_parser(parser)`, `auto_routing_command(args) -> int`, and `AutoRoutingService.from_plugin_context(ctx)`.

- [ ] **Step 1: Write failing discovery/registration tests**

```python
def test_auto_routing_plugin_registers_cli_and_explicit_skill(plugin_context):
    module = load_bundled_plugin("auto_routing")
    module.register(plugin_context)
    assert plugin_context.cli_commands == ["auto-routing"]
    assert plugin_context.skills == ["auto-routing:auto-routing"]
    assert plugin_context.tools == []
    assert plugin_context.middleware == []


def test_stage_one_status_is_shadow_only(service, capsys):
    assert service.status()["runtime_projection"] == "not_installed"
    assert service.status()["activation_mode"] in {"off", "shadow"}
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_plugin_registration.py
```

Expected: collection fails because `plugins/auto_routing` does not exist.

- [ ] **Step 3: Add the registration-only shell**

Use this manifest and registration shape:

```yaml
name: auto-routing
version: "0.1.0"
description: "Profile-local executable-inventory advisor and cache-safe Auto model router"
author: "Hermes fork contributors"
kind: standalone
hooks: []
```

```python
from pathlib import Path

from .auto_routing.cli import auto_routing_command, build_parser
from .auto_routing.service import AutoRoutingService


def register(ctx) -> None:
    service = AutoRoutingService.from_plugin_context(ctx)
    ctx.register_cli_command(
        name="auto-routing",
        help="Configure and inspect automatic model routing",
        setup_fn=build_parser,
        handler_fn=lambda args: auto_routing_command(args, service=service),
        description="Executable inventory, profile advice, validation, and routing history",
    )
    ctx.register_skill(
        "auto-routing",
        Path(__file__).parent / "skills" / "auto-routing" / "SKILL.md",
        description="Create or edit Auto Routing profiles through a validated CLI proposal",
    )
```

The skill must instruct Hermes to run `hermes auto-routing inventory --json`, interview only during setup/edit, require all four objectives, call `plan`, show `preview`, and run `setup --apply` only after explicit approval. It must state that runtime routing is silent and that unverified/local-uninstalled candidates are not recommendations.

Create `tests/plugins/auto_routing/conftest.py` with these shared, deterministic
fixtures: `isolated_home` sets `HERMES_HOME` to `tmp_path / "profile"` and clears
Hermes path/config caches; `mutable_clock` exposes `now()`, `today()`, and
`advance(seconds=...)`; `plugin_context` is a real `PluginContext`-compatible
recorder for CLI commands, skills, tools, middleware, and hooks;
`load_bundled_plugin(name)` loads `plugins/<name>/__init__.py` through the same
`hermes_plugins.<slug>` module name used by the plugin loader; and `service`
constructs `AutoRoutingService` with that context and isolated home. Later
tasks extend this file only with low-level builders shared by three or more
test modules; task-specific fixtures remain in their own test file.

- [ ] **Step 4: Run GREEN and plugin regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_plugin_registration.py \
  tests/hermes_cli/test_plugin_cli_registration.py \
  tests/test_plugin_skills.py
git diff --check
```

Expected: all tests pass; registration adds no tool or middleware.

- [ ] **Step 5: Commit**

```bash
git add plugins/auto_routing tests/plugins/auto_routing/conftest.py tests/plugins/auto_routing/test_plugin_registration.py
git diff --cached --check
git commit -m "feat: scaffold auto routing plugin"
```

---

### Task 3: Define Immutable Domain and Authority Validation

**Files:**
- Create: `plugins/auto_routing/auto_routing/models.py`
- Create: `plugins/auto_routing/auto_routing/config.py`
- Create: `tests/plugins/auto_routing/test_models_config.py`

**Interfaces:**
- Consumes: the `plugins.entries.auto-routing` mapping from `load_config_readonly()`.
- Produces: `AutoRoutingConfig`, `PolicyEnvelope`, `RuntimeKey`, `AccessEconomics`, `RoutingTarget`, `RouteProfile`, `TaskAssessment`, `RoutingDecision`, `RuntimeObservation`, `CatalogEvidence`, `AdaptiveRevision`, `parse_config(root)`, and `authority_revision(config)`.

- [ ] **Step 1: Write failing schema and invariant tests**

```python
def test_profile_requires_complete_objectives_and_verified_target(valid_root):
    parsed = parse_config(valid_root)
    assert parsed.profiles["coding"].objectives.model_dump() == {
        "quality": 0.55, "reliability": 0.25, "latency": 0.10, "cost": 0.10
    }
    assert parsed.profiles["coding"].primary.reasoning.minimum == "low"


@pytest.mark.parametrize("missing", ["quality", "reliability", "latency", "cost"])
def test_missing_objective_is_invalid(valid_root, missing):
    del valid_root["plugins"]["entries"]["auto-routing"]["profiles"]["coding"]["objectives"][missing]
    with pytest.raises(ConfigError, match=missing):
        parse_config(valid_root)


def test_authority_hash_ignores_inventory_derived_target_state(valid_root):
    parsed = parse_config(valid_root)
    coding = parsed.profiles["coding"]
    observed_primary = coding.primary.model_copy(update={
        "supported_reasoning_efforts": ("low", "medium", "high")
    })
    with_observation = parsed.model_copy(update={
        "profiles": {
            **parsed.profiles,
            "coding": coding.model_copy(update={"primary": observed_primary}),
        }
    })
    first = authority_revision(parsed)
    second = authority_revision(with_observation)
    assert first == second
    assert len(first) == 64


def test_runtime_key_distinguishes_pool_and_local_backend_paths() -> None:
    base = RuntimeKey(
        provider="custom",
        model="qwen3:14b",
        auth_identity="configured:work",
        credential_pool_identity="pool:work",
        endpoint_identity="endpoint:local",
        api_mode="chat_completions",
        local_backend="ollama",
        inventory_revision="inv-1",
    )
    assert base.stable_id() != base.model_copy(update={
        "credential_pool_identity": "pool:personal"
    }).stable_id()
    assert base.stable_id() != base.model_copy(update={
        "local_backend": "lmstudio"
    }).stable_id()
    assert base.stable_id() == base.model_copy(update={
        "inventory_revision": "inv-2"
    }).stable_id()
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_models_config.py
```

Expected: import failure for the missing models/config modules.

- [ ] **Step 3: Implement the stable records and validators**

Define frozen Pydantic models with these exact identity fields and enums; add
the required `hashlib`, `json`, and `math` standard-library imports:

```python
ActivationMode = Literal["off", "shadow", "active"]
InventoryState = Literal[
    "verified", "configured_unverified", "temporarily_unavailable", "ineligible"
]
ReasoningEffort = Literal[
    "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"
]


class RuntimeKey(BaseModel):
    model_config = ConfigDict(frozen=True)
    provider: str
    model: str
    auth_identity: str
    credential_pool_identity: str = ""
    endpoint_identity: str = ""
    api_mode: str
    local_backend: str = ""
    inventory_revision: str

    def stable_id(self) -> str:
        payload = json.dumps(
            self.model_dump(exclude={"inventory_revision"}),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class ObjectiveWeights(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    quality: float
    reliability: float
    latency: float
    cost: float

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        keys = ("quality", "reliability", "latency", "cost")
        missing = [key for key in keys if key not in data]
        if missing:
            raise ValueError(f"missing objective weights: {', '.join(missing)}")
        values = [float(data[key]) for key in keys]
        if any(not math.isfinite(v) or v < 0 for v in values) or sum(values) <= 0:
            raise ValueError("objective weights must be finite, non-negative, and sum above zero")
        total = sum(values)
        return {
            **data,
            **{
                key: float(data[key]) / total
                for key in keys
            },
        }
```

`auth_identity` names the non-secret authentication mode/profile (for example,
subscription versus metered API credentials); `credential_pool_identity` names
the exact addressable rotating pool within that path and is empty for a direct
non-pooled credential. `local_backend` distinguishes installed backend
instances even when provider/model/endpoint fields otherwise coincide. All
three identity dimensions participate in `stable_id()`; only
`inventory_revision` is excluded.

`AccessEconomics` separates `billing_kind` (`metered`, `subscription`, or `local`), metered input/output prices, user-configured or observed effective marginal/amortized per-task cost, subscription plan/quota/reset state, local energy/compute estimate, observed throttle/cooldown, source/provenance, confidence, and observation time. Unknown optional numbers remain `None`, never zero by coercion. It contains no plan credential or account identifier. `RuntimeObservation` carries one economics record per full access path.

`PolicyEnvelope` must include every immutable policy field from the approved spec. `RoutingTarget` contains the requested runtime, reasoning default/minimum/maximum, and an inventory-derived `supported_reasoning_efforts` tuple; that tuple is observational and is excluded from the authority hash. Config may contain user-owned non-secret economics overrides keyed by runtime stable ID, but public metered pricing may never overwrite a subscription/local path's effective cost. `AutoRoutingConfig` must reject Stage 1 `active`, reject per-profile limits that loosen global limits, reject `canary_fraction > max_canary_fraction`, reject reasoning bounds out of the canonical order shown above, and treat an empty license list as no additional allowlist. `AdaptiveRevision` contains `revision_id`, `authority_id`, optional `parent_revision_id`, a complete overlay, canonical explanation, `created_at`, and `is_baseline`. `authority_revision` serializes only user-owned authority with sorted keys and SHA-256.

Define `valid_root` in this test file as the full minimal authority mapping:
shadow activation; fresh/delegation scopes; compliant inherit safe default; all
immutable policy fields; one `coding` profile with the objective values shown
above; and one target with low/medium/high reasoning bounds. It contains no
credentials and is deep-copied for every test.

- [ ] **Step 4: Run GREEN and static checks**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_models_config.py
uv run --extra dev ruff check plugins/auto_routing/auto_routing/models.py plugins/auto_routing/auto_routing/config.py
git diff --check
```

Expected: all tests and Ruff pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/auto_routing/auto_routing/models.py plugins/auto_routing/auto_routing/config.py tests/plugins/auto_routing/test_models_config.py
git diff --cached --check
git commit -m "feat: validate auto routing authority"
```

---

### Task 4: Implement Safe Config Preview and Atomic Apply

**Files:**
- Create: `plugins/auto_routing/auto_routing/config_io.py`
- Create: `tests/plugins/auto_routing/test_config_io.py`

**Interfaces:**
- Consumes: `parse_config()`, `hermes_constants.get_config_path()`, `require_readable_config_before_write()`, and `utils.atomic_roundtrip_yaml_update()`.
- Produces: `preview_update(proposal, path=None) -> ConfigPreview` and `apply_update(proposal, expected_precondition_sha256, path=None) -> AppliedConfig`.

- [ ] **Step 1: Write failing preservation/conflict/backup tests**

```python
def test_apply_preserves_unrelated_yaml_and_requires_exact_hash(config_path, valid_subtree):
    config_path.write_text("# keep me\ndisplay:\n  skin: kawaii\n", encoding="utf-8")
    preview = preview_update(valid_subtree, path=config_path)
    with pytest.raises(ConfigConflict):
        apply_update(
            valid_subtree,
            expected_precondition_sha256="0" * 64,
            path=config_path,
        )

    changed_proposal = valid_subtree.model_copy(update={"activation": {"mode": "off"}})
    with pytest.raises(ConfigConflict):
        apply_update(
            changed_proposal,
            expected_precondition_sha256=preview.precondition_sha256,
            path=config_path,
        )
    assert config_path.read_bytes() == preview.before_bytes

    result = apply_update(
        valid_subtree,
        expected_precondition_sha256=preview.precondition_sha256,
        path=config_path,
    )
    text = config_path.read_text(encoding="utf-8")
    assert "# keep me" in text and "skin: kawaii" in text
    assert result.backup_path.read_bytes() == preview.before_bytes


def test_managed_plugin_subtree_fails_closed(config_path, valid_subtree, monkeypatch):
    monkeypatch.setattr("hermes_cli.managed_scope.managed_config_keys", lambda: {"plugins.entries.auto-routing.policy"})
    with pytest.raises(ManagedConfigError):
        preview_update(valid_subtree, path=config_path)
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_config_io.py
```

Expected: import failure for `config_io`.

- [ ] **Step 3: Implement cross-process guarded replacement**

Create a platform-specific lock using `msvcrt.locking` on Windows and `fcntl.flock` on POSIX. Under the lock: read exact bytes, compute SHA-256, revalidate the proposed subtree, reject managed keys, compute `timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")`, create `config_path.with_name(f"{config_path.name}.auto-routing.{timestamp}.bak")`, and call:

```python
atomic_roundtrip_yaml_update(
    config_path,
    "plugins.entries.auto-routing",
    proposal.model_dump(mode="json", by_alias=True, exclude_none=True),
)
```

`preview_update` returns exact before/after bytes, unified diff, `before_sha256`, `after_sha256`, `authority_revision`, the backup filename pattern, and `precondition_sha256` without writing. The precondition hash is SHA-256 over canonical command/path, before and after hashes, normalized proposed subtree, and authority ID; it excludes time/randomness. The CLI's historical `--expected-config-sha` flag carries this full precondition hash—not merely the current file hash. `apply_update` recomputes the complete preview after taking the lock and rejects any changed source bytes or proposal before writing, then creates the timestamped backup and verifies the on-disk parse. A failed verification restores the backup atomically.

- [ ] **Step 4: Run GREEN and interruption regression**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_config_io.py
git diff --check
```

Expected: all tests pass, including simulated replacement failure restoring the original bytes.

- [ ] **Step 5: Commit**

```bash
git add plugins/auto_routing/auto_routing/config_io.py tests/plugins/auto_routing/test_config_io.py
git diff --cached --check
git commit -m "feat: apply auto routing config atomically"
```

---

### Task 5: Add Profile-Local SQLite State, Revisions, and Budget Reservations

**Files:**
- Create: `plugins/auto_routing/auto_routing/storage.py`
- Create: `tests/plugins/auto_routing/test_storage.py`
- Create: `tests/plugins/auto_routing/test_budget_ledger.py`
- Create: `tests/plugins/auto_routing/test_storage_concurrency.py`
- Create: `tests/plugins/auto_routing/test_profile_isolation.py`

**Interfaces:**
- Consumes: `get_hermes_home()`, `hermes_state.apply_wal_with_fallback()`, and `hermes_cli.sqlite_util.add_column_if_missing()`.
- Produces: `RoutingStore`, `connect()`, `init_db()`, authority/catalog/inventory snapshot writers, immutable revision readers, and `reserve_budget()/reconcile_budget()`.

- [ ] **Step 1: Write failing real-SQLite tests**

```python
def test_store_is_profile_local_wal_and_revision_atomic(isolated_home):
    store = RoutingStore.open()
    assert store.path == isolated_home / "auto-routing" / "state.db"
    assert store.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() in {"wal", "delete"}
    revision = store.build_baseline_revision(authority_id="a1", overlay={"profiles": {}})
    store.publish_revision(revision, expected_active_id=None)
    assert store.read_active_revision("a1").revision_id == revision.revision_id


def test_budget_reservation_is_atomic_and_reconciled(store, mutable_clock):
    reservation = store.reserve_budget("classifier", worst_case_usd=0.20, daily_limit_usd=1.00, now=mutable_clock.now())
    store.reconcile_budget(reservation.reservation_id, actual_usd=0.07)
    assert store.daily_budget("classifier", mutable_clock.today()).spent_usd == pytest.approx(0.07)


def test_second_connection_gets_bounded_busy_failure(store_factory):
    first = store_factory()
    second = store_factory()
    with first.write_txn():
        with pytest.raises(StoreBusy):
            second.reserve_budget(
                "classifier", worst_case_usd=0.10, daily_limit_usd=1.00, now=0.0
            )


def test_profiles_use_distinct_state_databases(profile_home_factory):
    default_store = RoutingStore.open(home=profile_home_factory("default"))
    work_store = RoutingStore.open(home=profile_home_factory("work"))
    default_store.write_authority_revision("auth-default", {"profiles": {}})
    assert work_store.read_authority_revision("auth-default") is None
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_storage.py \
  tests/plugins/auto_routing/test_budget_ledger.py \
  tests/plugins/auto_routing/test_storage_concurrency.py \
  tests/plugins/auto_routing/test_profile_isolation.py
```

Expected: import failure for `storage` or missing persistence/concurrency APIs.

- [ ] **Step 3: Implement declarative schema and bounded writes**

Create `schema_meta`, `authority_revisions`, `inventory_snapshots`, `inventory_observations`, `catalog_snapshots`, `catalog_evidence`, `adaptive_revisions`, `active_adaptive_revisions`, `routing_decisions`, `decision_candidates`, `route_epochs`, and `budget_ledger`. Use `CREATE TABLE/INDEX IF NOT EXISTS`, foreign keys, `busy_timeout=5000`, `isolation_level=None`, and `BEGIN IMMEDIATE` for pointer changes/reservations. Retry locked writes up to 15 times with 20–150 ms jitter, then return the last complete read revision or raise `StoreBusy` to mutating CLI code.

Use immutable canonical JSON blobs plus query columns; never place prompt/response/key/token columns in schema. The revision tables introduced here are the single schema retained by later plans:

```sql
CREATE TABLE IF NOT EXISTS adaptive_revisions (
    revision_id TEXT PRIMARY KEY,
    authority_id TEXT NOT NULL,
    parent_revision_id TEXT,
    document_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS active_adaptive_revisions (
    authority_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES adaptive_revisions(revision_id),
    updated_at TEXT NOT NULL
);
```

Revision publication is a compare-and-swap:

```python
def publish_revision(
    self, revision: AdaptiveRevision, expected_active_id: str | None
) -> None:
    with self.write_txn() as conn:
        current = self._active_id(conn, revision.authority_id)
        if current != expected_active_id:
            raise RevisionConflict(expected_active_id, current)
        document = revision.canonical_json()
        checksum = hashlib.sha256(document.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO adaptive_revisions "
            "(revision_id, authority_id, parent_revision_id, document_json, checksum, explanation_json, created_at, complete) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (
                revision.revision_id,
                revision.authority_id,
                revision.parent_revision_id,
                document,
                checksum,
                revision.canonical_explanation_json(),
                revision.created_at,
            ),
        )
        stored_document, stored_checksum = conn.execute(
            "SELECT document_json, checksum FROM adaptive_revisions WHERE revision_id = ?",
            (revision.revision_id,),
        ).fetchone()
        if stored_checksum != hashlib.sha256(stored_document.encode("utf-8")).hexdigest():
            raise RevisionChecksumError(revision.revision_id)
        conn.execute(
            "UPDATE adaptive_revisions SET complete = 1 WHERE revision_id = ?",
            (revision.revision_id,),
        )
        conn.execute(
            "INSERT INTO active_adaptive_revisions(authority_id, revision_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(authority_id) DO UPDATE SET revision_id=excluded.revision_id, updated_at=excluded.updated_at",
            (revision.authority_id, revision.revision_id, revision.created_at),
        )
```

`read_active_revision(authority_id)` joins only `complete=1` rows and verifies
the checksum. `test_budget_ledger.py` owns atomic reservation/reconciliation
cases and defines `store` from the shared `isolated_home` plus
`mutable_clock`; `test_storage_concurrency.py` opens independent connections and proves
bounded lock behavior; `test_profile_isolation.py` switches `HERMES_HOME`
between two roots and proves neither database observes the other's rows.

- [ ] **Step 4: Run GREEN and reopen/migration tests**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_storage.py \
  tests/plugins/auto_routing/test_budget_ledger.py \
  tests/plugins/auto_routing/test_storage_concurrency.py \
  tests/plugins/auto_routing/test_profile_isolation.py
git diff --check
```

Expected: all tests pass through close/reopen, additive reconciliation, reservation conflict, and incomplete-revision recovery cases.

- [ ] **Step 5: Commit**

```bash
git add plugins/auto_routing/auto_routing/storage.py tests/plugins/auto_routing/test_storage.py tests/plugins/auto_routing/test_budget_ledger.py tests/plugins/auto_routing/test_storage_concurrency.py tests/plugins/auto_routing/test_profile_isolation.py
git diff --cached --check
git commit -m "feat: persist auto routing state"
```

---

### Task 6: Build the Verified Executable Inventory

**Files:**
- Create: `agent/reasoning_support.py`
- Modify: `agent/models_dev.py`
- Modify: `hermes_cli/models.py`
- Modify: `hermes_cli/model_switch.py`
- Modify: `hermes_cli/inventory.py`
- Create: `plugins/auto_routing/auto_routing/adapters/base.py`
- Create: `plugins/auto_routing/auto_routing/adapters/hermes_0_18.py`
- Create: `plugins/auto_routing/auto_routing/adapters/__init__.py`
- Create: `plugins/auto_routing/auto_routing/inventory.py`
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Create: `tests/agent/test_reasoning_support.py`
- Modify: `tests/hermes_cli/test_provider_live_curated_merge.py`
- Create: `tests/plugins/auto_routing/test_inventory.py`

**Interfaces:**
- Consumes: `load_picker_context()`, `build_models_payload(discovery_provenance=True)`, `provider_model_discovery()`, `resolve_runtime_provider(target_model=...)`, `resolve_reasoning_support(provider, model, api_mode, metadata)`, models.dev capabilities, provider cooldown state, local live model listings, and the Stage 1 budget ledger.
- Produces: `HermesAdapter.inventory(refresh=False)`, `HermesAdapter.resolve(runtime_key)`, `HermesAdapter.verify_access(runtime_key, request)`, `InventoryService.refresh()`, `InventoryService.preview_verification(runtime_id)`, `InventoryService.apply_verification(preview_hash, acknowledge_billable)`, and `InventorySnapshot.eligible()`.

- [ ] **Step 1: Write failing eligibility-state tests**

```python
def test_only_live_proven_access_is_verified(fake_adapter):
    assert PROVIDER_MODEL_DISCOVERY_CONTRACT_VERSION == 1
    assert "authenticated_live" in PROVIDER_MODEL_PROVENANCE_VALUES
    assert "succeeded" in PROVIDER_MODEL_LIVE_ATTEMPT_STATUSES
    fake_adapter.rows = [
        provider_row(
            "openai-codex", ["gpt-5.4"], authenticated=True,
            live_attempt_status="succeeded",
            model_provenance={"gpt-5.4": "authenticated_live"},
            provenance_details={"gpt-5.4": {
                "endpoint_identity": "endpoint:codex",
                "auth_identity": "subscription:default",
                "observed_at": "2026-07-15T12:00:00Z",
            }},
            auth_identity="subscription:default",
        ),
        provider_row(
            "anthropic", ["claude-sonnet-4-6"], authenticated=True,
            live_attempt_status="failed",
            model_provenance={"claude-sonnet-4-6": "static_curated"},
            provenance_details={"claude-sonnet-4-6": {"source": "curated"}},
        ),
        provider_row(
            "moa", ["default"], authenticated=True,
            live_attempt_status="succeeded",
            model_provenance={"default": "authenticated_live"},
            provenance_details={"default": {
                "endpoint_identity": "endpoint:moa",
                "auth_identity": "moa:default",
                "observed_at": "2026-07-15T12:00:00Z",
            }},
            auth_identity="moa:default",
        ),
    ]
    snapshot = InventoryService(fake_adapter).refresh()
    assert [(r.key.provider, r.state) for r in snapshot.runtimes] == [
        ("openai-codex", "verified"),
        ("anthropic", "configured_unverified"),
    ]
    assert [r.key.model for r in snapshot.eligible()] == ["gpt-5.4"]


def test_failed_live_discovery_static_fallback_never_proves_access(fake_adapter):
    fake_adapter.rows = [
        provider_row(
            "custom:work", ["private-model"], authenticated=True,
            live_attempt_status="failed",
            model_provenance={"private-model": "configured_declared"},
            provenance_details={"private-model": {"source": "configured"}},
        )
    ]
    runtime = InventoryService(fake_adapter).refresh().runtimes[0]
    assert runtime.state == "configured_unverified"
    assert runtime.reasons == ["model_access_not_live_verified"]


@pytest.mark.parametrize(
    ("provenance", "details"),
    [
        ("authenticated_live", {}),
        ("validated_contract", {"contract_id": "codex-subscription"}),
    ],
)
def test_malformed_strong_provenance_fails_closed(
    fake_adapter, provenance, details,
) -> None:
    fake_adapter.rows = [provider_row(
        "openai-codex", ["gpt-5.4"], authenticated=True,
        live_attempt_status="succeeded",
        model_provenance={"gpt-5.4": provenance},
        provenance_details={"gpt-5.4": details},
    )]
    runtime = InventoryService(fake_adapter).refresh().runtimes[0]
    assert runtime.state == "configured_unverified"
    assert runtime.reasons == ["invalid_model_provenance_details"]


def test_live_discovery_cache_is_scoped_to_exact_credential_fingerprint(
    provider_discovery_harness,
) -> None:
    provider_discovery_harness.configure_access(
        resolver_name="openai:pool-a", credential_fingerprint="pool:a",
        models=["gpt-5.4"],
    )
    provider_discovery_harness.configure_access(
        resolver_name="openai:pool-b", credential_fingerprint="pool:b",
        live_error=TimeoutError(),
    )
    pool_a = provider_discovery_harness.discover(
        provider="openai", resolver_name="openai:pool-a"
    )
    pool_b = provider_discovery_harness.discover(
        provider="openai", resolver_name="openai:pool-b"
    )
    assert pool_a.model_provenance["gpt-5.4"] == "authenticated_live"
    assert pool_b.model_provenance.get("gpt-5.4") != "authenticated_live"
    assert pool_b.credential_fingerprint == "pool:b"
    with pytest.raises(dataclasses.FrozenInstanceError):
        pool_b.live_attempt_status = "succeeded"


def test_discovery_payload_is_opt_in_and_default_shape_is_unchanged(
    picker_context,
) -> None:
    baseline = build_models_payload(picker_context)
    enriched = build_models_payload(picker_context, discovery_provenance=True)
    assert without_discovery_fields(enriched) == baseline
    assert all("discovery" in row for row in enriched["providers"])


def test_local_model_requires_reachable_backend_installed_identity_and_hardware(fake_adapter):
    fake_adapter.local = local_row(
        model="qwen3:14b", backend_identity="ollama:default",
        reachable=True, installed=True, open_weights=True, memory_ok=False,
    )
    runtime = InventoryService(fake_adapter).refresh().runtimes[0]
    assert runtime.key.local_backend == fake_adapter.local.backend_identity
    assert runtime.state == "ineligible"
    assert runtime.reasons == ["hardware_compatibility_unproven"]


def test_paid_verification_is_explicit_bounded_and_exact(
    fake_adapter, routing_store, policy, mutable_clock
):
    fake_adapter.rows = [
        provider_row(
            "anthropic", ["claude-sonnet-4-6"], authenticated=True,
            live_attempt_status="failed",
            model_provenance={"claude-sonnet-4-6": "static_curated"},
            provenance_details={"claude-sonnet-4-6": {"source": "curated"}},
        )
    ]
    policy = policy.model_copy(update={"allow_paid_access_probes": True})
    service = InventoryService(fake_adapter, routing_store, policy, clock=mutable_clock)
    runtime = service.refresh().runtimes[0]

    preview = service.preview_verification(runtime.key.stable_id())
    assert preview.runtime_id == runtime.key.stable_id()
    assert preview.maximum_output_tokens == 16
    assert preview.worst_case_cost_usd > 0
    assert fake_adapter.verify_access_calls == []

    with pytest.raises(VerificationApprovalRequired):
        service.apply_verification(preview.precondition_hash, acknowledge_billable=False)

    result = service.apply_verification(
        preview.precondition_hash, acknowledge_billable=True
    )
    assert result.state == "verified"
    assert fake_adapter.verify_access_calls == [runtime.key]
    assert routing_store.read_inventory(runtime.key).verification_source == "explicit_probe"


def test_reasoning_support_never_expands_a_bare_boolean() -> None:
    assert REASONING_SUPPORT_CONTRACT_VERSION == 1
    exact = resolve_reasoning_support(
        provider="openai-codex",
        model="gpt-5.4",
        api_mode="codex_responses",
        metadata={"reasoning_options": ["low", "medium", "high", "xhigh"]},
    )
    assert exact.exact is True
    assert exact.efforts == ("low", "medium", "high", "xhigh")

    unknown = resolve_reasoning_support(
        provider="custom",
        model="unknown-reasoner",
        api_mode="chat_completions",
        metadata={"supports_reasoning": True},
    )
    assert unknown.exact is False
    assert unknown.efforts == ()


def test_resolve_rejects_collapsed_or_wrong_access_path(fake_adapter) -> None:
    subscription = provider_row(
        "openai-codex", ["gpt-5.4"], authenticated=True,
        live_attempt_status="succeeded",
        model_provenance={"gpt-5.4": "authenticated_live"},
        provenance_details={"gpt-5.4": {
            "endpoint_identity": "endpoint:codex",
            "auth_identity": "subscription:default",
            "observed_at": "2026-07-15T12:00:00Z",
        }},
        auth_identity="subscription:default", resolver_name="openai-codex",
    )
    metered = provider_row(
        "openai", ["gpt-5.4"], authenticated=True,
        live_attempt_status="succeeded",
        model_provenance={"gpt-5.4": "authenticated_live"},
        provenance_details={"gpt-5.4": {
            "endpoint_identity": "endpoint:openai",
            "auth_identity": "api-key:work",
            "observed_at": "2026-07-15T12:00:00Z",
        }},
        auth_identity="api-key:work", resolver_name="openai",
    )
    fake_adapter.rows = [subscription, metered]
    snapshot = InventoryService(fake_adapter).refresh()
    resolved = fake_adapter.resolve(snapshot.runtimes[0].key)
    assert resolved.runtime_key == snapshot.runtimes[0].key

    fake_adapter.return_wrong_auth_identity = True
    with pytest.raises(RuntimeResolutionMismatch):
        fake_adapter.resolve(snapshot.runtimes[0].key)
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_inventory.py
```

Expected: missing inventory/adapter imports.

- [ ] **Step 3: Implement fail-closed inventory evidence**

First add a feature-neutral frozen dataclass discovery result in `hermes_cli/models.py` with `PROVIDER_MODEL_DISCOVERY_CONTRACT_VERSION = 1`, immutable `PROVIDER_MODEL_PROVENANCE_VALUES = ("authenticated_live", "validated_contract", "stale_live_cache", "static_curated", "configured_declared", "current_offline_fallback")`, and immutable `PROVIDER_MODEL_LIVE_ATTEMPT_STATUSES = ("not_attempted", "succeeded", "failed", "probe_disabled")`. `ProviderModelDiscovery` contains ordered model IDs, per-model provenance, per-model non-secret provenance details, live-attempt status, observation timestamp, and the existing non-secret credential fingerprint. `provider_model_discovery(provider: Optional[str], *, force_refresh: bool = False, resolver_name: Optional[str] = None) -> ProviderModelDiscovery` performs the provider-specific fetch and merge. `resolver_name` is a configured, non-secret, addressable Hermes access path; when present, discovery resolves and authenticates only that path. The existing `provider_model_ids()` and `cached_provider_model_ids()` remain list-returning compatibility wrappers over the default resolver. Cache keys include provider, endpoint identity, resolver name, and exact credential-pool fingerprint; a successful listing under pool A can never verify the same provider/model under pool B. Cache entries persist provenance and the successful live timestamp. A failed refresh may still show stale/static models in pickers but must label them honestly; it never upgrades them to live. `list_authenticated_providers()` and custom-provider discovery invoke this once per configured resolver and propagate the result instead of collapsing every row to `source="hermes"`, and the trailing keyword-only `build_models_payload(..., discovery_provenance: bool = False)` preserves the existing payload byte shape by default while the explicit `True` form exposes one `discovery` record per provider/access row. An `authenticated_live` mark is legal only when the exact credential/access path authenticated the listing; its detail requires endpoint identity, auth identity, and success timestamp. A `validated_contract` mark requires a reviewed provider-specific subscription catalog contract ID and integer version—never a generic curated list. Missing or malformed required details downgrade that model to configured-unverified with `invalid_model_provenance_details`.

Extend `tests/hermes_cli/test_provider_live_curated_merge.py` to prove (a) live-plus-curated merges label overlap/live-only as `authenticated_live` and curated-only as `static_curated`, (b) a failed live request labels the visible fallback `static_curated` or `stale_live_cache`, (c) a custom endpoint distinguishes successful `/models`, failed `/models`, and probe-disabled configured rows, (d) the legacy list wrappers retain byte-for-byte ordered model IDs, (e) a successful pool-A cache cannot grant pool B live provenance, and (f) the opt-in discovery payload strips exactly back to the unchanged default payload.

The Hermes 0.18 adapter must:

1. call `build_models_payload(load_picker_context(), picker_hints=True, pricing=True, capabilities=True, discovery_provenance=True, refresh=refresh)` and fail closed when a row lacks per-model provenance;
2. discard `moa` and non-agentic/no-tool models;
3. create configured observations without secret values;
4. mark `verified` only when that exact model's provenance is `authenticated_live` or a named `validated_contract`, a still-fresh successful explicit access verification, a still-fresh completion receipt with full `RuntimeKey` attribution (populated by Stage 3), or installed-local compatibility proves that exact model on that access path;
5. keep static-only rows `configured_unverified`;
6. map cooldown/credential exhaustion to `temporarily_unavailable` without deleting history;
7. verify local loopback backend reachability and exact installed model identity, require open-weight/license metadata, and require either a currently loaded healthy model or a conservative RAM/VRAM fit; and
8. retain a non-secret, addressable `resolver_name`, credential-pool identity, and local-backend identity for each observation; call `resolve_runtime_provider(requested=observation.resolver_name, target_model=model)` only during validation/projection/explicit verification, canonicalize the returned provider/source/base-URL hash/API mode/pool/local-backend identity, and require it to equal the requested full `RuntimeKey`; and
9. return a `ResolvedRuntime` whose secret/callable credentials remain memory-only and are excluded from `repr()`/serialization.

Create `REASONING_SUPPORT_CONTRACT_VERSION = 1`, frozen `ReasoningSupport(efforts, provider_aliases, provenance, exact)`, and content-free `resolve_reasoning_support(*, provider: str, model: str, api_mode: str, metadata: Mapping[str, Any] | None = None) -> ReasoningSupport` in Hermes core. Retain provider-scoped models.dev `reasoning_options`; combine them only with authenticated GitHub catalog efforts, LM Studio allowed options, and the exact effort aliases/clamps already used by Hermes Codex/chat-completions/Anthropic translators. A bare `supports_reasoning=True` never invents levels. Unknown or non-controllable support returns `exact=False, efforts=()`. The adapter stores this record on each observation; Stage 2 active doctor rejects a configured target unless its default/minimum/maximum resolve through an exact non-empty tuple. Provider-specific wire translation remains in Hermes.

Treat each addressable credential pool as one `credential_pool_identity` under its `auth_identity`; rotating secret members inside that pool do not become separate runtimes. If two configured paths cannot be reconstructed/addressed independently, mark both `ineligible` with `ambiguous_access_path` rather than collapsing them. Custom endpoints use a named configured runtime plus a non-reversible endpoint identity hash; never pass a stored raw endpoint or credential from plugin state back into the resolver.

Populate `AccessEconomics` from the actual access path: metered APIs use their token price; subscriptions retain plan limit/quota/reset/throttling and a separate effective marginal/amortized local cost; local runtimes retain optional energy/compute cost. Public API token pricing is evidence for the metered path only and must not be copied onto the same model's subscription runtime. Exhausted subscription quota becomes `temporarily_unavailable`; unknown quota remains eligible with an uncertainty/reliability penalty and an explicit explanation. A user economics override is behavioral config, not `.env`, and never proves access.

Make `tests/agent/test_reasoning_support.py` table-driven over representative Codex, Anthropic, Gemini, Kimi, GitHub Copilot, xAI, and LM Studio metadata, asserting every reported generic effort maps through the same provider translator and aliases/clamps used at request time. Include the unknown-boolean case above and a non-reasoning model; no live calls are permitted.

Unknown hardware/model-size facts yield `hardware_compatibility_unproven`, never an optimistic estimate. `preview_verification` accepts only the stable ID of a currently configured-unverified, non-local runtime from the current inventory snapshot. It resolves that exact configured `RuntimeKey`; it cannot accept arbitrary model, provider, endpoint, auth, or credential arguments. Require `policy.allow_paid_access_probes`, a bounded one-call economics record, a finite worst-case monetary estimate plus configured overhead reserve, and available routing-overhead budget before returning a canonical preview hash. Metered paths require fresh finite input/output prices; subscription paths may use a finite effective marginal cost (including zero) but must expose the bounded quota unit consumed by this one 16-token call and current non-exhausted plan state. Unknown/unbounded economics blocks the probe.

Extend declarative `SCHEMA_SQL` additively with `runtime_verification_attempts(precondition_hash PRIMARY KEY, runtime_id, authority_id, inventory_revision, budget_reservation_id, status, reason_code, input_tokens, output_tokens, actual_cost_usd, created_at, completed_at)`; no column may hold request/response content, endpoint, or credential data.

`apply_verification` revalidates the current inventory/policy/budget and preview hash, then requires `acknowledge_billable=True`. The hash covers command/runtime, authority and inventory revisions, pricing source/value, worst-case amount, budget-ledger revision, TTL, and prior verification-attempt sequence. Atomically insert the hash as a unique one-shot attempt and reserve budget before any provider call, so concurrent/replayed apply requests cannot bill twice; every later preview includes the changed attempt sequence. Through the resolved provider path it makes exactly one no-tool, temperature-zero completion with the fixed plugin-owned prompt `Return exactly AUTO_ROUTING_ACCESS_OK`, a maximum of 16 output tokens, and no conversation/session persistence. The adapter binds the call to the exact resolved provider/auth/pool/endpoint/API-mode/local-backend path and returns that `RuntimeKey`; when a provider reports a response model, it must canonicalize to the requested model through the provider adapter's explicit alias map. Success requires that bound identity, the sentinel, and finite usage. Store only runtime ID, verification source, timestamps/TTL, usage counts, cost, status, and a response hash—not prompt/response text. Reconcile the reservation on every outcome. Failure, mismatch, timeout, or denial leaves the runtime `configured_unverified` and records a sanitized reason. No background refresh, advisor operation, setup/edit action, or later optimizer may invoke this method.

Define `FakeHermesAdapter`, `provider_row()`, and `local_row()` in this test
file. The fake returns only non-secret inventory observations and records every
`resolve(target_model=...)` call; `provider_row` requires explicit
`authenticated`, per-model provenance, per-model provenance details, and live-attempt status fields. It rejects `authenticated_live` without endpoint/auth/timestamp details and `validated_contract` without contract ID/version. Put the pool-cache-isolation and payload-shape tests in `tests/hermes_cli/test_provider_live_curated_merge.py`; its `provider_discovery_harness` configures two addressable resolver names with separate memory-only credentials, scopes a real temporary cache by their returned non-secret credential fingerprints, and `without_discovery_fields()` removes only the opt-in provider `discovery` keys before comparing canonical JSON bytes. `local_row` requires explicit
backend identity, reachability, installation, open-weight, license, model-size, RAM, and
VRAM facts. The fake adapter's verification method records the exact key and
returns only sentinel/status/runtime identity/usage metadata. Define mutable
`policy`, real temporary `routing_store`, and `mutable_clock` fixtures here.

- [ ] **Step 4: Run GREEN and Hermes resolver regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_inventory.py \
  tests/plugins/auto_routing/test_budget_ledger.py \
  tests/agent/test_reasoning_support.py \
  tests/hermes_cli/test_provider_live_curated_merge.py \
  tests/hermes_cli/test_runtime_provider_resolution.py
git diff --check
```

Expected: all tests pass; a configured-but-unverified provider never appears in `eligible()`.

- [ ] **Step 5: Commit**

```bash
git add agent/reasoning_support.py agent/models_dev.py hermes_cli/models.py hermes_cli/model_switch.py hermes_cli/inventory.py tests/agent/test_reasoning_support.py tests/hermes_cli/test_provider_live_curated_merge.py plugins/auto_routing/auto_routing/adapters plugins/auto_routing/auto_routing/inventory.py plugins/auto_routing/auto_routing/storage.py tests/plugins/auto_routing/test_inventory.py
git diff --cached --check
git commit -m "feat: inventory executable routing runtimes"
```

---

### Task 7: Preserve Catalog Provenance and Rank Advisor Candidates

**Files:**
- Create: `plugins/auto_routing/auto_routing/catalog.py`
- Create: `plugins/auto_routing/auto_routing/scoring.py`
- Create: `plugins/auto_routing/auto_routing/advisor.py`
- Create: `tests/plugins/auto_routing/test_catalog.py`

**Interfaces:**
- Consumes: verified `InventorySnapshot`, `agent.models_dev.get_model_info()`, Hermes picker pricing/capabilities, and user-imported JSON evidence.
- Produces: `CatalogService.refresh(sources)`, `CatalogService.evidence_for(runtime)`, `Advisor.propose(request)`, and `Advisor.dry_run(prompts, proposal)`.

- [ ] **Step 1: Write failing provenance/ranking tests**

```python
def test_conflicting_evidence_remains_separate_and_cannot_grant_access(catalog, inventory):
    catalog.import_records([
        evidence("swe-bench", "coding", 0.62, source_url="https://www.swebench.com/", confidence=0.8),
        evidence("review-lab", "coding", 0.48, source_url="https://example.test/review", confidence=0.4),
    ])
    rows = catalog.evidence_for(inventory.verified("openai-codex", "gpt-5.4"))
    assert [row.source_id for row in rows] == ["swe-bench", "review-lab"]
    assert all(row.source_url and row.retrieved_at for row in rows)


def test_catalog_evidence_cannot_upgrade_an_unverified_runtime(catalog, inventory):
    candidate = inventory.configured_unverified(
        "anthropic", "claude-sonnet-4-6"
    )
    catalog.import_records([
        evidence(
            "review-lab", "coding", 0.99,
            model=candidate.key.model,
            source_url="https://example.test/review",
            confidence=0.99,
        ),
    ])
    assert catalog.evidence_for(candidate)
    assert candidate.state == "configured_unverified"
    assert candidate not in inventory.eligible()


def test_advisor_never_recommends_unverified_runtime(advisor, proposal_request):
    proposal = advisor.propose(proposal_request)
    targets = [proposal.primary, *proposal.fallbacks]
    assert all(target.inventory_state == "verified" for target in targets)
    assert proposal.explanation.rejected["anthropic/claude-sonnet-4-6"] == "configured_unverified"


def test_subscription_and_metered_paths_use_separate_economics(
    advisor, cost_heavy_request, same_model_access_paths
) -> None:
    subscription, metered = same_model_access_paths
    proposal = advisor.propose(
        cost_heavy_request.model_copy(update={"inventory": same_model_access_paths})
    )
    assert subscription.key.model == metered.key.model
    assert subscription.key.stable_id() != metered.key.stable_id()
    assert proposal.primary.runtime_id == subscription.key.stable_id()
    assert proposal.explanation.candidates[metered.key.stable_id()]["estimated_cost_usd"] > 0
    assert proposal.explanation.candidates[subscription.key.stable_id()]["billing_kind"] == "subscription"
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_catalog.py
```

Expected: missing catalog/advisor imports.

- [ ] **Step 3: Implement three source adapters and deterministic initial rank**

Implement `HermesCatalogSource`, `ModelsDevCatalogSource`, and `JsonCatalogSource`. JSON import validates each record's canonical link, dates, model/version, domain/task, direction, scale, value, sample/confidence, and normalization. Model-level quality/capability evidence may be shared across access paths only with an exact canonical model/version match; reliability, latency, price, quota, throttle, and effective-cost evidence remains access-path-local unless its source explicitly identifies that full runtime. Reject endpoint URLs, credentials, executable fields, and unknown metric direction.

Put normalization and this fixed utility formula in pure `scoring.py`; the advisor and Stage 2 selector must call the same function rather than copy it. Initial advisor utility uses only hard-eligible candidates:

```python
utility = (
    objectives.quality * conservative_metric("quality")
    + objectives.reliability * conservative_metric("reliability")
    + objectives.latency * (1.0 - normalized_latency)
    + objectives.cost * (1.0 - normalized_cost)
    - uncertainty_penalty
    - staleness_penalty
)
```

Missing performance evidence gets a documented conservative prior and penalty; it is not zero. For cost utility and hard task-cost gates, metered paths use bounded token estimates, subscriptions use their effective local marginal/amortized per-task cost plus separate quota/throttle capacity, and local paths use their configured/observed compute cost; never substitute a same-model public API price across billing kinds. Unknown quota/cost adds uncertainty and may block an otherwise unbounded hard gate, but never becomes a fabricated number. Base rank is a final tie-breaker. The explanation lists every accepted/rejected candidate, normalized inputs, billing kind/quota/throttle, sources/dates/confidence, and uncertainty. `dry_run` classifies representative requirements without executing the task or changing YAML/DB active revisions.

Define the catalog/inventory/advisor fixtures in this test file from two
verified observations and one configured-unverified observation, plus
`same_model_access_paths` for subscription and metered access to one model. The
`evidence()` builder requires source ID/URL, retrieval/publication timestamps,
model version, domain, metric direction/scale/value, sample size, confidence,
and normalization method. `proposal_request` declares all four objectives and
finite cost/latency limits.

- [ ] **Step 4: Run GREEN and offline-cache tests**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_catalog.py
git diff --check
```

Expected: all tests pass with live-source failure falling back to the last valid snapshot and a larger staleness penalty.

- [ ] **Step 5: Commit**

```bash
git add plugins/auto_routing/auto_routing/catalog.py plugins/auto_routing/auto_routing/scoring.py plugins/auto_routing/auto_routing/advisor.py tests/plugins/auto_routing/test_catalog.py
git diff --cached --check
git commit -m "feat: rank routing profiles with provenance"
```

---

### Task 8: Complete the Stage 1 CLI, Advisor Flow, and End-to-End Gate

**Files:**
- Modify: `plugins/auto_routing/auto_routing/cli.py`
- Modify: `plugins/auto_routing/auto_routing/service.py`
- Modify: `plugins/auto_routing/auto_routing/config_io.py`
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Modify: `plugins/auto_routing/skills/auto-routing/SKILL.md`
- Modify: `plugins/auto_routing/README.md`
- Create: `tests/plugins/auto_routing/fixtures/advisor_interview.json`
- Create: `tests/plugins/auto_routing/test_advisor_cli.py`
- Create: `tests/plugins/auto_routing/test_foundation_e2e.py`

**Interfaces:**
- Consumes: config/store/inventory/catalog/advisor services from Tasks 3–7.
- Produces: Stage 1 CLI commands `setup`, `edit`, `inventory`, `verify-runtime`, `refresh-catalog`, `plan`, `validate`, `status`, and `doctor`.

- [ ] **Step 1: Write failing CLI and real-path E2E tests**

```python
def test_setup_requires_preview_hash_and_explicit_apply(cli, proposal_file):
    preview = cli.run("setup", "--proposal", proposal_file, "--json")
    assert preview["applied"] is False
    assert len(preview["expected_config_sha256"]) == 64
    assert preview["expected_config_sha256"] == preview["precondition_sha256"]
    applied = cli.run(
        "setup", "--proposal", proposal_file, "--apply",
        "--expected-config-sha", preview["expected_config_sha256"], "--json",
    )
    assert applied["applied"] is True
    assert applied["activation"]["mode"] == "shadow"


def test_foundation_never_changes_agent_runtime(isolated_home, fake_provider, plugin_enabled):
    before = run_real_agent_once("reply with ok", fake_provider)
    preview = run_cli(
        "auto-routing", "setup", "--proposal", approved_proposal(), "--json"
    )
    run_cli(
        "auto-routing",
        "setup",
        "--proposal",
        approved_proposal(),
        "--apply",
        "--expected-config-sha",
        preview["expected_config_sha256"],
    )
    after = run_real_agent_once("reply with ok", fake_provider)
    assert (after.provider, after.model) == (before.provider, before.model)
    assert routing_store().count_decisions() == 0


def test_verify_runtime_requires_matching_preview_and_billable_ack(
    cli, configured_unverified_runtime, fake_provider
):
    preview = cli.run(
        "verify-runtime", configured_unverified_runtime.stable_id(), "--json"
    )
    assert preview["applied"] is False
    assert preview["billable"] is True
    assert fake_provider.request_count == 0

    rejected = cli.run(
        "verify-runtime",
        configured_unverified_runtime.stable_id(),
        "--apply", "--expect-hash", preview["precondition_hash"], "--json",
    )
    assert rejected["exit_code"] == 2
    assert fake_provider.request_count == 0

    applied = cli.run(
        "verify-runtime",
        configured_unverified_runtime.stable_id(),
        "--apply", "--expect-hash", preview["precondition_hash"],
        "--ack-billable", "--json",
    )
    assert applied["state"] == "verified"
    assert fake_provider.request_count == 1


def test_golden_advisor_interview_blocks_until_every_required_fact(
    advisor, advisor_interview_fixture
) -> None:
    turns = json.loads(advisor_interview_fixture.read_text(encoding="utf-8"))
    for case in turns["partial_requests"]:
        readiness = advisor.validate_request(case["request"])
        assert readiness.ready is False
        assert list(readiness.missing_facts) == case["expected_missing_facts"]

    plan = advisor.propose(turns["complete_request"])
    assert plan.readiness.ready is True
    assert plan.dry_run.results
    assert all(item.resolution_status == "verified" for item in plan.targets)
    assert all(item.sources and item.uncertainty is not None for item in plan.ranking)
    assert plan.initial_revision.canonical_json()
    assert "--apply" not in plan.next_command


def test_setup_db_failure_restores_yaml_and_leaves_no_half_authority(
    cli, proposal_file, config_path, routing_store, fault_injector
) -> None:
    before = config_path.read_bytes()
    preview = cli.run("setup", "--proposal", proposal_file, "--json")
    fault_injector.fail_next_baseline_publish()
    result = cli.run(
        "setup", "--proposal", proposal_file, "--apply",
        "--expected-config-sha", preview["expected_config_sha256"], "--json",
    )
    assert result["exit_code"] == 2
    assert config_path.read_bytes() == before
    assert routing_store.list_authority_revisions() == []
    assert not list(config_path.parent.glob("auto-routing-apply-*.pending.json"))


def test_restart_recovers_crash_after_yaml_replace(
    cli, proposal_file, config_path, service_factory, fault_injector
) -> None:
    preview = cli.run("setup", "--proposal", proposal_file, "--json")
    fault_injector.crash_after_yaml_replace()
    with pytest.raises(SimulatedProcessDeath):
        cli.run(
            "setup", "--proposal", proposal_file, "--apply",
            "--expected-config-sha", preview["expected_config_sha256"],
        )
    assert list(config_path.parent.glob("auto-routing-apply-*.pending.json"))

    restarted = service_factory()
    assert restarted.doctor()["incomplete_config_apply"] is False
    assert restarted.store.read_active_revision(restarted.authority_id).is_baseline
    assert not list(config_path.parent.glob("auto-routing-apply-*.pending.json"))


def test_stage1_commands_publish_explicit_write_classes(cli) -> None:
    assert cli.command_metadata("status").write_class == "read_only"
    assert cli.command_metadata("inventory", refresh=True).write_class == "append_only_observation"
    assert cli.command_metadata("refresh-catalog").write_class == "append_only_observation"
    assert cli.command_metadata("setup").write_class == "guarded_control_plane"
    assert cli.command_metadata("verify-runtime").write_class == "guarded_control_plane"
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_advisor_cli.py \
  tests/plugins/auto_routing/test_foundation_e2e.py
```

Expected: missing commands and service orchestration failures.

- [ ] **Step 3: Wire the exact Stage 1 command surface**

Register these argparse forms:

```text
hermes auto-routing setup --proposal FILE [--apply --expected-config-sha SHA256] [--json]
hermes auto-routing edit --proposal FILE [--apply --expected-config-sha SHA256] [--json]
hermes auto-routing inventory [--refresh] [--include-ineligible] [--json]
hermes auto-routing verify-runtime RUNTIME_STABLE_ID [--apply --expect-hash HASH --ack-billable] [--json]
hermes auto-routing refresh-catalog [--models-dev] [--hermes] [--file FILE] [--json]
hermes auto-routing plan --request FILE [--prompt-file FILE ...] [--json]
hermes auto-routing validate [--proposal FILE] [--json]
hermes auto-routing status [--json]
hermes auto-routing doctor [--json]
```

Implement a closed `CommandWriteClass` enum with `read_only`,
`append_only_observation`, and `guarded_control_plane`; every registered
subcommand declares one and includes it in help/JSON metadata. Setup/edit and
billable verification are guarded. Non-billable inventory/catalog refreshes
append immutable, deduplicated observations in short transactions and cannot
write authority, activation, active-revision, or decision tables. The remaining
Stage 1 commands are read-only. An unclassified command is a registration error.

`verify-runtime` is the only Stage 1 path to a paid or quota-consuming access completion. Its preview names the exact configured runtime, billing kind/economics source, fixed probe shape, maximum monetary cost, maximum quota unit, budget reservation class, verification TTL, and canonical precondition hash. Apply requires the same runtime, unchanged policy/inventory/economics, matching hash, `--ack-billable`, and the policy opt-in; it delegates to Task 6 and never installs a model or changes the active agent runtime. The advisor skill may offer it only during setup/edit after explaining cost/quota impact and obtaining approval; ordinary chat, `plan`, `inventory --refresh`, and autonomous stages never call it.

Define one complete `AdvisorRequest` contract covering workload domains/examples, modalities, risk/tool-use, hard provider/model/license/local/subscription/cost/latency limits, all four objective weights, classifier/evaluator identity plus full-disclosure approval, named profiles/base ranks, primary/fallback access paths, per-target reasoning default/minimum/maximum, and representative prompts. `plan` returns exit 2 plus an ordered `missing_facts` list until all required facts exist. A complete plan reports evidence URLs/dates/confidence/uncertainty, rejected inaccessible candidates, economics by access path, dry-run results, exact resolver validation, the YAML diff, and the canonical initial baseline revision; it never emits an apply command as already approved.

Make setup/edit a recoverable cross-file saga under the profile routing lock. Preview covers exact before/after YAML bytes plus the complete baseline authority/revision JSON and their hashes. Apply writes and fsyncs `auto-routing-apply-{operation_id}.pending.json` beside the config with before/after hashes, backup path, authority ID, baseline revision ID/checksum, and phase but no secrets; atomically replaces YAML; publishes authority plus baseline in one SQLite transaction; verifies both; marks the journal complete; then removes it. A normal DB/config verification failure restores and verifies the YAML backup and rolls back DB before returning exit 2. At startup, a leftover journal deterministically completes an idempotent matching DB publication when YAML has the exact after hash, or restores the exact before backup otherwise; until recovery succeeds, effective activation is off and doctor reports `incomplete_config_apply`. Thus no process may observe approved YAML with a missing/mismatched baseline as usable authority.

`doctor` checks config readability/schema, profile isolation, DB/WAL/fallback, pending apply journals, inventory freshness, exact primary/fallback verification, safe-default compliance, classifier/evaluator trust allowlists, and Stage 2 adapter absence. It returns exit `0` when the advisor is healthy and exit `2` for invalid authority; the absent runtime adapter is reported as `not_installed`, not a Stage 1 failure. `setup/edit` materialize normalized objective weights and an initial baseline revision through the saga above.

In `test_advisor_cli.py`, define a CLI runner that invokes the registered
handler with parsed argparse values and captures JSON/exit status. In the E2E
file, `fake_provider` is a local OpenAI-compatible HTTP server,
`run_real_agent_once` constructs the real `AIAgent` against it, and
`approved_proposal()` writes a valid shadow proposal file. Load/enable the
plugin through the real `PluginManager`; open `RoutingStore` through the real
profile path. No helper may patch `AIAgent.run_conversation`, config IO, plugin
discovery, or SQLite.

`advisor_interview_fixture` points to the committed JSON fixture.
`fault_injector` is a test-only dependency injected into
the saga/store boundary; production services expose no mutable fault flag.
`service_factory` reconstructs the real service/store against the same temporary
profile and therefore runs startup journal recovery.

The golden fixture contains cumulative partial requests with the exact expected missing-fact sequence and one complete request for the flow: inventory; workload/modalities/risk/tool use; hard limits; four objectives; classifier/evaluator disclosure; named profiles; evidence dates/uncertainty; access-path economics; per-target reasoning bounds; representative dry run; resolver validation; exact diff/initial revision; and explicit approval. Exercise those contracts through the advisor/CLI behavior above; do not duplicate skill prose into the fixture as a change-detector assertion. Update the skill with the corresponding JSON-field checks and a firm approval boundary, including the separate `verify-runtime` preview, maximum-cost explanation, policy opt-in, explicit billable acknowledgement, and post-verification inventory refresh. The README documents `hermes plugins enable auto-routing`, `auto-routing:auto-routing`, profile-local paths, no raw prompt storage, no automatic probes, and shadow-only behavior.

- [ ] **Step 4: Run the Stage 1 gate**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing
uv run --extra dev python -m pytest -q \
  tests/hermes_cli/test_plugin_cli_registration.py \
  tests/test_plugin_skills.py \
  tests/test_packaging_metadata.py \
  tests/agent/test_plugin_llm.py
uv run --extra dev ruff check plugins/auto_routing tests/plugins/auto_routing
git diff --check
```

Expected: all tests pass, Ruff is clean, ordinary runtime identity remains unchanged, and no routing decision row exists.

- [ ] **Step 5: Commit**

```bash
git add plugins/auto_routing tests/plugins/auto_routing
git diff --cached --check
git commit -m "feat: ship auto routing advisor foundation"
```
