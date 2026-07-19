# Hermes Auto Routing Hardening and Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the completed Auto Routing plugin resilient to host drift, schema change, concurrent processes, interruption, corrupt state, malicious metadata, clean installation, profile-local export/import, and eventual standalone-repository extraction.

**Architecture:** Versioned host contracts turn private Hermes seams into a tested compatibility boundary that safely refuses unknown hosts. Declarative schema plus transactional backups/recovery protect profile-local state. Canonical redacted exchange bundles support explicit transfer without secrets or task content. A packaging boundary check permits Hermes-private imports only in the adapter/registration layer, and a standalone entry point loads the same implementation used inside the fork.

**Tech Stack:** Hermes `0.18.2` compatibility contract, Python `sqlite3.backup`, `zipfile`, `hashlib`, `ast`, `subprocess`, `psutil` PID/create-time identity, Pydantic v2, pytest process/fault tests, setuptools build metadata, temporary virtual environments, and the existing plugin loader.

## Global Constraints

- The initially supported private-host contract is exactly Hermes Agent `0.18.2`, the version in this fork. A later version is unsupported until its generated contract and real-path suite are reviewed and committed.
- Unknown versions or drift in a critical fresh-session, exact access-binding, model-discovery/reasoning, PluginLlm, RuntimeIntent, child-construction, or manual-precedence capability produce an inactive adapter and unchanged Hermes behavior. Drift isolated to the optional recorded-fallback seam keeps the complete pre-call/manual wrapper group active and reports `post_call_failover=False`; drift in the versioned evidence payload disables evidence/adaptation while preserving compatible static routing. Never install a partial critical group.
- Compatibility checks inspect signatures and required attributes, not fragile source-code hashes. Behavior tests remain the authoritative gate.
- Schema creation remains declarative and idempotent. Ordered migrations contain only required data transforms, are checksum-addressed, and create a verified pre-migration backup.
- Runtime readers see the last complete valid state during bounded SQLite contention. Corruption falls back only to a verified local snapshot; otherwise routing inherits baseline Hermes behavior.
- Export/import excludes credentials, raw prompts/responses, transient disclosed content, provider error bodies, absolute home paths, and analytics identifiers. Cross-profile import is explicit.
- Restore/import never overwrites the only recoverable copy. All writes are preview/hash guarded, locked, validated in a sibling temporary path, and atomically replaced.
- Schema migration, restore, and downgrade never replace a database beneath another live process. Every hardened `RoutingStore` registers a profile-local process lease before opening; destructive maintenance quiesces all local instances and refuses while any other live PID/start-token lease exists.
- No automatic live provider calls occur in tests or installation. A live smoke command requires an explicit runtime allowlist and `--allow-billable`; local-only targets still require an explicit target.
- Standalone packaging has no runtime dependency on a pinned Hermes wheel. It declares the host integration through the `hermes_agent.plugins` entry point and imports host internals lazily only inside the compatibility adapter.
- `plugins/auto_routing` remains the source in this fork. Extraction documentation describes a future operation; this plan does not publish a repository or package.
- Preserve the Hermes MIT license and credit the MIT-licensed `b3nw/hermes-delegate-routing` design reference. If implementation code is actually reused, preserve its copyright/license notice and preferably its git authorship.
- Full prompt-cache, manual precedence, fallback, inventory, authority, privacy, experiment, and rollback invariants from Plans 1–5 remain release blockers.
- Before every Step 5 commit, run both `git diff --check` and the shown `git diff --cached --check`; stop on any output/error.

---

## File Map

### Plugin files created

- `plugins/auto_routing/auto_routing/compatibility.py` — supported-host registry and contract comparison.
- `plugins/auto_routing/auto_routing/migrations.py` — ordered transform registry and preflight.
- `plugins/auto_routing/auto_routing/backup.py` — verified SQLite/config backup and atomic restore.
- `plugins/auto_routing/auto_routing/maintenance.py` — process leases, quiescence generation, and destructive-maintenance coordinator.
- `plugins/auto_routing/auto_routing/exchange.py` — canonical redacted export/import bundles.
- `plugins/auto_routing/auto_routing/diagnostics.py` — local health, redaction, and support bundle.
- `plugins/auto_routing/auto_routing/plugin.py` — shared bundled/entry-point registration.
- `plugins/auto_routing/auto_routing/assets/skills/auto-routing/SKILL.md` — packaged explicit-load skill.
- `plugins/auto_routing/auto_routing/assets/contracts/hermes-0.18.2.json` — reviewed host seam contract packaged through `importlib.resources`.
- `plugins/auto_routing/pyproject.toml` — future standalone wheel metadata.
- `plugins/auto_routing/LICENSE` — copy of this repository's MIT license.
- `plugins/auto_routing/THIRD_PARTY_NOTICES.md` — Hermes and routing-reference provenance.
- `plugins/auto_routing/EXTRACTION.md` — standalone repository boundary and procedure.

### Existing files modified

- `plugins/auto_routing/__init__.py` — re-export shared registration.
- `plugins/auto_routing/plugin.yaml` — supported-host and packaged-skill metadata.
- `plugins/auto_routing/auto_routing/adapters/hermes_0_18.py` — consume the reviewed compatibility contract.
- `plugins/auto_routing/auto_routing/storage.py` — migration, corruption, and verified-snapshot paths.
- `plugins/auto_routing/auto_routing/config_io.py` — backup/restore integration.
- `plugins/auto_routing/auto_routing/service.py` — degraded-state recovery and diagnostics.
- `plugins/auto_routing/auto_routing/cli.py` — compatibility, backup, restore, export/import, and support commands.
- `plugins/auto_routing/README.md` — supported-version, recovery, live-smoke, and credit documentation.
- `pyproject.toml` — final bundled asset/package-data coverage.
- `MANIFEST.in` — final sdist coverage.
- `website/docs/user-guide/plugins/auto-routing.md` — fork-local user guide.

### Scripts created

- `scripts/check_auto_routing_compat.py` — compare current host with the committed contract or emit a review candidate.
- `scripts/check_auto_routing_boundary.py` — AST-based private-import boundary gate.
- `scripts/auto_routing_live_smoke.py` — explicit opt-in live verification.

### Tests created

- `tests/plugins/auto_routing/test_compatibility_contract.py`
- `tests/plugins/auto_routing/test_migrations_backup_restore.py`
- `tests/plugins/auto_routing/test_multiprocess_stress.py`
- `tests/plugins/auto_routing/helpers/process_worker.py`
- `tests/plugins/auto_routing/test_exchange.py`
- `tests/plugins/auto_routing/test_security_hardening.py`
- `tests/plugins/auto_routing/test_extraction_boundary.py`
- `tests/plugins/auto_routing/test_standalone_package.py`
- `tests/plugins/auto_routing/test_clean_install_smoke.py`
- `tests/plugins/auto_routing/test_live_smoke_guard.py`

---

### Task 1: Freeze and Enforce the Hermes 0.18.2 Compatibility Contract

**Files:**
- Create: `plugins/auto_routing/auto_routing/compatibility.py`
- Create: `plugins/auto_routing/auto_routing/assets/contracts/hermes-0.18.2.json`
- Create: `scripts/check_auto_routing_compat.py`
- Modify: `plugins/auto_routing/auto_routing/adapters/hermes_0_18.py`
- Modify: `plugins/auto_routing/plugin.yaml`
- Create: `tests/plugins/auto_routing/test_compatibility_contract.py`

- [ ] **Step 1: Write failing exact-version, drift, and all-or-nothing tests**

```python
def test_current_host_matches_committed_contract() -> None:
    result = compare_current_host(load_contract("hermes-0.18.2.json"))
    assert result.host_version == "0.18.2"
    assert result.compatible
    assert result.differences == ()


def test_unknown_patch_version_is_not_assumed_compatible(monkeypatch) -> None:
    monkeypatch.setattr("hermes_cli.__version__", "0.18.3")
    result = select_contract()
    assert not result.compatible
    assert result.reason == "unsupported_host_version"


def test_one_drifted_seam_prevents_every_wrapper(fake_host, adapter_service) -> None:
    fake_host.change_signature("tools.delegate_tool._build_child_agent", "(*args, new_required)")
    status = Hermes018Adapter(fake_host).install(adapter_service)
    assert not status.active
    assert status.installed_wrappers == ()
    assert fake_host.all_original_identities_preserved()


def test_fallback_only_drift_preserves_precall_routing(fake_host, adapter_service) -> None:
    fake_host.change_signature(
        "run_agent.AIAgent._try_activate_fallback", "(self, reason=None, new_optional=None)"
    )
    drifted_group = fake_host.group_identities("fallback")
    drifted_fallback = fake_host.resolve("run_agent.AIAgent._try_activate_fallback")
    status = Hermes018Adapter(fake_host).install(adapter_service)
    assert status.active
    assert status.post_call_failover is False
    assert fake_host.group_identities("fallback") == drifted_group
    assert fake_host.resolve("run_agent.AIAgent._try_activate_fallback") is drifted_fallback
    assert not getattr(
        fake_host.resolve("run_agent.AIAgent._try_activate_fallback"),
        "__auto_routing_wrapped__",
        False,
    )


def test_missing_plugin_llm_reasoning_contract_blocks_active(
    fake_host, adapter_service
) -> None:
    fake_host.remove_parameter(
        "agent.plugin_llm.PluginLlm.complete_structured", "reasoning_config"
    )
    status = Hermes018Adapter(fake_host).install(adapter_service)
    assert status.active is False
    assert status.reason == "critical_host_contract_mismatch"


def test_runtime_intent_record_contract_drift_blocks_active(fake_host, adapter_service) -> None:
    fake_host.set_constant("agent.runtime_intent.RUNTIME_INTENT_CONTRACT_VERSION", 2)
    status = Hermes018Adapter(fake_host).install(adapter_service)
    assert status.active is False
    assert status.reason == "critical_host_contract_mismatch"


def test_runtime_intent_constructor_seam_drift_blocks_active(fake_host, adapter_service) -> None:
    fake_host.remove_parameter("run_agent.AIAgent.__init__", "runtime_intent")
    status = Hermes018Adapter(fake_host).install(adapter_service)
    assert status.active is False
    assert status.reason == "critical_host_contract_mismatch"


@pytest.mark.parametrize("slot", ["local_backend", "_credential_pool"])
def test_runtime_access_slot_contract_drift_blocks_active(
    fake_host, adapter_service, slot,
) -> None:
    fake_host.remove_slot("agent.runtime_access.RuntimeAccessBinding", slot)
    status = Hermes018Adapter(fake_host).install(adapter_service)
    assert status.active is False
    assert status.reason == "critical_host_contract_mismatch"


@pytest.mark.parametrize(
    ("path", "parameter"),
    [
        ("hermes_cli.models.provider_model_discovery", "resolver_name"),
        ("hermes_cli.inventory.build_models_payload", "discovery_provenance"),
    ],
)
def test_discovery_callable_drift_blocks_active(
    fake_host, adapter_service, path, parameter,
) -> None:
    fake_host.remove_parameter(path, parameter)
    status = Hermes018Adapter(fake_host).install(adapter_service)
    assert status.active is False
    assert status.reason == "critical_host_contract_mismatch"


def test_discovery_provenance_enum_drift_blocks_active(fake_host, adapter_service) -> None:
    fake_host.set_constant(
        "hermes_cli.models.PROVIDER_MODEL_PROVENANCE_VALUES",
        ("authenticated_live", "static_curated"),
    )
    status = Hermes018Adapter(fake_host).install(adapter_service)
    assert status.active is False
    assert status.reason == "critical_host_contract_mismatch"


def test_missing_versioned_post_turn_fields_disable_evidence_and_adaptation_only(
    fake_host, adapter_service
) -> None:
    fake_host.set_constant("agent.turn_finalizer.POST_LLM_CALL_PAYLOAD_VERSION", 1)
    status = Hermes018Adapter(fake_host).install(adapter_service)
    assert status.active is True
    assert status.evidence_attribution is False
    assert status.adaptation is False
    assert adapter_service.effective_adaptation_mode == "disabled"


def test_annotation_repr_changes_do_not_change_normalized_contract(fake_host) -> None:
    fake_host.change_annotations_only(
        "run_agent.AIAgent.run_conversation", {"stream_callback": "Callable[..., Any]"}
    )
    assert compare_host(fake_host, load_contract("hermes-0.18.2.json")).compatible
```

The test-local `fake_host` maps every contract symbol to a callable/object with the committed normalized parameters, dataclass fields/frozen flag, slots, literal constants, and required set members; it exposes a constructed fake agent carrying the required live-instance attributes and records wrapper assignments. `change_signature()` replaces one callable with a dynamically defined function, `remove_parameter()` removes a required capability keyword, `remove_slot()` changes a slotted record layout, `set_constant()` mutates a version literal, `group_identities()` returns the current callable identities for an atomic capability group, and `change_annotations_only()` preserves parameters/defaults. `adapter_service` is the minimal fake implementing `decide()`, `record_adapter_event()`, and adaptation capability gating.

- [ ] **Step 2: Run the compatibility test and record RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_compatibility_contract.py
```

Expected: failures report missing contract registry/comparison.

- [ ] **Step 3: Generate and review the exact 0.18.2 contract**

The JSON contract contains normalized callable structure rather than Python-version-sensitive annotation reprs:

```json
{
  "contract_version": 4,
  "host_distribution": "hermes-agent",
  "host_version": "0.18.2",
  "seams": [
    {"module": "run_agent", "qualname": "AIAgent.run_conversation", "group": "critical", "parameters": [["self","POSITIONAL_OR_KEYWORD",{"required":true}],["user_message","POSITIONAL_OR_KEYWORD",{"required":true}],["system_message","POSITIONAL_OR_KEYWORD",{"default":null}],["conversation_history","POSITIONAL_OR_KEYWORD",{"default":null}],["task_id","POSITIONAL_OR_KEYWORD",{"default":null}],["stream_callback","POSITIONAL_OR_KEYWORD",{"default":null}],["persist_user_message","POSITIONAL_OR_KEYWORD",{"default":null}],["persist_user_timestamp","POSITIONAL_OR_KEYWORD",{"default":null}],["moa_config","POSITIONAL_OR_KEYWORD",{"default":null}]]},
    {"module": "run_agent", "qualname": "AIAgent.switch_model", "group": "critical", "parameters": [["self","POSITIONAL_OR_KEYWORD",{"required":true}],["new_model","POSITIONAL_OR_KEYWORD",{"required":true}],["new_provider","POSITIONAL_OR_KEYWORD",{"required":true}],["api_key","POSITIONAL_OR_KEYWORD",{"default":""}],["base_url","POSITIONAL_OR_KEYWORD",{"default":""}],["api_mode","POSITIONAL_OR_KEYWORD",{"default":""}],["runtime_access_binding","POSITIONAL_OR_KEYWORD",{"default":null}]]},
    {"module": "agent.agent_runtime_helpers", "qualname": "switch_model", "group": "critical", "parameters": [["agent","POSITIONAL_OR_KEYWORD",{"required":true}],["new_model","POSITIONAL_OR_KEYWORD",{"required":true}],["new_provider","POSITIONAL_OR_KEYWORD",{"required":true}],["api_key","POSITIONAL_OR_KEYWORD",{"default":""}],["base_url","POSITIONAL_OR_KEYWORD",{"default":""}],["api_mode","POSITIONAL_OR_KEYWORD",{"default":""}],["runtime_access_binding","POSITIONAL_OR_KEYWORD",{"default":null}]]},
    {"module": "tools.delegate_tool", "qualname": "_build_child_agent", "group": "critical", "parameters": [["task_index","POSITIONAL_OR_KEYWORD",{"required":true}],["goal","POSITIONAL_OR_KEYWORD",{"required":true}],["context","POSITIONAL_OR_KEYWORD",{"required":true}],["toolsets","POSITIONAL_OR_KEYWORD",{"required":true}],["model","POSITIONAL_OR_KEYWORD",{"required":true}],["max_iterations","POSITIONAL_OR_KEYWORD",{"required":true}],["task_count","POSITIONAL_OR_KEYWORD",{"required":true}],["parent_agent","POSITIONAL_OR_KEYWORD",{"required":true}],["override_provider","POSITIONAL_OR_KEYWORD",{"default":null}],["override_base_url","POSITIONAL_OR_KEYWORD",{"default":null}],["override_api_key","POSITIONAL_OR_KEYWORD",{"default":null}],["override_api_mode","POSITIONAL_OR_KEYWORD",{"default":null}],["override_request_overrides","POSITIONAL_OR_KEYWORD",{"default":null}],["override_reasoning_config","POSITIONAL_OR_KEYWORD",{"default":null}],["override_runtime_access_binding","POSITIONAL_OR_KEYWORD",{"default":null}],["override_max_tokens","POSITIONAL_OR_KEYWORD",{"default":null}],["override_acp_command","POSITIONAL_OR_KEYWORD",{"default":null}],["override_acp_args","POSITIONAL_OR_KEYWORD",{"default":null}],["role","POSITIONAL_OR_KEYWORD",{"default":"leaf"}]]},
    {"module": "run_agent", "qualname": "AIAgent._try_activate_fallback", "group": "fallback", "parameters": [["self","POSITIONAL_OR_KEYWORD",{"required":true}],["reason","POSITIONAL_OR_KEYWORD",{"default":null}],["runtime_access_binding","POSITIONAL_OR_KEYWORD",{"default":null}]]},
    {"module": "agent.chat_completion_helpers", "qualname": "try_activate_fallback", "group": "fallback", "parameters": [["agent","POSITIONAL_OR_KEYWORD",{"required":true}],["reason","POSITIONAL_OR_KEYWORD",{"default":null}],["runtime_access_binding","POSITIONAL_OR_KEYWORD",{"default":null}]]},
    {"module": "run_agent", "qualname": "AIAgent._restore_primary_runtime", "group": "fallback", "parameters": [["self","POSITIONAL_OR_KEYWORD",{"required":true}],["runtime_access_binding","POSITIONAL_OR_KEYWORD",{"default":null}]]},
    {"module": "agent.agent_runtime_helpers", "qualname": "restore_primary_runtime", "group": "fallback", "parameters": [["agent","POSITIONAL_OR_KEYWORD",{"required":true}],["runtime_access_binding","POSITIONAL_OR_KEYWORD",{"default":null}]]},
    {"module": "agent.conversation_loop", "qualname": "_sync_failover_system_message", "group": "fallback", "parameters": [["agent","POSITIONAL_OR_KEYWORD",{"required":true}],["api_messages","POSITIONAL_OR_KEYWORD",{"required":true}],["active_system_prompt","POSITIONAL_OR_KEYWORD",{"required":true}]]},
    {"module": "agent.reasoning_support", "qualname": "resolve_reasoning_support", "group": "critical", "parameters": [["provider","KEYWORD_ONLY",{"required":true}],["model","KEYWORD_ONLY",{"required":true}],["api_mode","KEYWORD_ONLY",{"required":true}],["metadata","KEYWORD_ONLY",{"default":null}]]}
  ],
  "capability_callables": [
    {"module": "run_agent", "qualname": "AIAgent.__init__", "group": "critical", "match": "required_subset", "parameters": [["runtime_intent","POSITIONAL_OR_KEYWORD",{"default":null}]]},
    {"module": "agent.plugin_llm", "qualname": "PluginLlm.complete", "group": "critical", "match": "required_subset", "parameters": [["reasoning_config","KEYWORD_ONLY",{"default":null}]]},
    {"module": "agent.plugin_llm", "qualname": "PluginLlm.complete_structured", "group": "critical", "match": "required_subset", "parameters": [["reasoning_config","KEYWORD_ONLY",{"default":null}]]},
    {"module": "agent.plugin_llm", "qualname": "PluginLlm.acomplete", "group": "critical", "match": "required_subset", "parameters": [["reasoning_config","KEYWORD_ONLY",{"default":null}]]},
    {"module": "agent.plugin_llm", "qualname": "PluginLlm.acomplete_structured", "group": "critical", "match": "required_subset", "parameters": [["reasoning_config","KEYWORD_ONLY",{"default":null}]]},
    {"module": "hermes_cli.models", "qualname": "provider_model_discovery", "group": "critical", "match": "exact", "parameters": [["provider","POSITIONAL_OR_KEYWORD",{"required":true}],["force_refresh","KEYWORD_ONLY",{"default":false}],["resolver_name","KEYWORD_ONLY",{"default":null}]]},
    {"module": "hermes_cli.inventory", "qualname": "build_models_payload", "group": "critical", "match": "required_subset", "parameters": [["ctx","POSITIONAL_OR_KEYWORD",{"required":true}],["picker_hints","KEYWORD_ONLY",{"default":false}],["pricing","KEYWORD_ONLY",{"default":false}],["capabilities","KEYWORD_ONLY",{"default":false}],["refresh","KEYWORD_ONLY",{"default":false}],["discovery_provenance","KEYWORD_ONLY",{"default":false}]]}
  ],
  "record_contracts": [
    {"module": "agent.runtime_intent", "qualname": "RuntimeIntent", "group": "critical", "dataclass_fields": ["source"], "dataclass_frozen": true, "constants": {"RUNTIME_INTENT_CONTRACT_VERSION": 1, "RUNTIME_INTENT_SOURCES": ["config_default","explicit_session","configured_scope","scheduled_pin","batch_pin","internal","auto_projection","unknown"]}},
    {"module": "agent.runtime_access", "qualname": "RuntimeAccessBinding", "group": "critical", "slots": ["provider","model","api_mode","endpoint_identity","auth_identity","credential_pool_identity","local_backend","_base_url","_api_key","_credential_pool","_sealed"], "constants": {"RUNTIME_ACCESS_BINDING_CONTRACT_VERSION": 1}},
    {"module": "agent.reasoning_support", "qualname": "ReasoningSupport", "group": "critical", "dataclass_fields": ["efforts","provider_aliases","provenance","exact"], "dataclass_frozen": true, "constants": {"REASONING_SUPPORT_CONTRACT_VERSION": 1}},
    {"module": "hermes_cli.models", "qualname": "ProviderModelDiscovery", "group": "critical", "dataclass_fields": ["model_ids","model_provenance","provenance_details","live_attempt_status","observed_at","credential_fingerprint"], "dataclass_frozen": true, "constants": {"PROVIDER_MODEL_DISCOVERY_CONTRACT_VERSION": 1, "PROVIDER_MODEL_PROVENANCE_VALUES": ["authenticated_live","validated_contract","stale_live_cache","static_curated","configured_declared","current_offline_fallback"], "PROVIDER_MODEL_LIVE_ATTEMPT_STATUSES": ["not_attempted","succeeded","failed","probe_disabled"]}},
    {"module": "agent.plugin_llm", "qualname": null, "group": "critical", "dataclass_fields": [], "constants": {"PLUGIN_LLM_REASONING_CONTRACT_VERSION": 1}},
    {"module": "agent.turn_finalizer", "qualname": null, "group": "evidence", "dataclass_fields": [], "constants": {"POST_LLM_CALL_PAYLOAD_VERSION": 2}, "required_set_members": {"POST_LLM_CALL_PAYLOAD_FIELDS": ["hook_payload_version","session_id","task_id","turn_id","model","provider","reasoning_config","outcome","outcome_reason","api_calls","tool_iterations","response_transformed"]}}
  ],
  "required_symbols": [
    "tools.delegate_tool.delegate_task",
    "agent.conversation_loop.build_turn_context",
    "hermes_cli.inventory.load_picker_context"
  ],
  "instance_attributes": [
    {"owner": "run_agent.AIAgent", "name": "_fallback_chain", "group": "fallback", "check": "on_live_instance"}
  ]
}
```

Regenerate normalized parameter records from the post-Plan-2 checkout and compare them with the reviewed literals above:

```bash
uv run python scripts/check_auto_routing_compat.py --emit-candidate plugins/auto_routing/auto_routing/assets/contracts/hermes-0.18.2.json
git diff -- plugins/auto_routing/auto_routing/assets/contracts/hermes-0.18.2.json
```

Expected: the script reads `hermes_cli.__version__`, resolves every declared
seam/capability/record, serializes callable parameter name,
`inspect.Parameter.kind.name`, tagged required/default semantics, ordered
dataclass fields plus frozen status, ordered `__slots__`, literal version
constants, and required set members, and exits
0 with no contract diff. Exact wrapped seams and `provider_model_discovery()`
require a complete signature; the AIAgent constructor, PluginLlm methods, and
`build_models_payload()` use the declared required subset so an unrelated additive parameter does not
disable routing. It deliberately
excludes annotations and return reprs so Python 3.11–3.13 typing display changes
cannot disable a compatible host. Non-JSON/non-literal defaults fail generation
for human review rather than falling back to `repr`. If an
earlier task intentionally changed a seam, review and update both the literal
contract and its adapter behavior test in the same commit. Load the JSON with
`importlib.resources.files("auto_routing").joinpath("assets/contracts/hermes-0.18.2.json")`
in standalone mode and the equivalent package-relative resource in bundled
mode; never depend on a repository-relative path.

`SUPPORTED_CONTRACTS` maps only `Version("0.18.2")` to this resource. `_fallback_chain` is instance-only and is checked on the actual agent before Auto fallback installation/use; absence disables only the fallback group and is never resolved as a class symbol. The adapter resolves and validates all seams first and builds wrapper objects without assignment. Under one process lock it installs the critical run/child/manual/access-binding group only when every critical callable, record field/frozen or slot layout, discovery enum/version constant, and PluginLlm reasoning parameter matches. It installs the fallback/primary-restore wrappers only when all five fallback callables (`AIAgent` forwarders, both direct helpers, and prompt synchronization) plus the live instance attribute match; fallback-only drift retains every original fallback-group callable, reports the reduced capability, and makes critical fresh projection leave the Auto agent's native chain empty while preserving its recorded chain only in plugin state. Any critical failure assigns nothing and records a redacted inactive reason.

The versioned post-turn payload is an `evidence` capability group rather than a static-routing wrapper. If its version or required field set drifts, static pre-call routing may remain active, but hook registration, evaluator learning, conservative/autonomous schedulers, and all canary assignment are disabled; `doctor` blocks any non-disabled adaptation mode until the capability returns. Never infer fields from arbitrary hook kwargs. This makes PluginLlm reasoning forwarding, model-discovery provenance and callable semantics, exact reasoning support, RuntimeIntent semantics, shared main/child/fallback/restore access binding, child reasoning, and canonical post-turn evidence explicit standalone host prerequisites instead of symbol-presence guesses.

- [ ] **Step 4: Run compatibility, adapter, route, and fallback regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_compatibility_contract.py \
  tests/plugins/auto_routing/test_adapter_contract.py \
  tests/plugins/auto_routing/test_auto_fallback.py \
  tests/agent/test_plugin_llm.py \
  tests/agent/test_turn_finalizer_hooks.py \
  tests/agent/test_reasoning_support.py \
  tests/agent/test_runtime_access.py \
  tests/hermes_cli/test_provider_live_curated_merge.py \
  tests/gateway/test_agent_cache.py \
  tests/run_agent/test_switch_model_pool_reload_52727.py \
  tests/run_agent/test_provider_fallback.py \
  tests/agent/test_restore_primary_pool_reselect.py \
  tests/tools/test_delegate.py
```

Expected: all selected tests pass and current-host comparison reports `0.18.2 compatible`.

- [ ] **Step 5: Commit the supported-host contract**

```bash
git add plugins/auto_routing/auto_routing/compatibility.py plugins/auto_routing/auto_routing/assets/contracts/hermes-0.18.2.json scripts/check_auto_routing_compat.py plugins/auto_routing/auto_routing/adapters/hermes_0_18.py plugins/auto_routing/plugin.yaml tests/plugins/auto_routing/test_compatibility_contract.py
git diff --cached --check
git commit -m "test(auto-routing): enforce Hermes 0.18.2 compatibility contract"
```

---

### Task 2: Add Verified Migrations, Backups, Restore, and Downgrade Recovery

**Files:**
- Create: `plugins/auto_routing/auto_routing/migrations.py`
- Create: `plugins/auto_routing/auto_routing/backup.py`
- Create: `plugins/auto_routing/auto_routing/maintenance.py`
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Modify: `plugins/auto_routing/auto_routing/config_io.py`
- Modify: `plugins/auto_routing/auto_routing/cli.py`
- Create: `tests/plugins/auto_routing/test_migrations_backup_restore.py`

- [ ] **Step 1: Write failing interrupted-migration, corrupt-backup, and restore tests**

```python
def test_data_migration_creates_verified_backup_before_transform(state_fixture) -> None:
    before = state_fixture.store.canonical_dump()
    result = state_fixture.migrator.migrate(target_version=2)
    assert result.applied_versions == (2,)
    assert result.backup_manifest.database_sha256 == sha256_file(result.backup_path)
    backup_store = RoutingStore(result.backup_path, migrate=False)
    assert backup_store.schema_version == 1
    assert backup_store.canonical_dump() == before
    assert not backup_store.has_column("routing_decisions", "v2_runtime_id")
    assert state_fixture.store.schema_version == 2


def test_interrupted_migration_restores_pre_migration_state(state_fixture) -> None:
    before = state_fixture.store.canonical_dump()
    with pytest.raises(InjectedMigrationFailure):
        state_fixture.migrator.migrate(target_version=2, fault_after_transform=True)
    reopened = RoutingStore(state_fixture.db_path)
    assert reopened.canonical_dump() == before


def test_restore_rejects_checksum_mismatch_without_touching_live_state(state_fixture) -> None:
    backup = state_fixture.backup_service.create("manual")
    backup.database_path.write_bytes(backup.database_path.read_bytes() + b"corrupt")
    before = state_fixture.store.canonical_dump()
    with pytest.raises(BackupValidationError, match="database checksum mismatch"):
        state_fixture.backup_service.restore(backup.manifest_path)
    assert RoutingStore(state_fixture.db_path).canonical_dump() == before


def test_restore_refuses_while_another_process_has_the_store_open(state_fixture) -> None:
    backup = state_fixture.backup_service.create("manual")
    before = state_fixture.store.canonical_dump()
    with live_store_process(state_fixture.profile_home) as other:
        preview = state_fixture.backup_service.preview_restore(backup.manifest_path)
        assert preview.eligible is False
        assert preview.blockers == [f"live_process:{other.pid}:{other.start_token}"]
        with pytest.raises(MaintenanceBusy):
            state_fixture.backup_service.restore(
                backup.manifest_path,
                precondition_hash=preview.precondition_hash,
            )
    assert RoutingStore(state_fixture.db_path).canonical_dump() == before


def test_restore_reopens_current_process_on_new_generation(state_fixture) -> None:
    backup = state_fixture.backup_service.create("manual")
    old_generation = state_fixture.store.maintenance_generation
    result = state_fixture.backup_service.restore_with_preview_apply(backup.manifest_path)
    assert result.maintenance_generation == old_generation + 1
    assert state_fixture.service.store.maintenance_generation == result.maintenance_generation
    assert state_fixture.service.store.integrity_check() == "ok"


def test_preview_hash_survives_quiescing_multiple_local_stores(state_fixture) -> None:
    peer = state_fixture.open_peer_service(optimizer_enabled=False)
    backup = state_fixture.backup_service.create("manual")
    preview = state_fixture.backup_service.preview_restore(backup.manifest_path)

    result = state_fixture.backup_service.restore(
        backup.manifest_path,
        precondition_hash=preview.precondition_hash,
    )

    assert result.accepted_precondition_hash == preview.precondition_hash
    assert state_fixture.service.store.maintenance_generation == result.maintenance_generation
    assert peer.store.maintenance_generation == result.maintenance_generation
    assert state_fixture.service.store.integrity_check() == "ok"
    assert peer.store.integrity_check() == "ok"


def test_failed_store_open_cleans_lease_and_local_registration(state_fixture) -> None:
    failing = state_fixture.new_store(fail_after_lease_publish=True)
    with pytest.raises(InjectedOpenFailure):
        failing.open()

    assert failing.instance_id not in state_fixture.maintenance.local_instance_ids
    assert not state_fixture.maintenance.lease_path(failing.instance_id).exists()
    assert state_fixture.backup_service.preview_restore(
        state_fixture.backup_service.create("after-failed-open").manifest_path,
    ).eligible is True
    assert state_fixture.new_store().open().integrity_check() == "ok"


def test_drain_timeout_reopens_every_local_store_without_replacement(state_fixture) -> None:
    peer = state_fixture.open_peer_service(optimizer_enabled=False)
    backup = state_fixture.backup_service.create("manual")
    before = sha256_file(state_fixture.db_path)
    generation = state_fixture.store.maintenance_generation

    with peer.store.hold_counted_transaction():
        preview = state_fixture.backup_service.preview_restore(backup.manifest_path)
        with pytest.raises(MaintenanceBusy, match="local drain timeout"):
            state_fixture.backup_service.restore(
                backup.manifest_path,
                precondition_hash=preview.precondition_hash,
                drain_timeout=0.01,
            )

    assert sha256_file(state_fixture.db_path) == before
    assert state_fixture.service.store.maintenance_generation == generation
    assert peer.store.maintenance_generation == generation
    assert state_fixture.service.store.accepting_transactions is True
    assert peer.store.accepting_transactions is True
    assert state_fixture.service.store.integrity_check() == "ok"
    assert peer.store.integrity_check() == "ok"


def test_maintenance_restarts_only_previously_enabled_schedulers(state_fixture) -> None:
    enabled = state_fixture.service
    disabled = state_fixture.open_peer_service(optimizer_enabled=False)
    enabled.optimizer.start()
    assert enabled.optimizer.running is True
    assert disabled.optimizer.running is False

    backup = state_fixture.backup_service.create("manual")
    state_fixture.backup_service.restore_with_preview_apply(backup.manifest_path)

    assert enabled.optimizer.running is True
    assert enabled.optimizer.restart_count == 1
    assert disabled.optimizer.running is False
    assert disabled.optimizer.start_count == 0
```

Define `state_fixture` in the test file with a temporary profile home, schema version 1 fixture database, config file, real backup/migration/maintenance services, and an additive data transform that canonicalizes one legacy runtime ID. It can open multiple independently leased services in the same process, exposes counted transactions and scheduler lifecycle counters, and supplies the one-shot `fail_after_lease_publish` fault only to test-injected stores. `live_store_process()` uses `multiprocessing.get_context("spawn")`, opens the real store in a child, returns its PID/process-create-time token over a pipe, and keeps it open until the context exits. All fault flags exist only on test-injected service instances.

- [ ] **Step 2: Run migration/recovery tests and record RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_migrations_backup_restore.py
```

Expected: failures report missing migration/verified restore services.

- [ ] **Step 3: Implement the migration and recovery protocol**

`maintenance.py` owns a profile-local `runtime-leases/` directory and canonical
`maintenance.json`. Before opening SQLite, every `RoutingStore` briefly takes
the existing profile routing lock, refuses an in-progress maintenance marker,
and atomically writes a lease containing only profile ID, random instance ID,
PID, `psutil.Process(pid).create_time()` as the PID-reuse-safe start token,
maintenance generation, opened/last-seen timestamps, and schema version. It
removes the lease on clean close and refreshes `last_seen` during normal store
operations. Stale files are removed only when PID plus start token proves that
process is dead; an inaccessible or indeterminate process is treated as live.
Lease publication plus connection/bootstrap/validation is one exception-safe
open protocol: if anything after publication fails, close any partial
connection, remove only that instance's lease under the routing lock,
unregister it from the process-local coordinator, clear its transaction gate,
and re-raise the original error. A failed open must never leave a ghost lease
or coordinator member that blocks later maintenance.
The process-local coordinator tracks every service/store instance so it can
stop optimizer threads, reject new local transactions, drain in-flight local
transactions, close every local connection, and reopen them after maintenance.
Before stopping background schedulers it records, per service and scheduler,
whether each scheduler was enabled and actually running. Reopen restores stores
for every local member but restarts only schedulers that were enabled and
running before quiescence; disabled or manually stopped schedulers stay stopped.

Schema migration, recovery restore, and downgrade are destructive maintenance.
Their canonical preview hash includes the current maintenance generation,
operation and target hashes, a canonical logical database-state digest from a
short SQLite snapshot (never raw database/WAL file bytes), the config digest, and sorted
other-process `(profile_id, pid, start_token)` lease identities. It deliberately
excludes current-process random instance IDs, lease timestamps, and lease-file
paths, so closing local leases during quiescence cannot invalidate the operator's
preview. Apply takes the routing lock for the entire operation, removes only
proven-dead leases, rechecks the hash before quiescence, quiesces every instance
registered in the current process, and rechecks the same canonical inputs after
quiescence. The routing lock prevents a foreign open between either recheck and
replacement, while a newly registered local instance is included in the same
coordinator drain. If local work does not drain within the bounded
maintenance timeout, reopen/clear the local gate and raise `MaintenanceBusy`
without touching the database; reopen every local store and restore only its
previously running schedulers. If any lease from another live PID/start token remains,
raise `MaintenanceBusy`, reopen the current process on the unchanged database,
and perform no schema/file write. Because `RoutingStore.open()` also takes this
lock before publishing its lease, a new process cannot slip between the recheck
and replacement. Automatic migration on open obeys the same rule and raises
`MigrationRequiresQuiescence` with restart instructions instead of modifying a
database used by another process.

Each ordered transform is a frozen record with `version`, `name`, `checksum`, `upgrade(connection)`, and `verify(connection)`. On open, inspect `PRAGMA user_version`, integrity, and required declarative/additive changes without mutating the database. A brand-new empty database may initialize directly. For any existing database requiring `SCHEMA_SQL`, additive reconciliation, or an ordered transform, take the backup before the first write:

1. after successful quiescence, write `maintenance.json` with state `in_progress`, old generation, target generation, operation, and precondition hash;
2. checkpoint WAL with `PRAGMA wal_checkpoint(TRUNCATE)` after all local connections are closed;
3. compute `timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")`, then use `sqlite3.Connection.backup()` into `get_hermes_home() / "auto-routing" / "backups" / f"{timestamp}-schema-v{current_version}" / "state.db"`;
4. copy config and write canonical `manifest.json` containing profile name, schema/authority/revision IDs, maintenance generation, sizes, and SHA-256 hashes;
5. reopen and integrity-check the backup;
6. run declarative `SCHEMA_SQL` and additive reconciliation, then execute each transform and verifier inside `BEGIN IMMEDIATE`;
7. advance `user_version` and the database's `maintenance_generation` only after every declarative/additive/ordered verification succeeds;
8. atomically publish `maintenance.json` as `stable` at the new generation, reopen all current-process services, publish their new leases, and only then release the routing lock.

On failure, close live connections, validate the backup, atomically move the
failed database aside with
`db_path.with_name(f"{db_path.name}.failed-{timestamp}")`, restore through
`db_path.with_name(f".{db_path.name}.restore-{uuid.uuid4().hex}.tmp")`, call
`os.replace()` only after checksum and `PRAGMA integrity_check`, and reopen.
`downgrade` means restoring the verified automatic backup created before the
requested migration; no reverse transform is invented. Recovery updates the
candidate database to the target maintenance generation before `os.replace`,
so a reopened connection can never acknowledge an old file under a new marker.
On startup, an `in_progress` marker is recoverable only when no other live lease
exists; validate whether the old or candidate file is complete, choose exactly
one by checksum/generation, and finish publishing before opening normal stores.

Register preview/hash-gated commands:

```text
hermes auto-routing backup [--apply --expect-hash HASH]
hermes auto-routing restore MANIFEST [--apply --expect-hash HASH]
hermes auto-routing downgrade SCHEMA_VERSION [--apply --expect-hash HASH]
```

- [ ] **Step 4: Run migration, storage, config, and corruption regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_migrations_backup_restore.py \
  tests/plugins/auto_routing/test_storage.py \
  tests/plugins/auto_routing/test_storage_concurrency.py \
  tests/plugins/auto_routing/test_config_io.py
```

Expected: all selected tests pass; every fault leaves either the original or fully migrated state.

- [ ] **Step 5: Commit verified recovery**

```bash
git add plugins/auto_routing/auto_routing/migrations.py plugins/auto_routing/auto_routing/backup.py plugins/auto_routing/auto_routing/maintenance.py plugins/auto_routing/auto_routing/storage.py plugins/auto_routing/auto_routing/config_io.py plugins/auto_routing/auto_routing/cli.py tests/plugins/auto_routing/test_migrations_backup_restore.py
git diff --cached --check
git commit -m "feat(auto-routing): add verified migration recovery"
```

---

### Task 3: Stress Concurrent Readers, Decisions, Optimizers, and Process Failure

**Files:**
- Create: `tests/plugins/auto_routing/helpers/process_worker.py`
- Create: `tests/plugins/auto_routing/test_multiprocess_stress.py`
- Modify: `plugins/auto_routing/auto_routing/maintenance.py`
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Modify: `plugins/auto_routing/auto_routing/service.py`

- [ ] **Step 1: Write failing real-process stress and kill-recovery tests**

```python
def test_concurrent_processes_never_read_partial_state(process_harness) -> None:
    result = process_harness.run(
        readers=8,
        decision_writers=4,
        optimizers=3,
        operations_per_process=250,
        seed=20260715,
    )
    assert result.process_failures == ()
    assert result.sqlite_integrity == "ok"
    assert result.partial_revision_reads == 0
    assert result.duplicate_operation_decisions == 0
    assert result.simultaneous_optimizer_owners == 0


def test_killed_optimizer_lease_expires_and_work_recovers(process_harness) -> None:
    killed = process_harness.start_optimizer(pause_after_lease=True)
    process_harness.terminate(killed)
    process_harness.clock.advance(seconds=31)
    successor = process_harness.run_optimizer_once()
    assert successor.acquired_lease
    assert successor.published_complete_revision


def test_decision_lock_exhaustion_never_projects_uncommitted_route(process_harness) -> None:
    result = process_harness.run_with_decision_db_locked(retry_attempts=15)
    assert result.status == "store_unavailable"
    assert result.new_decision_rows == 0
    assert result.new_assignment_rows == 0
    assert result.runtime_switches == 0

    reused = process_harness.resume_precommitted_decision_while_locked()
    assert reused.reused_committed_snapshot is True


def test_paused_live_process_blocks_restore_even_with_stale_last_seen(process_harness) -> None:
    holder = process_harness.start_store_holder(pause_heartbeats=True)
    process_harness.clock.advance(seconds=3600)
    blocked = process_harness.preview_and_apply_restore()
    assert blocked.status == "maintenance_busy"
    assert blocked.blocking_start_token == holder.start_token
    assert blocked.database_replaced is False
    process_harness.stop(holder)
    assert process_harness.preview_and_apply_restore().status == "restored"


def test_new_open_waits_and_acknowledges_post_restore_generation(process_harness) -> None:
    restore = process_harness.start_restore(pause_after_maintenance_marker=True)
    opener = process_harness.start_store_holder()
    assert opener.opened_event.wait(timeout=0.2) is False
    process_harness.resume(restore)
    assert opener.opened_event.wait(timeout=5.0) is True
    assert opener.maintenance_generation == restore.published_generation
```

`process_harness` launches `sys.executable tests/plugins/auto_routing/helpers/process_worker.py` with JSON commands over stdin and writes results to per-process JSON files under the temporary profile. It uses `multiprocessing` only for the controllable fake clock server; SQLite access occurs in independent interpreter processes. Maintenance workers expose explicit pause points after lease publication and after the `in_progress` generation marker, never sleep-based races.

- [ ] **Step 2: Run the stress tests and record RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_multiprocess_stress.py
```

Expected: at least one contention/recovery invariant fails before bounded retry and lease recovery are complete.

- [ ] **Step 3: Close all identified concurrency gaps**

Use one connection per process/thread, `busy_timeout=5000`, WAL with fallback reporting, explicit `BEGIN IMMEDIATE` only for short writes, and the existing 15-attempt 20–150 ms jittered retry ceiling. Never hold a transaction across classifier/provider calls. Decision idempotency is enforced by unique operation keys; optimizer exclusivity by the expiring lease; active revisions by complete/checksum join; config by the cross-process file lock.

When bounded decision-write retry is exhausted, return `store_unavailable`; the adapter leaves the live runtime unchanged and creates neither an in-memory nor a deferred routing decision. Last checksum-valid snapshots remain usable only for reports, shadow explanations, and reads of already committed decisions. Optimizer write exhaustion exits without mutation. Best-effort post-turn evidence that cannot be committed is dropped with a redacted local `state_write_failed` event; this plan defines no cross-process deferred-evidence claim or non-SQLite outbox. An optimizer ownership lease remains time-expiring and needs no PID probing. A database-open process lease is deliberately different: it never expires merely because `last_seen` is old, and destructive maintenance removes it only after PID plus process-create-time proves the owner dead. Store open and destructive maintenance both use the same routing lock, while the maintenance generation plus process-local quiescence prevents any connection from surviving across a database replacement.

- [ ] **Step 4: Run stress three times plus durable child regressions**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_multiprocess_stress.py
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_multiprocess_stress.py
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_multiprocess_stress.py
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_canary_integration.py \
  tests/plugins/auto_routing/test_authority_rebase.py \
  tests/tools/test_async_delegation.py
```

Expected: every one of the three independent stress runs passes with SQLite
integrity `ok`; no repetition plugin is required.

- [ ] **Step 5: Commit multi-process recovery**

```bash
git add plugins/auto_routing/auto_routing/maintenance.py plugins/auto_routing/auto_routing/storage.py plugins/auto_routing/auto_routing/service.py tests/plugins/auto_routing/helpers/process_worker.py tests/plugins/auto_routing/test_multiprocess_stress.py
git diff --cached --check
git commit -m "test(auto-routing): harden multi-process state recovery"
```

---

### Task 4: Add Redacted Profile-Local Export and Explicit Import

**Files:**
- Create: `plugins/auto_routing/auto_routing/exchange.py`
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Modify: `plugins/auto_routing/auto_routing/cli.py`
- Create: `tests/plugins/auto_routing/test_exchange.py`

- [ ] **Step 1: Write failing round-trip, redaction, and cross-profile tests**

```python
def test_export_round_trip_preserves_complete_revisions(exchange_fixture) -> None:
    bundle = exchange_fixture.export()
    imported = exchange_fixture.import_into_empty_profile(bundle)
    assert imported.authority_id == exchange_fixture.authority_id
    assert imported.active_revision.canonical_json() == exchange_fixture.active_revision.canonical_json()
    assert imported.runtime_keys[0].auth_identity == "oauth:default"


def test_export_contains_no_content_credentials_or_absolute_home(exchange_fixture) -> None:
    bundle = exchange_fixture.export()
    raw = bundle.read_bytes().lower()
    for forbidden in (
        b"raw prompt",
        b"raw response",
        b"sk-secret-canary-123",
        b"refresh-token-secret-456",
        str(exchange_fixture.hermes_home).encode().lower(),
    ):
        assert forbidden not in raw


def test_cross_profile_import_requires_explicit_flag(exchange_fixture) -> None:
    bundle = exchange_fixture.export()
    with pytest.raises(ImportPolicyError, match="cross-profile import requires explicit approval"):
        exchange_fixture.import_into_profile(bundle, profile_name="work")
```

Define `exchange_fixture` in the test with two profile-local homes, sample decisions/evidence/revisions/lineage, a non-secret `auth_identity="oauth:default"`, sentinel raw content and exact fake credential values `sk-secret-canary-123` and `refresh-token-secret-456` in adjacent Hermes state, and an export path under the test temp directory. Runtime identity field names and non-secret credential-profile identities must survive; only secret material is forbidden.

- [ ] **Step 2: Run exchange tests and record RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_exchange.py
```

Expected: failures report missing bundle exporter/import policy.

- [ ] **Step 3: Implement a canonical versioned bundle**

Write a ZIP with deterministic filenames and timestamps:

```text
manifest.json
authority-summary.json
catalog-sources.jsonl
inventory-observations.jsonl
contextual-estimates.jsonl
revisions.jsonl
experiments.jsonl
decisions.jsonl
evidence.jsonl
lineage.jsonl
```

The manifest contains format version, source profile name, authority hash, schema version, creation time, row counts, per-file SHA-256, and an explicit `excluded_fields` list. Export only typed allowlisted projections; never serialize database rows generically. Import validates ZIP path traversal, sizes, hashes, JSON schemas, runtime keys, authority compatibility, profile policy, and all revision checksums into a sibling temporary database. It then previews exact inserts/conflicts and applies under lock/precondition hash. Cross-profile import requires `--allow-cross-profile-import` and rebases through Stage 5 rather than activating the foreign revision directly.

Register:

```text
hermes auto-routing export PATH [--apply --expect-hash HASH]
hermes auto-routing import PATH [--allow-cross-profile-import] [--apply --expect-hash HASH]
```

- [ ] **Step 4: Run exchange, profile-isolation, and privacy regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_exchange.py \
  tests/plugins/auto_routing/test_profile_isolation.py \
  tests/plugins/auto_routing/test_evidence_privacy.py
```

Expected: all selected tests pass and repeated export of fixed state is byte-identical except the manifest creation time when explicitly varied.

- [ ] **Step 5: Commit safe exchange bundles**

```bash
git add plugins/auto_routing/auto_routing/exchange.py plugins/auto_routing/auto_routing/storage.py plugins/auto_routing/auto_routing/cli.py tests/plugins/auto_routing/test_exchange.py
git diff --cached --check
git commit -m "feat(auto-routing): add redacted routing state exchange"
```

---

### Task 5: Harden Metadata, Paths, Logs, and Local Support Bundles

**Files:**
- Create: `plugins/auto_routing/auto_routing/diagnostics.py`
- Modify: `plugins/auto_routing/auto_routing/catalog.py`
- Modify: `plugins/auto_routing/auto_routing/inventory.py`
- Modify: `plugins/auto_routing/auto_routing/config_io.py`
- Modify: `plugins/auto_routing/auto_routing/service.py`
- Modify: `plugins/auto_routing/auto_routing/cli.py`
- Create: `tests/plugins/auto_routing/test_security_hardening.py`

- [ ] **Step 1: Write failing malicious-input and secret-canary tests**

```python
@pytest.mark.parametrize(
    "payload",
    [
        {"model": "../../config.yaml", "base_url": "https://attacker.invalid"},
        {"provider": "allowed\nINJECTED", "auth_path": "C:/secrets/token"},
        {"license": "MIT", "model": "moa:attacker-controlled"},
        {"model": "local/model", "installed": True, "hardware_compatible": "yes"},
    ],
)
def test_malicious_catalog_fields_cannot_create_executable_runtime(catalog_service, payload) -> None:
    result = catalog_service.import_record(payload, source="external")
    assert not result.executable
    assert result.runtime_key is None


def test_support_bundle_redacts_secret_canaries(diagnostic_fixture) -> None:
    diagnostic_fixture.seed_secret("sk-secret-canary-123")
    bundle = diagnostic_fixture.create_support_bundle()
    assert b"sk-secret-canary-123" not in bundle.read_bytes()


def test_output_path_cannot_escape_profile_home(config_service, tmp_path) -> None:
    with pytest.raises(PathPolicyError):
        config_service.create_backup(tmp_path.parent / "escape")
```

Define fixtures in the test file with an external catalog adapter, verified inventory resolver, temporary profile home, logs/database populated with secret canaries and prompt sentinels, and no network calls.

- [ ] **Step 2: Run security tests and record RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_security_hardening.py
```

Expected: at least one untrusted metadata/path/redaction assertion fails before hardening.

- [ ] **Step 3: Enforce typed provenance and allowlisted diagnostics**

External catalog records can contribute only descriptive capability, benchmark, price, and provenance fields. Executable provider/model/API mode/auth path/base URL always comes from the verified Hermes observation and its non-secret addressable `resolver_name`; resolve it with `resolve_runtime_provider(requested=observation.resolver_name, target_model=...)` and require the returned full access-path identity to match. Reject control characters, path traversal, invalid Unicode normalization, overlong IDs, NaN/infinity, wrong JSON types, MoA, and local install claims not confirmed by the local backend.

Resolve backup/export/support paths and prove they remain inside either the profile routing directory or the operator's explicitly supplied export directory; refuse symlinks/reparse points for internal backup/restore paths. Diagnostic bundles are built from field allowlists and include compatibility status, redacted config summary, schema/integrity result, inventory states/reasons, recent decision IDs, revision lineage, and redacted event codes. They exclude prompts, responses, environment variables, credential fields, endpoint query strings, and raw exception/provider bodies.

Register read-only `hermes auto-routing support --output PATH`; creating the file uses the same preview/hash apply contract because it writes externally. No support bundle is uploaded automatically.

- [ ] **Step 4: Run security, privacy, exchange, and no-network tests**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_security_hardening.py \
  tests/plugins/auto_routing/test_evidence_privacy.py \
  tests/plugins/auto_routing/test_exchange.py \
  tests/plugins/auto_routing/test_catalog.py
```

Expected: all selected tests pass; secret/prompt canaries do not appear in any produced artifact.

- [ ] **Step 5: Commit security hardening**

```bash
git add plugins/auto_routing/auto_routing/diagnostics.py plugins/auto_routing/auto_routing/catalog.py plugins/auto_routing/auto_routing/inventory.py plugins/auto_routing/auto_routing/config_io.py plugins/auto_routing/auto_routing/service.py plugins/auto_routing/auto_routing/cli.py tests/plugins/auto_routing/test_security_hardening.py
git diff --cached --check
git commit -m "fix(auto-routing): harden state and metadata boundaries"
```

---

### Task 6: Make the Fork Plugin Buildable as the Same Standalone Package

**Files:**
- Create: `plugins/auto_routing/auto_routing/plugin.py`
- Create: `plugins/auto_routing/auto_routing/host.py`
- Modify: `plugins/auto_routing/auto_routing/adapters/hermes_0_18.py`
- Modify: `plugins/auto_routing/auto_routing/config_io.py`
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Modify: `plugins/auto_routing/auto_routing/catalog.py`
- Modify: `plugins/auto_routing/auto_routing/backup.py`
- Modify: `plugins/auto_routing/auto_routing/maintenance.py`
- Modify: `plugins/auto_routing/auto_routing/service.py`
- Move: `plugins/auto_routing/skills/auto-routing/SKILL.md` → `plugins/auto_routing/auto_routing/assets/skills/auto-routing/SKILL.md`
- Modify: `plugins/auto_routing/__init__.py`
- Create: `plugins/auto_routing/pyproject.toml`
- Create: `plugins/auto_routing/LICENSE`
- Create: `plugins/auto_routing/THIRD_PARTY_NOTICES.md`
- Create: `plugins/auto_routing/EXTRACTION.md`
- Modify: `pyproject.toml`
- Modify: `MANIFEST.in`
- Create: `scripts/check_auto_routing_boundary.py`
- Create: `tests/plugins/auto_routing/test_extraction_boundary.py`
- Create: `tests/plugins/auto_routing/test_standalone_package.py`
- Modify: `tests/plugins/auto_routing/test_migrations_backup_restore.py`

- [ ] **Step 1: Write failing boundary, artifact, entry-point, and notice tests**

```python
def test_private_hermes_imports_are_confined_to_boundary() -> None:
    core = Path("plugins/auto_routing/auto_routing")
    boundary_modules = (
        "config_io.py",
        "storage.py",
        "catalog.py",
        "backup.py",
        "maintenance.py",
        "service.py",
    )
    assert all((core / relative).is_file() for relative in boundary_modules)
    assert scan_private_imports(core, required_core_modules=boundary_modules) == []


def test_standalone_wheel_contains_manifest_skill_contract_and_notices(built_plugin_wheel) -> None:
    names = set(zipfile.ZipFile(built_plugin_wheel).namelist())
    assert any(name.endswith("assets/skills/auto-routing/SKILL.md") for name in names)
    assert any(name.endswith("assets/contracts/hermes-0.18.2.json") for name in names)
    assert any(name.endswith("THIRD_PARTY_NOTICES.md") for name in names)


def test_entry_point_registers_same_plugin_in_clean_process(installed_plugin_env) -> None:
    result = installed_plugin_env.run(
        "python",
        "-c",
        "from importlib.metadata import entry_points; "
        "ep=next(e for e in entry_points(group='hermes_agent.plugins') if e.name=='auto-routing'); "
        "print(ep.load().__name__)",
    )
    assert result.stdout.strip() == "auto_routing.plugin"


def test_core_services_run_with_injected_standalone_host_ports(fake_host_ports) -> None:
    service = AutoRoutingService.from_host_ports(fake_host_ports)
    assert service.inventory.refresh().runtimes
    assert service.config.preview_update(fake_host_ports.valid_proposal).before_sha256
    service.store.record_decision(fake_host_ports.sample_decision)

    backup = service.backups.create("standalone-boundary")
    fake_host_ports.seed_proven_dead_lease()
    preview = service.backups.preview_restore(backup.manifest_path)
    result = service.backups.restore(
        backup.manifest_path,
        precondition_hash=preview.precondition_hash,
    )

    assert result.accepted_precondition_hash == preview.precondition_hash
    assert service.store.get_decision(fake_host_ports.sample_decision.id) is not None
    assert fake_host_ports.quiescence.events == [
        ("quiesce", service.store.instance_id),
        ("reopen", result.maintenance_generation),
    ]
    assert fake_host_ports.process_liveness.checked_identities
```

`built_plugin_wheel` runs `uv build plugins/auto_routing --wheel --out-dir TEMP_DIR`. `installed_plugin_env` creates a temporary venv with `system_site_packages=True`, installs only that wheel without dependencies, and executes the entry-point check. `scan_private_imports` is imported from the script module and must fail if any required core module is missing from its scan. `fake_host_ports` uses only temporary paths and recording fake liveness/quiescence ports; it provides no importable Hermes module and performs no network call. Extend the Task 2 maintenance fixture to build those same protocol-shaped path, liveness, and quiescence fakes so the extraction refactor cannot fall back to Hermes globals.

- [ ] **Step 2: Run extraction tests and record RED**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_extraction_boundary.py \
  tests/plugins/auto_routing/test_standalone_package.py \
  tests/plugins/auto_routing/test_migrations_backup_restore.py
```

Expected: failures report missing entry-point package/assets and boundary tooling.

- [ ] **Step 3: Consolidate registration and declare the standalone artifact**

Before moving registration, add frozen protocols in `auto_routing/host.py` for profile/config paths, managed-config write authorization, executable inventory/runtime resolution, exact reasoning support, catalog sources, process-liveness checks, and process-local service quiescence/reopen. The quiescence port must return an opaque snapshot of the local stores and scheduler states it stopped; its reopen call consumes that snapshot plus the published generation, which keeps backup/maintenance core code independent of service implementation details. Refactor `config_io.py` to perform its own ruamel round-trip plus standard-library lock/temp-file/fsync/`os.replace` mechanics against injected paths/guard; refactor `storage.py` to own SQLite WAL fallback and additive-column logic; and inject profile paths and liveness/quiescence ports into `backup.py` and `maintenance.py`. Move Hermes/models.dev catalog source construction and every `hermes_constants`, `hermes_state`, `hermes_cli.*`, `agent.*`, `tools.*`, or `run_agent` import into `plugin.py` or `adapters/hermes_0_18.py`. Core `catalog.py` accepts `CatalogSource` protocols and core `service.py` accepts a complete `HostPorts`; neither discovers Hermes globals. The extraction acceptance test must traverse inventory/catalog, config, storage, backup creation, maintenance preview/apply, liveness, quiescence, and reopen through those ports, and the Task 2 multi-store/drain/scheduler tests remain green against the injected construction.

Move all `register(ctx)` logic into `auto_routing/plugin.py`. It builds the Hermes 0.18.2 host ports/adapters and injects them into `AutoRoutingService`. Bundled `plugins/auto_routing/__init__.py` contains only:

```python
from .auto_routing.plugin import register

__all__ = ["register"]
```

The standalone project declares this complete build/package boundary:

```toml
[build-system]
requires = ["setuptools>=77.0,<83"]
build-backend = "setuptools.build_meta"

[project]
name = "hermes-auto-routing"
version = "0.1.0"
description = "Cache-safe configurable Auto model routing for Hermes Agent"
readme = "README.md"
requires-python = ">=3.11,<3.14"
license = "MIT"
license-files = ["LICENSE", "THIRD_PARTY_NOTICES.md"]
dependencies = [
  "packaging==26.0",
  "psutil==7.2.2",
  "pydantic==2.13.4",
  "ruamel.yaml==0.18.17",
]

[project.entry-points."hermes_agent.plugins"]
"auto-routing" = "auto_routing.plugin"

[tool.setuptools.packages.find]
where = ["."]
include = ["auto_routing", "auto_routing.*"]

[tool.setuptools.package-data]
auto_routing = [
  "assets/skills/*/SKILL.md",
  "assets/contracts/*.json",
]
```

The contract's only canonical copy is
`auto_routing/assets/contracts/hermes-0.18.2.json`. Compatibility and skill
registration resolve package files relative to `compatibility.py` and
`plugin.py`, so the same code works under bundled package name
`plugins.auto_routing.auto_routing` and standalone package name
`auto_routing`. At the root distribution, add
`"**/assets/contracts/*.json"`, `"**/THIRD_PARTY_NOTICES.md"`,
`"**/LICENSE"`, and `"**/EXTRACTION.md"` to the existing `plugins` package-data
array. Keep Plan 1's `recursive-include plugins SKILL.md` and add these exact
sdist rules to `MANIFEST.in`:

```text
recursive-include plugins/auto_routing/auto_routing/assets *.json SKILL.md
include plugins/auto_routing/LICENSE plugins/auto_routing/THIRD_PARTY_NOTICES.md plugins/auto_routing/EXTRACTION.md
```

Do not maintain a second contract or skill copy.

The AST boundary allows private Hermes imports only in `auto_routing/plugin.py` and `auto_routing/adapters/*.py`. All other modules may import only the plugin's host protocols/domain plus public third-party/standard-library packages. The boundary test names `config_io.py`, `storage.py`, `catalog.py`, `backup.py`, and `maintenance.py` explicitly so an allowlist regression cannot hide them, and the injected-port test exercises each without importing Hermes. `LICENSE` is an exact copy of the repository MIT license. `THIRD_PARTY_NOTICES.md` states that `b3nw/hermes-delegate-routing` (MIT, copyright 2026 b3nw) informed the guarded standalone-plugin approach and that no source was copied at plan creation; if implementation reuses source, update the notice with exact files/commit and retain its license header before commit.

`EXTRACTION.md` lists the seven generic Hermes capabilities that remain
host-side: plugin-LLM reasoning forwarding; per-model authenticated-live versus
contract/static discovery provenance; exact content-free per-model reasoning
support; content-free `RuntimeIntent` provenance at agent construction
boundaries; one exact memory-only runtime-access binding shared by main switches, child
construction, direct fallback, and primary restoration; the internal-only
delegated-child reasoning override (with an unchanged model-visible tool
schema); and versioned canonical post-turn hook metadata.
It also documents copying this directory into a new repository, running
boundary/package/host tests, preserving git authorship, and not publishing
until the user requests it.

- [ ] **Step 4: Build both root and standalone artifacts and run loader tests**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_extraction_boundary.py \
  tests/plugins/auto_routing/test_standalone_package.py \
  tests/plugins/auto_routing/test_migrations_backup_restore.py \
  tests/test_plugin_skills.py \
  tests/test_packaging_metadata.py
uv build --wheel --sdist
uv build plugins/auto_routing --wheel --sdist
```

Expected: tests pass and both build commands produce wheel/sdist artifacts containing the skill, compatibility contract, license, and notice.

- [ ] **Step 5: Commit the extraction-ready package**

```bash
git add plugins/auto_routing pyproject.toml MANIFEST.in scripts/check_auto_routing_boundary.py tests/plugins/auto_routing/test_extraction_boundary.py tests/plugins/auto_routing/test_standalone_package.py tests/plugins/auto_routing/test_migrations_backup_restore.py
git diff --cached --check
git commit -m "build(auto-routing): make plugin extraction ready"
```

---

### Task 7: Gate Clean Installation and Explicit Live Smoke Verification

**Files:**
- Create: `scripts/auto_routing_live_smoke.py`
- Create: `tests/plugins/auto_routing/test_clean_install_smoke.py`
- Create: `tests/plugins/auto_routing/test_live_smoke_guard.py`
- Modify: `plugins/auto_routing/README.md`
- Create: `website/docs/user-guide/plugins/auto-routing.md`

- [ ] **Step 1: Write failing clean-install and billable-call guard tests**

```python
def test_clean_install_loads_skill_and_routes_fake_endpoint(clean_install) -> None:
    result = clean_install.run_fake_endpoint_smoke()
    assert result.plugin_status == "active"
    assert result.skill_name == "auto-routing:auto-routing"
    assert result.first_call_runtime == result.recorded_decision_runtime
    assert result.system_prompt_hashes[0] == result.system_prompt_hashes[1]


def test_live_smoke_refuses_without_explicit_target_and_billable_ack(script_runner) -> None:
    missing = script_runner("scripts/auto_routing_live_smoke.py")
    assert missing.returncode == 2
    assert "--target and --allow-billable are required" in missing.stderr


def test_live_smoke_refuses_unverified_target(script_runner) -> None:
    result = script_runner(
        "scripts/auto_routing_live_smoke.py",
        "--target",
        "not-configured|model|chat_completions|api_key",
        "--allow-billable",
        "--max-estimated-cost-usd",
        "0.05",
    )
    assert result.returncode == 2
    assert "target is not verified in the current profile" in result.stderr
```

`clean_install` builds the standalone wheel, creates a temporary venv/profile, installs Hermes from the current checkout plus the plugin wheel, starts a local fake OpenAI-compatible HTTP endpoint, enables the plugin in shadow then active with exact preview hashes, and exercises a two-turn session and delegated child. `script_runner` uses a temporary empty profile and never reaches a provider call.

- [ ] **Step 2: Run clean-install/live-guard tests and record RED**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_clean_install_smoke.py \
  tests/plugins/auto_routing/test_live_smoke_guard.py
```

Expected: failures report missing smoke script or clean-install workflow.

- [ ] **Step 3: Implement safe smoke flows and complete operator docs**

The live script accepts:

```text
--target RUNTIME_STABLE_ID
--allow-billable
--max-estimated-cost-usd AMOUNT   # required, positive, at most policy ceiling
--prompt "Return exactly AUTO_ROUTING_SMOKE_OK"
```

It loads the current profile's verified inventory, requires an exact target match, confirms finite price and policy compliance, reserves the stated overhead/task estimate, prints a final cost/target confirmation, and requires interactive `yes` unless `--non-interactive-confirm EXACT_RUNTIME_ID` matches. It performs one bounded no-tool completion, records no prompt in routing state, reconciles usage, and exits nonzero unless the exact sentinel is returned. It never probes or downloads anything.

Document installation, setup interview, mode transitions, supported Hermes version, inventory meanings, why inaccessible models are absent, the Stage 1 previewed/cost-bounded `verify-runtime` approval flow, static routing, evidence, automatic optimizer triggers, canaries, autonomy, MoA exclusion, cron/batch pinned defaults, backup/restore/export/import, compatibility drift, support bundles, privacy, and the separate explicit live smoke. The website page links to the plugin README as the fork-local authoritative operations reference.

- [ ] **Step 4: Run the complete release gate without billable calls**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing
uv run --extra dev python -m pytest -q \
  tests/agent/test_plugin_llm.py \
  tests/test_plugin_skills.py \
  tests/test_packaging_metadata.py \
  tests/hermes_cli/test_runtime_provider_resolution.py \
  tests/gateway/test_agent_cache.py \
  tests/tools/test_delegate.py \
  tests/tools/test_async_delegation.py \
  tests/run_agent/test_provider_fallback.py \
  tests/run_agent/test_fallback_reasoning_override.py
uv run --extra dev ruff check plugins/auto_routing tests/plugins/auto_routing scripts/check_auto_routing_compat.py scripts/check_auto_routing_boundary.py scripts/auto_routing_live_smoke.py
uv run python scripts/check_auto_routing_compat.py --check
uv run python scripts/check_auto_routing_boundary.py --check
uv build --wheel --sdist
uv build plugins/auto_routing --wheel --sdist
git diff --check
```

Expected: all tests pass, compatibility reports exact Hermes `0.18.2`, boundary scan and Ruff report no errors, both artifacts build with required assets, and `git diff --check` emits no output. Do not execute the billable live command as part of automation.

- [ ] **Step 5: Optionally run one user-approved live target, then commit docs and smoke gates**

Only after the operator explicitly names a verified target and accepts cost:

```bash
read -r -p "Paste the user-approved stable_id from 'hermes auto-routing inventory --json': " RUNTIME_STABLE_ID
test -n "$RUNTIME_STABLE_ID"
uv run python scripts/auto_routing_live_smoke.py \
  --target "$RUNTIME_STABLE_ID" \
  --allow-billable \
  --max-estimated-cost-usd 0.05
```

Expected: the script shows the exact provider/model/reasoning/auth-path identity, receives final confirmation, prints `AUTO_ROUTING_SMOKE_OK`, and reports reconciled usage. If approval is not granted, record `live smoke not run; automated clean-install fake-endpoint gate passed` in the handoff—this does not invalidate the non-live release gate.

```bash
git add scripts/auto_routing_live_smoke.py tests/plugins/auto_routing/test_clean_install_smoke.py tests/plugins/auto_routing/test_live_smoke_guard.py plugins/auto_routing/README.md website/docs/user-guide/plugins/auto-routing.md
git diff --cached --check
git commit -m "docs(auto-routing): complete hardening and extraction runbook"
```

---

### Task 8: Freeze and Run the 500-Task Portfolio Acceptance Benchmark

**Files:**
- Create: `benchmarks/auto_routing/manifest.yaml`
- Create: `benchmarks/auto_routing/cases.jsonl`
- Create: `benchmarks/auto_routing/runner.py`
- Create: `benchmarks/auto_routing/scorers.py`
- Create: `tests/benchmarks/test_auto_routing_manifest.py`
- Create: `tests/benchmarks/test_auto_routing_runner.py`
- Create: `tests/plugins/auto_routing/test_portfolio_acceptance.py`
- Modify: `plugins/auto_routing/README.md`
- Modify: `website/docs/user-guide/plugins/auto-routing.md`

**Interfaces:**
- Produces immutable `AUTO_ROUTING_BENCHMARK_SCHEMA = "hermes.auto-routing-benchmark.v1"`, `BenchmarkManifest`, `BenchmarkCase`, `BenchmarkRun`, `RoutingPolicyResult`, `SafetySliceResult`, and `compare_portfolio_gate()`.
- Consumes the final `AutoRoutingService`, item #6-compatible hard-policy fixtures, item #12-compatible terminal outcome labels and independent scorers, recorded `RoutingDecision`/`EvidenceEvent` data, and the cache-lineage observations already exposed by the Hermes adapter.
- Adds no runtime hook, model-visible tool, paid probe, telemetry, or adaptive write path; the benchmark runner is an offline CLI/test surface and writes only content-redacted run artifacts beneath its explicit output directory.

- [ ] **Step 1: Write RED manifest and denominator tests**

```python
def test_frozen_manifest_has_exact_denominator_strata_and_baselines():
    manifest, cases = load_frozen_corpus()
    assert len(cases) == 500
    assert sum(manifest.strata.values()) == 500
    assert set(manifest.baselines) == {
        "strongest_model_only", "static_cheap_first", "frontier_auxiliary_only",
    }
    assert manifest.gates.max_quality_gap_pp == 2
    assert manifest.gates.minimum_cost_reduction == 0.30
    assert manifest.gates.maximum_privacy_residency_violations == 0
    assert manifest.gates.maximum_primary_cache_identity_changes == 0


def test_every_case_has_independent_end_state_scorer_and_safety_labels():
    _, cases = load_frozen_corpus()
    assert all(case.scorer_id and case.expected_terminal_classes for case in cases)
    assert all(case.stratum and case.max_cost_usd_micros >= 0 for case in cases)
    assert any(case.irreversible_or_high_risk for case in cases)
```

Freeze exact case IDs, input/artifact hashes, scorer versions, authorized provider/model inventory, pricing snapshot, task stratum, data/residency class, required modality/tools, maximum budget, risk label, and deterministic replay fixture before any candidate run. Keep raw private prompts out of git: corpus cases use synthetic/public inputs or encrypted user-supplied fixture references whose plaintext is resolved only by the local runner. The manifest records the frozen hash and rejects changed cases, scorers, prices, or safety labels.

- [ ] **Step 2: Run RED**

Run: `uv run --extra dev python -m pytest -q tests/benchmarks/test_auto_routing_manifest.py tests/benchmarks/test_auto_routing_runner.py`

Expected: FAIL because `benchmarks.auto_routing`, its frozen manifest, and its 500 cases do not exist.

- [ ] **Step 3: Implement deterministic replay and independent scoring**

```python
@dataclass(frozen=True)
class PortfolioGate:
    quality_gap_pp: Decimal
    cost_reduction: Decimal
    cost_per_verified_success_delta: Decimal
    privacy_residency_violations: int
    primary_cache_identity_changes: int
    high_risk_regressions: int
    escalation_brier_delta_vs_cheap_first: Decimal
    escalation_brier_delta_vs_frontier_aux: Decimal

    @property
    def passed(self) -> bool:
        return (
            self.quality_gap_pp <= Decimal("2")
            and self.cost_reduction >= Decimal("0.30")
            and self.cost_per_verified_success_delta <= 0
            and self.privacy_residency_violations == 0
            and self.primary_cache_identity_changes == 0
            and self.high_risk_regressions == 0
            and self.escalation_brier_delta_vs_cheap_first < 0
            and self.escalation_brier_delta_vs_frontier_aux < 0
        )
```

Run all four policies against the same frozen ordered cases, provider-response fixtures, clock, price snapshot, and scorer versions. Attribute integer cost micros from recorded usage; do not impute silent successes. An item #12-compatible scorer—not the routed model, classifier, learner, or selector—assigns `verified`, `completed_unverified`, `failed`, `blocked`, or `unknown_effect`. Compute verified success on the full denominator, cost per verified success with explicit zero-success handling, Wilson intervals by stratum, irreversible/high-risk deltas case-by-case, and Brier score plus reliability bins for every escalation opportunity. A missing decision, receipt, usage record, cache identity, policy explanation, or scorer result is a failed case, never an exclusion.

- [ ] **Step 4: Prove policy, cache, and replay safety on the real composition path**

Use a temporary `HERMES_HOME`, real plugin loading, real SQLite/WAL stores, real config compilation, and the Hermes adapter with only model/network responses substituted by frozen fixtures. Assert each candidate was eligible under item #6-compatible hard authority, every remote disclosure matched its residency label, each primary conversation retained one provider/model/runtime-access identity, auxiliary escalation preserved artifact hashes and lineage, and rerunning with the same seed produces byte-identical decisions and aggregate report. Fault runs cover interruption after decision persistence, missing usage, scorer crash, stale price/inventory hash, partial evidence, and adapter incompatibility; each must resume deterministically or fail the affected case closed.

- [ ] **Step 5: Run GREEN and the complete portfolio gate**

Run:

```bash
uv run --extra dev python -m pytest -q \
  tests/benchmarks/test_auto_routing_manifest.py \
  tests/benchmarks/test_auto_routing_runner.py \
  tests/plugins/auto_routing/test_portfolio_acceptance.py
uv run --extra dev python -m benchmarks.auto_routing.runner \
  --manifest benchmarks/auto_routing/manifest.yaml \
  --cases benchmarks/auto_routing/cases.jsonl \
  --policies strongest_model_only,static_cheap_first,frontier_auxiliary_only,candidate \
  --seed 20260716 \
  --output .artifacts/auto-routing-portfolio
```

Expected: PASS with exactly 500 scored cases per policy; candidate verified success is within two absolute percentage points of strongest-model-only, model cost is at least 30% lower, cost per verified success is no higher, irreversible/high-risk regressions and privacy/residency violations are zero, primary cache identity changes are zero, and candidate escalation Brier score is lower than both static baselines. If configured verified runtimes cannot execute the frozen matrix, report `proof_not_run` with exact missing runtime identities; never shrink the denominator or claim acceptance.

- [ ] **Step 6: Document reproduction and commit**

Document corpus provenance, local encrypted-fixture setup, exact baseline definitions, metric formulas, exclusions (none after freezing), safety slices, result artifact schema, independent scorer ownership, cost snapshot, and how to rerun without billable calls. State that live-provider confirmation is a separately approved follow-up and cannot replace the deterministic acceptance corpus.

```bash
git add benchmarks/auto_routing tests/benchmarks/test_auto_routing_manifest.py tests/benchmarks/test_auto_routing_runner.py tests/plugins/auto_routing/test_portfolio_acceptance.py plugins/auto_routing/README.md website/docs/user-guide/plugins/auto-routing.md
git diff --cached --check
git commit -m "test(auto-routing): add frozen portfolio acceptance gate"
```
