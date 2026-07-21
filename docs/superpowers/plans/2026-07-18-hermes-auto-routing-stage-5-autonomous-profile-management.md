# Hermes Auto Routing Stage 5 Autonomous Profile Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, local-only control plane that automatically maintains primary challengers and fallback order inside existing route profiles from verified local inventory and signed ranking packs.

**Architecture:** The new global configuration validates only trust and operating limits; a separate management state machine holds profile-local epochs, revisions, canary assignments, cooldowns, receipts, and leases. A pure reconciler ranks only a persisted verified inventory snapshot against a locally verified Ed25519 ranking pack, while a locked config-write saga records a reversible revision before changing canonical YAML. Management canaries reuse Stage 4's deterministic math but have distinct storage and never mutate `RouteProfile.adaptation` or `AdaptiveProfileControl`.

**Tech Stack:** Python 3.11+, Pydantic v2 frozen models, SQLite, PyYAML/config lock helpers, `cryptography` Ed25519, existing Hermes cron `no_agent` scripts, pytest/uv.

## Global Constraints

- The global `autonomous_profile_management` control plane is disabled by default; when disabled, every Stage 5 path is read-only.
- Stage 5 may only add, remove, and reorder approved primary challengers and fallbacks within an existing profile. It must never create, split, merge, delete, or rename profiles.
- Candidates may come only from the current persisted inventory snapshot when they are configured-and-verified or already-installed-and-runnable locally. Reconciliation must never refresh inventory, refresh a catalog, download a model, enable/install a provider, query the network, make a paid probe, emit telemetry, invoke an evaluator, or invoke MoA.
- Ranking packs are versioned files under the active Hermes profile, signed with a configured Ed25519 trust key, and verified before candidate ranking. Invalid, expired, unsigned, untrusted, malformed, or out-of-root packs produce a no-change hold.
- Store only content-free metadata: stable IDs/fingerprints, canonical hashes, bounded reason codes, counters, timestamps, compact scores, and receipt checksums. Never persist prompts, responses, task text, credentials, endpoint identities, raw provider payloads, or raw ranking-pack content.
- Direct user edits win. A changed authority cancels stale management work, increments the affected management epoch, and must never be overwritten by a planned reconciliation.
- Existing routing snapshots remain authoritative. Active, resumed, compressed, fixed, manual, recovered, and replayed work must preserve the exact route and fallback snapshot already recorded. Only fresh and eligible delegated decisions may receive a new management assignment.
- Stage 5 has a distinct management canary/control state. It may reuse the pure deterministic Stage 4 canary-arm, learner, confidence, and guardrail functions, but it must not read, write, enable, freeze, reset, or otherwise modify any `ProfileAdaptationSettings` or `AdaptiveProfileControl` state.
- Every automatic profile mutation is a canonical, reversible management revision under `profile_config_lock`/`locked_update(..., allow_active=True)`. A per-profile UTC daily admission cap counts a revision once; promotion, rollback, recovery, and retry of that revision do not consume another slot.
- Global enablement, schedule/trust/daily-cap settings, and freeze/unfreeze are guarded preview-then-apply controls. Individual eligible reconciliations happen automatically once the global control is enabled and scheduled.
- Use the existing `no_agent` cron mechanism for scheduled reconciliation. Do not create a background daemon, a model-visible tool, or a new user-facing `HERMES_*` setting.
- Validation must include focused unit, migration, concurrency, security, fresh-session, delegation, gateway, TUI, Windows/local-capability, and end-to-end tests. Use `uv run --with pytest pytest ...` on Windows when the local virtual environment is unavailable.

---

## File Map

| Path | Responsibility |
| --- | --- |
| `plugins/auto_routing/auto_routing/models.py` | Frozen Stage 5 config, ranking, management state, revision, patch, assignment, and decision-snapshot contracts. |
| `plugins/auto_routing/auto_routing/config.py` | Canonical serialization and authority hashing for the new global configuration subtree. |
| `plugins/auto_routing/auto_routing/ranking_pack.py` | Root-contained, deterministic Ed25519 ranking-pack parsing, expiry/trust verification, and content-free metadata projection. |
| `plugins/auto_routing/auto_routing/management.py` | Pure inventory eligibility, score calculation, desired ordering, mutation planning, canary assessment, and reason-code logic. |
| `plugins/auto_routing/auto_routing/storage.py` | Schema v8 management tables, immutable records, CAS control/profile state, leases, daily-cap accounting, and durable recovery receipts. |
| `plugins/auto_routing/auto_routing/config_io.py` | One locked config mutation/recovery helper that uses the existing atomic preview/backup/replace/restore protocol. |
| `plugins/auto_routing/auto_routing/service.py` | Management status/history/control APIs, reconciliation orchestration, recovery, routing-boundary assignment, and post-turn advancement. |
| `plugins/auto_routing/auto_routing/cli.py` | `manage` command metadata, guarded preview/apply dispatch, read-only reports, and safe scheduled reconciliation invocation. |
| `plugins/auto_routing/auto_routing/management_cron.py` | Installed profile-local Python script that invokes only `hermes auto-routing manage reconcile --scheduled --json` and emits content-free output. |
| `plugins/auto_routing/README.md` | User-facing configuration, trust-pack, scheduling, controls, safeguards, and snapshot semantics. |
| `plugins/auto_routing/skills/auto-routing/SKILL.md` | Assistant-facing setup/edit/inspection workflow; must require preview-first controls and explicit inventory verification. |
| `tests/plugins/auto_routing/test_management_models.py` | Model/config defaults, validation, and content-free restrictions. |
| `tests/plugins/auto_routing/test_ranking_pack.py` | Pack signatures, expiry, key trust, root containment, and no-network parsing behavior. |
| `tests/plugins/auto_routing/test_management_planner.py` | Deterministic eligibility, scoring, ordering, mutation safety, cap, and hold behavior. |
| `tests/plugins/auto_routing/test_management_storage.py` | v8 migration, immutable records, CAS, state transitions, receipts, and cap accounting. |
| `tests/plugins/auto_routing/test_management_storage_concurrency.py` | Independent-connection lease, stale-authority, config-receipt, and cap races. |
| `tests/plugins/auto_routing/test_management_reconciler.py` | Locked YAML revision/recovery, manual-edit precedence, canary/promotion/rollback orchestration. |
| `tests/plugins/auto_routing/test_management_assignment.py` | Fresh/delegation canary assignment and snapshot preservation across every excluded route boundary. |
| `tests/plugins/auto_routing/test_management_cli.py` | `manage` command parsing, previews, guarded application, scheduled mode, and content-free reports. |
| `tests/plugins/auto_routing/test_management_cron.py` | Script installation and existing no-agent cron job lifecycle. |
| `tests/plugins/auto_routing/test_stage5_management_e2e.py` | Fresh session, delegation, gateway, TUI, complete lifecycle, and Windows local-runtime boundary tests. |
| `tests/plugins/auto_routing/test_management_security.py` | Pack/path/signature/record hygiene and forbidden-side-effect regression tests. |

### Task 1: Frozen configuration and management record contracts

**Files:**
- Modify: `plugins/auto_routing/auto_routing/models.py:458-960,1361-1460`
- Modify: `plugins/auto_routing/auto_routing/config.py:28-75`
- Test: `tests/plugins/auto_routing/test_management_models.py`

**Interfaces:**
- Consumes: `RuntimeKey`, `RoutingTarget`, `RouteProfile`, `AutoRoutingConfig`, `RoutingDecision`, `FrozenModel`, `RuntimeStableId`, and `CanonicalTimestamp` from `models.py`.
- Produces: `AutonomousProfileManagementSettings`, `RankingPackTrust`, `RankingPackMetadata`, `ManagementPatch`, `ManagementRevision`, `ManagementProfileState`, `ManagementControl`, `ManagementCanaryAssignment`, `ManagementLifecycleEvent`, `ManagementDecisionSnapshot`, and `management_authority_revision(config: AutoRoutingConfig) -> str`.

- [ ] **Step 1: Write model and canonical-config tests before adding the fields**

```python
def test_management_is_disabled_by_default(valid_config: dict[str, object]) -> None:
    config = AutoRoutingConfig.model_validate(valid_config)
    assert config.autonomous_profile_management.enabled is False
    assert config.autonomous_profile_management.daily_change_limit == 1


def test_management_records_reject_raw_or_secret_content() -> None:
    with pytest.raises(ValidationError, match="content-free"):
        ManagementPatch.model_validate({
            "profile_id": "coding",
            "before_runtime_ids": ("a" * 64,),
            "after_runtime_ids": ("b" * 64,),
            "reason_codes": ("ranking_upgrade",),
            "forbidden_payload": "sk-secret-sentinel",
        })


def test_management_config_changes_canonical_authority(valid_config: dict[str, object]) -> None:
    base = AutoRoutingConfig.model_validate(valid_config)
    enabled = AutoRoutingConfig.model_validate({
        **base.model_dump(mode="json", by_alias=True),
        "autonomous_profile_management": {
            "enabled": True,
            "ranking_pack": {
                "ranking_pack_path": "auto-routing/ranking-packs/current.json",
                "trusted_ed25519_public_keys": (TEST_PUBLIC_KEY_B64,),
            },
            "daily_change_limit": 2,
            "schedule": "17 */6 * * *",
        },
    })
    assert management_authority_revision(base) != management_authority_revision(enabled)
```

- [ ] **Step 2: Run the focused tests and confirm they fail because Stage 5 models do not exist**

Run: `uv run --with pytest pytest tests/plugins/auto_routing/test_management_models.py -q`

Expected: FAIL with an import/attribute error for `AutonomousProfileManagementSettings`, `ManagementPatch`, or `management_authority_revision`.

- [ ] **Step 3: Add the frozen models and validate their bounded, content-free fields**

```python
class RankingPackTrust(FrozenModel):
    ranking_pack_path: NonEmptyString
    trusted_ed25519_public_keys: Annotated[tuple[NonEmptyString, ...], Field(min_length=1, max_length=8)]


class AutonomousProfileManagementSettings(FrozenModel):
    enabled: bool = False
    ranking_pack: RankingPackTrust | None = None
    daily_change_limit: Annotated[int, Field(ge=1, le=10, strict=True)] = 1
    schedule: NonEmptyString = "17 */6 * * *"

    @model_validator(mode="after")
    def require_trust_when_enabled(self) -> "AutonomousProfileManagementSettings":
        if self.enabled and self.ranking_pack is None:
            raise ValueError("enabled autonomous profile management requires ranking_pack trust")
        return self


class ManagementPatch(FrozenModel):
    profile_id: ProfileIdentifier
    before_runtime_ids: Annotated[tuple[RuntimeStableId, ...], Field(min_length=1, max_length=MAX_DECISION_CANDIDATES)]
    after_runtime_ids: Annotated[tuple[RuntimeStableId, ...], Field(min_length=1, max_length=MAX_DECISION_CANDIDATES)]
    reason_codes: Annotated[tuple[AuthorityLabel, ...], Field(min_length=1, max_length=16)]


class ManagementDecisionSnapshot(FrozenModel):
    management_revision_id: DurableIdentifier | None = None
    management_assignment_id: DurableIdentifier | None = None
    management_profile_snapshot: Mapping[ProfileIdentifier, DurableIdentifier] = Field(default_factory=dict)
```

Add `autonomous_profile_management: AutonomousProfileManagementSettings = Field(default_factory=AutonomousProfileManagementSettings)` to `AutoRoutingConfig`, add the `ManagementDecisionSnapshot` fields to `RoutingDecision` with `None`/empty defaults, and implement `management_authority_revision()` as a SHA-256 hash of only the canonical serialized management subtree. Validate unique runtime IDs, finite scores, ISO timestamps, non-empty bounded reason codes, and reject extra/free-form fields on every persisted management record.

- [ ] **Step 4: Make configuration serialization preserve one canonical management subtree**

```python
def config_document(config: AutoRoutingConfig) -> dict[str, Any]:
    return config.model_dump(mode="json", by_alias=True, exclude_none=False)


def management_authority_revision(config: AutoRoutingConfig) -> str:
    payload = json.dumps(
        config.autonomous_profile_management.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

Keep the existing full `authority_revision()` behavior unchanged; Stage 5 uses its own hash only for control previews and stale-work detection, while every profile revision records the normal full config authority before and after mutation.

- [ ] **Step 5: Run the model/config suite and commit the independently valid contracts**

Run: `uv run --with pytest pytest tests/plugins/auto_routing/test_management_models.py tests/plugins/auto_routing/test_models_config.py -q`

Expected: PASS.

```bash
git add plugins/auto_routing/auto_routing/models.py plugins/auto_routing/auto_routing/config.py tests/plugins/auto_routing/test_management_models.py
git commit -m "feat(auto-routing): add management authority contracts"
```

### Task 2: Signed local ranking-pack reader and verified-inventory projection

**Files:**
- Create: `plugins/auto_routing/auto_routing/ranking_pack.py`
- Modify: `plugins/auto_routing/auto_routing/inventory.py:148-360`
- Test: `tests/plugins/auto_routing/test_ranking_pack.py`

**Interfaces:**
- Consumes: `RankingPackTrust`, `RankingPackMetadata`, `RuntimeKey`, `ExecutableRuntime`, `InventorySnapshot`, active profile home, and `cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PublicKey`.
- Produces: `load_verified_ranking_pack(*, home: Path, trust: RankingPackTrust, now: datetime) -> VerifiedRankingPack`, `verified_inventory_candidates(snapshot: InventorySnapshot, now: datetime) -> tuple[ManagementInventoryCandidate, ...]`, and `ranking_pack_status(...) -> dict[str, object]`.

- [ ] **Step 1: Write tests for trust, expiry, path containment, and no external refresh**

```python
def test_verified_pack_requires_a_trusted_ed25519_signature(tmp_path: Path, trust: RankingPackTrust) -> None:
    pack_path = write_signed_pack(tmp_path, signer=TEST_PRIVATE_KEY)
    pack = load_verified_ranking_pack(home=tmp_path, trust=trust, now=NOW)
    assert pack.metadata.pack_id == "pack-2026-07"
    assert pack.rank_for("a" * 64).quality == pytest.approx(0.91)


@pytest.mark.parametrize("mutator", [tamper_signature, expire_pack, use_unknown_key, escape_root])
def test_invalid_pack_fails_closed_without_network_or_inventory_refresh(mutator, tmp_path: Path, trust: RankingPackTrust, monkeypatch) -> None:
    monkeypatch.setattr("plugins.auto_routing.auto_routing.inventory.InventoryService.refresh", lambda *_a, **_k: pytest.fail("refresh"))
    with pytest.raises(RankingPackError):
        load_verified_ranking_pack(home=tmp_path, trust=mutator(trust), now=NOW)
```

- [ ] **Step 2: Run the pack tests and confirm imports fail**

Run: `uv run --with pytest pytest tests/plugins/auto_routing/test_ranking_pack.py -q`

Expected: FAIL because `ranking_pack.py` and `load_verified_ranking_pack` are absent.

- [ ] **Step 3: Implement canonical signed envelopes and containment checks**

```python
def _canonical_signed_bytes(document: Mapping[str, object]) -> bytes:
    unsigned = {key: value for key, value in document.items() if key != "signature"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def load_verified_ranking_pack(*, home: Path, trust: RankingPackTrust, now: datetime) -> VerifiedRankingPack:
    root = (home / "auto-routing" / "ranking-packs").resolve(strict=True)
    candidate = (home / trust.ranking_pack_path).resolve(strict=True)
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise RankingPackError("ranking_pack_outside_allowed_root")
    document = json.loads(candidate.read_text(encoding="utf-8"))
    envelope = RankingPackEnvelope.model_validate(document)
    if envelope.expires_at <= now.astimezone(UTC):
        raise RankingPackError("ranking_pack_expired")
    public_key = _trusted_key(envelope.key_id, trust.trusted_ed25519_public_keys)
    try:
        public_key.verify(base64.b64decode(envelope.signature), _canonical_signed_bytes(document))
    except (InvalidSignature, ValueError) as error:
        raise RankingPackError("ranking_pack_signature_invalid") from error
    return VerifiedRankingPack.from_envelope(envelope, sha256=_sha256_bytes(candidate.read_bytes()))
```

The envelope must have schema version `1`, bounded `pack_id`, `issued_at`, `expires_at`, `key_id`, base64 signature, and 0..1 finite quality/reliability/latency/cost values keyed by a 64-character runtime stable ID. Do not retain raw JSON after parsing; `VerifiedRankingPack` exposes only the metadata fingerprint and normalized lookup rows. Do not add HTTP clients, provider calls, catalog calls, or inventory refresh calls.

- [ ] **Step 4: Project only already-verified candidate capabilities from a passed snapshot**

```python
def verified_inventory_candidates(snapshot: InventorySnapshot, now: datetime) -> tuple[ManagementInventoryCandidate, ...]:
    return tuple(
        ManagementInventoryCandidate.from_runtime(runtime)
        for runtime in sorted(snapshot.runtimes, key=lambda item: item.key.stable_id())
        if runtime.state == "verified"
        and runtime.verification_expires_at is not None
        and _parse_timestamp(runtime.verification_expires_at) > now
        and _is_configured_or_installed_local(runtime)
        and _has_runnable_capability(runtime)
    )
```

Treat missing/expired verification, absent runnable capability, and non-configured/non-local provenance as ineligible reason codes. This function accepts an `InventorySnapshot`; it must neither own an `InventoryService` nor call `refresh()`.

- [ ] **Step 5: Run pack and inventory tests, then commit**

Run: `uv run --with pytest pytest tests/plugins/auto_routing/test_ranking_pack.py tests/plugins/auto_routing/test_inventory.py -q`

Expected: PASS.

```bash
git add plugins/auto_routing/auto_routing/ranking_pack.py plugins/auto_routing/auto_routing/inventory.py tests/plugins/auto_routing/test_ranking_pack.py
git commit -m "feat(auto-routing): verify local management ranking packs"
```

### Task 3: Deterministic candidate ranking and safe profile mutation planner

**Files:**
- Create: `plugins/auto_routing/auto_routing/management.py`
- Test: `tests/plugins/auto_routing/test_management_planner.py`

**Interfaces:**
- Consumes: `RouteProfile`, `RoutingTarget`, `ObjectiveWeights`, `ProfileLimits`, `ManagementInventoryCandidate`, `VerifiedRankingPack`, `ManagementPatch`, and an injected UTC `now`.
- Produces: `rank_management_candidates(profile: RouteProfile, candidates: tuple[ManagementInventoryCandidate, ...], pack: VerifiedRankingPack) -> tuple[RankedManagementCandidate, ...]`, `plan_management_revision(...) -> ManagementPlan`, and `management_hold(reason_code: str) -> ManagementPlan`.

- [ ] **Step 1: Write deterministic-ranking and no-change safety tests**

```python
def test_rank_uses_profile_objectives_and_runtime_id_tiebreaker(profile: RouteProfile, candidates, pack) -> None:
    ranked = rank_management_candidates(profile, candidates, pack)
    assert [item.runtime_id for item in ranked if item.eligible] == ["a" * 64, "b" * 64]
    assert ranked[0].score == pytest.approx(0.83)


def test_primary_upgrade_becomes_challenger_not_immediate_primary(profile, candidates, pack) -> None:
    plan = plan_management_revision(profile=profile, candidates=candidates, pack=pack, active_assignments=(), now=NOW)
    assert plan.action == "propose_canary"
    assert plan.after_profile.primary == profile.primary
    assert plan.after_profile.primary_challengers[0].runtime.stable_id() == "b" * 64


def test_planner_preserves_viable_route_and_unfinished_assignment(profile, candidates, pack, assignment) -> None:
    plan = plan_management_revision(profile=profile, candidates=candidates, pack=pack, active_assignments=(assignment,), now=NOW)
    assert assignment.runtime_id in plan.after_runtime_ids
    assert len(plan.after_runtime_ids) >= 1
```

- [ ] **Step 2: Run the planner tests and confirm they fail**

Run: `uv run --with pytest pytest tests/plugins/auto_routing/test_management_planner.py -q`

Expected: FAIL because `management.py` has not been created.

- [ ] **Step 3: Implement the pure ranking function and ordered reason codes**

```python
def _score(weights: ObjectiveWeights, row: RankingMetrics) -> float:
    return (
        weights.quality * row.quality
        + weights.reliability * row.reliability
        + weights.latency * (1.0 - row.latency)
        + weights.cost * (1.0 - row.cost)
    )


def rank_management_candidates(profile: RouteProfile, candidates: tuple[ManagementInventoryCandidate, ...], pack: VerifiedRankingPack) -> tuple[RankedManagementCandidate, ...]:
    ranked = []
    for candidate in candidates:
        rejection = _eligibility_reason(profile, candidate, pack)
        metrics = pack.rank_for(candidate.runtime_id)
        ranked.append(RankedManagementCandidate(
            runtime_id=candidate.runtime_id,
            eligible=rejection is None,
            reason_codes=() if rejection is None else (rejection,),
            score=None if rejection is not None else _score(profile.objectives, metrics),
        ))
    return tuple(sorted(ranked, key=lambda item: (not item.eligible, -(item.score or 0.0), item.runtime_id)))
```

Use this exact ordered rejection sequence: `inventory_not_verified`, `local_capability_missing`, `ranking_missing`, `license_rejected`, `context_limit_rejected`, `reasoning_limit_rejected`, `cost_limit_rejected`, `latency_limit_rejected`. Convert a management candidate to a `RoutingTarget` only by copying its verified `RuntimeKey`, supported reasoning, and bounded economics; never synthesize credentials, endpoint identity, capabilities, or unsupported effort.

- [ ] **Step 4: Implement canonical planner transitions without I/O**

```python
def plan_management_revision(*, profile: RouteProfile, candidates: tuple[ManagementInventoryCandidate, ...], pack: VerifiedRankingPack, active_assignments: tuple[ManagementCanaryAssignment, ...], now: datetime) -> ManagementPlan:
    ranked = rank_management_candidates(profile, candidates, pack)
    eligible = tuple(item for item in ranked if item.eligible)
    if not eligible:
        return management_hold("no_eligible_candidate")
    return _build_safe_profile_plan(profile, eligible, active_assignments, pack.metadata, now)
```

`_build_safe_profile_plan` must return `no_change` for identical canonical order, `propose_canary` when a non-primary leader is newly introduced as the first challenger, or `fallback_reorder` when only the fallback chain changes. It must retain one viable route, retain the target of every non-terminal management assignment, deduplicate by `runtime.stable_id()`, never alter profile description/match/objectives/limits/provenance/adaptation, and produce an immutable `ManagementPatch` containing before/after stable-ID tuples and bounded reason codes.

- [ ] **Step 5: Run the planner suite and commit**

Run: `uv run --with pytest pytest tests/plugins/auto_routing/test_management_planner.py tests/plugins/auto_routing/test_models_config.py -q`

Expected: PASS.

```bash
git add plugins/auto_routing/auto_routing/management.py tests/plugins/auto_routing/test_management_planner.py
git commit -m "feat(auto-routing): plan deterministic profile management"
```

### Task 4: Schema v8 immutable management storage and concurrency contracts

**Files:**
- Modify: `plugins/auto_routing/auto_routing/storage.py:63,2780-2875,3414-8205`
- Test: `tests/plugins/auto_routing/test_management_storage.py`
- Test: `tests/plugins/auto_routing/test_management_storage_concurrency.py`

**Interfaces:**
- Consumes: all Task 1 management models, existing `_canonical_json`, `_assert_content_free`, `write_txn`, `RevisionConflict`, and `RoutingStore` connection ownership rules.
- Produces: schema version `8`; management table/read APIs; `read_management_control()`, `transition_management_control()`, `read_management_profile_state()`, `publish_management_revision()`, `transition_management_profile_state()`, `reserve_management_assignment()`, `finalize_management_assignment()`, `list_open_management_assignments()`, `acquire_management_lease()`, `release_management_lease()`, `record_management_receipt()`, `recover_management_receipt()`, and `management_daily_admissions()`.

- [ ] **Step 1: Write migration and immutable-record tests**

```python
def test_v8_creates_complete_management_surface(store: RoutingStore) -> None:
    tables = {row["name"] for row in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "management_controls", "management_profile_states", "management_revisions",
        "management_lifecycle_events", "management_canary_assignments",
        "management_leases", "management_config_receipts",
    } <= tables
    assert store.schema_version == 8


def test_same_management_revision_id_cannot_change_document(store: RoutingStore, revision: ManagementRevision) -> None:
    store.publish_management_revision(revision)
    with pytest.raises(ImmutableRecordConflict):
        store.publish_management_revision(revision.model_copy(update={"resulting_authority_id": "b" * 64}))


def test_daily_admission_is_atomic_across_connections(db_path: Path, revision: ManagementRevision) -> None:
    assert try_admit_from_two_independent_stores(db_path, revision, limit=1) == [True, False]
```

- [ ] **Step 2: Run storage tests and confirm v7 lacks the v8 surface**

Run: `uv run --with pytest pytest tests/plugins/auto_routing/test_management_storage.py tests/plugins/auto_routing/test_management_storage_concurrency.py -q`

Expected: FAIL because schema version is `7` and management APIs/tables are absent.

- [ ] **Step 3: Add the strictly validated schema v8 tables and migration guard**

```python
SCHEMA_VERSION = "8"

_MANAGEMENT_TABLES = frozenset({
    "management_controls", "management_profile_states", "management_revisions",
    "management_lifecycle_events", "management_canary_assignments",
    "management_leases", "management_config_receipts",
})

def _reject_incompatible_partial_management_schema(connection: sqlite3.Connection, stored_version: int | None) -> None:
    present = {str(row["name"]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'") if str(row["name"]) in _MANAGEMENT_TABLES}
    if not present and (stored_version is None or stored_version < 8):
        return
    missing = sorted(_MANAGEMENT_TABLES - present)
    if missing:
        raise UnsupportedSchemaVersion(f"v8 database has an incompatible partial management schema missing {missing[0]}")
    expected, _indexes = _expected_schema_signatures()
    for table in sorted(_MANAGEMENT_TABLES):
        if _table_schema_signature(connection, table) != expected[table]:
            raise UnsupportedSchemaVersion(f"v8 database has an incompatible partial {table} schema")
```

Call the new rejection function inside the same `write_txn` as the prior evidence/adaptation guards before creating any v8 tables. Each record table needs canonical JSON plus checksum and explicit foreign keys to its parent authority/revision/state. `management_revisions` stores only stable IDs, before/result authority IDs, pack/inventory fingerprints, management epoch, canonical patch JSON, action, timestamps, and checksum. Its raw config document must not be stored.

- [ ] **Step 4: Implement CAS transitions, daily cap, leases, assignments, and receipts**

```python
def transition_management_profile_state(self, *, profile_id: str, authority_id: str, expected_generation: int, state: ManagementProfileState, event: ManagementLifecycleEvent) -> ManagementProfileState:
    with self.write_txn() as connection:
        current = self.read_management_profile_state(authority_id, profile_id, connection=connection)
        if current.generation != expected_generation:
            raise RevisionConflict("stale management profile generation")
        _insert_validated_management_event(connection, event)
        _replace_management_state(connection, state.model_copy(update={"generation": current.generation + 1}))
    return self.read_management_profile_state(authority_id, profile_id)


def try_admit_management_revision(self, *, profile_id: str, utc_day: str, daily_limit: int, revision: ManagementRevision) -> bool:
    with self.write_txn() as connection:
        admitted = _count_management_admissions(connection, profile_id, utc_day)
        if admitted >= daily_limit:
            _insert_management_hold(connection, profile_id, revision.preceding_authority_id, "daily_cap_reached")
            return False
        _insert_management_revision(connection, revision, admitted_utc_day=utc_day)
        return True
```

Use an expiring `management_leases` row keyed by authority/profile; an assignment is initially `reserved`, becomes `finalized` only after final runtime and reasoning resolution, and becomes terminal only through an immutable event. A receipt has `prepared`, `config_replaced`, `committed`, or `recovery_required` phase plus canonical before/after authority IDs, backup checksum, and revision ID. Every `from_row` path must checksum-verify and Pydantic-validate before returning a record.

- [ ] **Step 5: Run migration, integrity, and independent-connection tests, then commit**

Run: `uv run --with pytest pytest tests/plugins/auto_routing/test_management_storage.py tests/plugins/auto_routing/test_management_storage_concurrency.py tests/plugins/auto_routing/test_adaptation_storage.py tests/plugins/auto_routing/test_storage_concurrency.py -q`

Expected: PASS, including strict rejection of a partial v8 schema.

```bash
git add plugins/auto_routing/auto_routing/storage.py tests/plugins/auto_routing/test_management_storage.py tests/plugins/auto_routing/test_management_storage_concurrency.py
git commit -m "feat(auto-routing): persist management revisions safely"
```

### Task 5: Locked profile-config revision saga and manual-edit precedence

**Files:**
- Modify: `plugins/auto_routing/auto_routing/config_io.py:139-320`
- Modify: `plugins/auto_routing/auto_routing/service.py:169-800,2136-2350`
- Test: `tests/plugins/auto_routing/test_management_reconciler.py`

**Interfaces:**
- Consumes: `ManagementPlan`, `ManagementRevision`, Task 4 receipt/state APIs, `parse_config`, `authority_revision`, `locked_update`, and `LockedConfigUpdate.create_backup/replace/restore`.
- Produces: `LockedConfigUpdate.current_config() -> AutoRoutingConfig`, `apply_management_config_revision(*, proposal: AutoRoutingConfig, revision: ManagementRevision, expected_authority_id: str, store: RoutingStore, config_path: Path) -> ManagementRevisionResult`, `recover_management_config_revision(...) -> ManagementRevisionResult`, and `AutoRoutingService.reconcile_management(now: datetime | None = None, scheduled: bool = False) -> ManagementReconcileReport`.

- [ ] **Step 1: Write failure-first saga tests**

```python
def test_reconcile_reloads_authority_under_lock_and_manual_edit_wins(service, config_path: Path) -> None:
    preview = service.plan_management_reconciliation(now=NOW)
    replace_config_authority(config_path, changed_by_user=True)
    report = service.apply_management_plan(preview.plan_id, now=NOW)
    assert report.changed is False
    assert report.reason_code == "authority_changed"
    assert user_document(config_path) == read_yaml(config_path)


def test_db_failure_after_replace_restores_exact_prior_bytes(service, config_path: Path, monkeypatch) -> None:
    before = config_path.read_bytes()
    monkeypatch.setattr(service.store, "record_management_receipt", raise_sqlite_failure)
    report = service.reconcile_management(now=NOW)
    assert report.reason_code == "config_restored_after_store_failure"
    assert config_path.read_bytes() == before


def test_crash_receipt_recovers_or_freezes_without_stale_overwrite(service) -> None:
    service.store.seed_prepared_management_receipt(...)
    assert service.recover_management() in {"recovered", "frozen_recovery_required"}
```

- [ ] **Step 2: Run reconciliation tests and confirm the service methods are missing**

Run: `uv run --with pytest pytest tests/plugins/auto_routing/test_management_reconciler.py -q`

Expected: FAIL with missing `reconcile_management`/`apply_management_config_revision` methods.

- [ ] **Step 3: Add one locked write helper with receipt-first recovery semantics**

```python
class LockedConfigUpdate:
    # Keep this read under the same pinned path and lock as preview/replace.
    def current_config(self) -> AutoRoutingConfig:
        document = yaml.safe_load(self._pinned_path.target_path.read_text(encoding="utf-8"))
        if not isinstance(document, Mapping):
            raise ConfigConflict("locked config is not a mapping")
        return parse_config(document)


def apply_management_config_revision(*, proposal: AutoRoutingConfig, revision: ManagementRevision, expected_authority_id: str, store: RoutingStore, config_path: Path) -> ManagementRevisionResult:
    with locked_update(proposal, config_path, allow_active=True) as update:
        current = update.current_config()
        if authority_revision(current) != expected_authority_id:
            return ManagementRevisionResult(False, "authority_changed", None)
        backup = update.create_backup()
        store.record_management_receipt(ManagementConfigReceipt.prepared(revision, backup.checksum))
        try:
            update.replace()
            store.record_management_receipt(ManagementConfigReceipt.config_replaced(revision))
            store.commit_management_revision(revision)
        except BaseException:
            try:
                update.restore(backup)
                store.mark_management_receipt_restored(revision.revision_id)
                return ManagementRevisionResult(False, "config_restored_after_store_failure", revision.revision_id)
            except BaseException:
                store.freeze_management_recovery(revision.revision_id)
                raise
        store.mark_management_receipt_committed(revision.revision_id)
        return ManagementRevisionResult(True, "revision_applied", revision.revision_id)
```

Do not use `apply_update()` for this saga because management needs the config locks, byte-for-byte backup, receipt phase, and rollback to remain coupled. Re-load the current authority while inside the lock; if it differs, cancel the plan, record the content-free `authority_changed` hold under a new management epoch, and do not call `replace()`. Catch only to restore/re-freeze; never swallow a failed restoration.

- [ ] **Step 4: Orchestrate read-only planning and automatic reconciliation**

```python
def reconcile_management(self, *, now: datetime | None = None, scheduled: bool = False) -> ManagementReconcileReport:
    config = self._configured_authority()
    settings = config.autonomous_profile_management
    if not settings.enabled:
        return ManagementReconcileReport.hold("management_disabled")
    if self.store.read_management_control().frozen:
        return ManagementReconcileReport.hold("management_frozen")
    snapshot = self._current_persisted_inventory_snapshot()
    pack = load_verified_ranking_pack(home=self.home, trust=settings.ranking_pack, now=self._utc_now(now))
    return self._reconcile_profiles_with_leases(config, snapshot, pack, self._utc_now(now), scheduled=scheduled)
```

`_current_persisted_inventory_snapshot()` must read a stored/current snapshot only; it must reject absence or ambiguity and cannot call `InventoryService.refresh`. Acquire one management lease per profile, calculate each plan with Task 3, use the Task 4 atomic daily admission, execute Task 5's saga only for an admitted mutation, and release every lease in `finally`. A pack, state, lease, receipt, cap, or runtime-eligibility problem produces a per-profile hold and continues safely with other profiles.

- [ ] **Step 5: Run saga/reconciler and existing config tests, then commit**

Run: `uv run --with pytest pytest tests/plugins/auto_routing/test_management_reconciler.py tests/plugins/auto_routing/test_config_io.py tests/plugins/auto_routing/test_storage.py -q`

Expected: PASS.

```bash
git add plugins/auto_routing/auto_routing/config_io.py plugins/auto_routing/auto_routing/service.py tests/plugins/auto_routing/test_management_reconciler.py
git commit -m "feat(auto-routing): reconcile management revisions under lock"
```

### Task 6: Separate management canaries, decision snapshots, and rollback lifecycle

**Files:**
- Modify: `plugins/auto_routing/auto_routing/service.py:2136-2520,3207-3900`
- Modify: `plugins/auto_routing/auto_routing/decisions.py`
- Test: `tests/plugins/auto_routing/test_management_assignment.py`
- Test: `tests/plugins/auto_routing/test_management_reconciler.py`

**Interfaces:**
- Consumes: Task 4 management state/assignment APIs; `operation_identity_hash`, Stage 4 deterministic arm/learner/guardrail functions from `adaptation.py`/`learner.py`; `RoutingDecision`; existing selector/resolver fallback behavior.
- Produces: `AutoRoutingService.maybe_advance_management(profile_id: str, now: datetime | None = None) -> ManagementAdvance`, `AutoRoutingService._reserve_management_assignment(...) -> ManagementDecisionSnapshot`, and `AutoRoutingService.record_management_outcome(...) -> ManagementAdvance`.

- [ ] **Step 1: Write assignment-boundary tests before changing routing**

```python
def test_fresh_management_canary_persists_final_runtime_before_dispatch(active_service) -> None:
    decision = active_service.create_runtime_decision(fresh_request())
    assert decision.management_assignment_id is not None
    assignment = active_service.store.read_management_assignment(decision.management_assignment_id)
    assert assignment.runtime_id == decision.runtime_key.stable_id()
    assert assignment.reasoning_effort == decision.reasoning_effort


@pytest.mark.parametrize("request", [manual_request(), replay_request(), resumed_request(), compressed_request(), fixed_request()])
def test_existing_or_user_owned_boundaries_never_receive_management_overlay(active_service, request) -> None:
    decision = active_service.create_runtime_decision(request)
    assert decision.management_assignment_id is None
    assert decision.management_profile_snapshot == {}


def test_management_promotion_does_not_change_stage4_adaptation_state(active_service) -> None:
    before = active_service.store.read_profile_control(AUTHORITY, "coding")
    active_service.record_management_outcome(canary_outcome(...))
    after = active_service.store.read_profile_control(AUTHORITY, "coding")
    assert after == before
```

- [ ] **Step 2: Run the assignment tests and confirm no management snapshot exists**

Run: `uv run --with pytest pytest tests/plugins/auto_routing/test_management_assignment.py -q`

Expected: FAIL because `RoutingDecision` is not populated with a management assignment.

- [ ] **Step 3: Reserve/finalize a management assignment only at the final fresh/delegation boundary**

```python
def _reserve_management_assignment(self, *, decision_input: DecisionInput, selected: ResolvedSelection, now: datetime) -> ManagementDecisionSnapshot:
    if decision_input.scope not in {"fresh_session", "delegation"} or decision_input.is_manual_or_replay_boundary:
        return ManagementDecisionSnapshot()
    state = self.store.read_management_profile_state(selected.authority_revision, selected.profile_id)
    if state.phase != "canary" or state.frozen:
        return ManagementDecisionSnapshot()
    arm = deterministic_canary_arm(operation_identity_hash(decision_input), state.canary_fraction)
    target = state.challenger_target_id if arm == "challenger" else state.control_target_id
    resolved = self._resolve_exact_management_target(selected, target)
    reservation = self.store.reserve_management_assignment(state=state, arm=arm, operation_id=decision_input.operation_id, runtime_id=resolved.runtime_id, reasoning_effort=resolved.reasoning_effort)
    finalized = self.store.finalize_management_assignment(reservation.assignment_id, resolved=resolved)
    return ManagementDecisionSnapshot(management_revision_id=state.active_revision_id, management_assignment_id=finalized.assignment_id, management_profile_snapshot={selected.profile_id: state.profile_revision_id})
```

Apply the overlay after profile selection but before the final runtime/effect resolution. If target resolution, reservation, or finalization fails, use the already-recorded valid control route and persist no challenger dispatch; do not mutate Stage 4 state. Add the resulting snapshot fields to the final `RoutingDecision` and storage serialization, retaining existing decision checksums for older decisions with default-empty Stage 5 fields.

- [ ] **Step 4: Advance the independent management lifecycle using Stage 4 pure math only**

```python
def maybe_advance_management(self, *, profile_id: str, now: datetime | None = None) -> ManagementAdvance:
    state = self.store.read_management_profile_state_for_current_authority(profile_id)
    if state.frozen or state.phase in {"recovery_required", "cooldown"}:
        return ManagementAdvance.hold(state.phase)
    summary = summarize_quality(self.store.list_management_comparable_outcomes(state.active_revision_id))
    decision = promotion_decision(summary, minimum_samples=state.minimum_comparable_samples, regression_threshold=state.observed_regression_threshold, confidence_level=state.confidence_level)
    return self._apply_management_promotion_or_exact_rollback(state, decision, now=self._utc_now(now))
```

Use Stage 4's pure quality summary, confidence and guardrail calculations with management-owned observations only. Promotion runs a new receipt-backed locked config revision that makes the recorded challenger primary. Rejection, configured retry/latency/cost guardrail breach, budget exhaustion, or resolver failure restores the exact backup authority from the original management receipt, terminates assignments, increments only the management rejection count, and enters management cooldown. A promotion/rollback is a transition of the admitted revision, not a second daily admission.

- [ ] **Step 5: Run Stage 4 regression and management assignment suites, then commit**

Run: `uv run --with pytest pytest tests/plugins/auto_routing/test_management_assignment.py tests/plugins/auto_routing/test_management_reconciler.py tests/plugins/auto_routing/test_adaptation_assignment.py tests/plugins/auto_routing/test_adaptation_lifecycle.py -q`

Expected: PASS; the Stage 4 tests prove management did not couple to adaptation control.

```bash
git add plugins/auto_routing/auto_routing/service.py plugins/auto_routing/auto_routing/decisions.py tests/plugins/auto_routing/test_management_assignment.py tests/plugins/auto_routing/test_management_reconciler.py
git commit -m "feat(auto-routing): canary autonomous profile changes"
```

### Task 7: Guarded management CLI controls and existing-cron scheduling

**Files:**
- Modify: `plugins/auto_routing/auto_routing/cli.py:15-330,427-535`
- Create: `plugins/auto_routing/auto_routing/management_cron.py`
- Modify: `plugins/auto_routing/auto_routing/service.py:1581-1700,5284-5360`
- Test: `tests/plugins/auto_routing/test_management_cli.py`
- Test: `tests/plugins/auto_routing/test_management_cron.py`

**Interfaces:**
- Consumes: `AutoRoutingService.reconcile_management`, `management_status`, `management_history`, Task 5 preview/apply APIs, `cron.jobs.create_job/update_job/remove_job`, and Task 1 global settings.
- Produces: `manage inventory`, `manage ranking`, `manage status`, `manage history`, `manage reconcile`, `manage enable`, `manage disable`, `manage freeze`, `manage unfreeze`, `manage schedule`, and `install_management_cron(...) -> ManagementCronInstall`.

- [ ] **Step 1: Write CLI metadata, guard, and scheduled-mode tests**

```python
def test_manage_control_requires_preview_hash_before_apply(parser, service) -> None:
    args = parser.parse_args(["manage", "freeze", "--apply"])
    result = execute(args, service=service)
    assert result.ok is False
    assert result.error_code == "expected_hash_required"


def test_manage_reconcile_scheduled_mode_is_noninteractive_and_content_free(parser, service) -> None:
    args = parser.parse_args(["manage", "reconcile", "--scheduled", "--json"])
    result = execute(args, service=service)
    assert result.ok is True
    assert "prompt" not in json.dumps(result.payload).lower()


def test_enable_installs_no_agent_python_cron_job(tmp_path: Path, service) -> None:
    applied = service.apply_management_control(action="enable", expected_hash=service.preview_management_control("enable")["precondition_hash"])
    job = get_job(applied.cron_job_id)
    assert job["no_agent"] is True
    assert job["script"].endswith("auto-routing-management.py")
```

- [ ] **Step 2: Run CLI/cron tests and confirm `manage` is not registered**

Run: `uv run --with pytest pytest tests/plugins/auto_routing/test_management_cli.py tests/plugins/auto_routing/test_management_cron.py -q`

Expected: FAIL because `manage` is not a command and the service control APIs are absent.

- [ ] **Step 3: Register read-only and guarded `manage` leaves**

```python
_MANAGE_SPECS = (
    _CommandSpec(CommandMetadata("manage inventory", "Show persisted eligible management inventory", CommandWriteClass.READ_ONLY), _manage_read_only),
    _CommandSpec(CommandMetadata("manage ranking", "Show verified local ranking-pack status", CommandWriteClass.READ_ONLY), _manage_read_only),
    _CommandSpec(CommandMetadata("manage status", "Show global autonomous-management status", CommandWriteClass.READ_ONLY), _manage_read_only),
    _CommandSpec(CommandMetadata("manage history", "Show immutable management revision history", CommandWriteClass.READ_ONLY), _manage_history),
    _CommandSpec(CommandMetadata("manage reconcile", "Run one local automatic reconciliation", CommandWriteClass.GUARDED_CONTROL_PLANE), _manage_reconcile),
    _CommandSpec(CommandMetadata("manage enable", "Preview or enable global autonomous profile management", CommandWriteClass.GUARDED_CONTROL_PLANE), _manage_mutation),
    _CommandSpec(CommandMetadata("manage disable", "Preview or disable global autonomous profile management", CommandWriteClass.GUARDED_CONTROL_PLANE), _manage_mutation),
    _CommandSpec(CommandMetadata("manage freeze", "Preview or freeze management changes globally", CommandWriteClass.GUARDED_CONTROL_PLANE), _manage_mutation),
    _CommandSpec(CommandMetadata("manage unfreeze", "Preview or unfreeze management changes globally", CommandWriteClass.GUARDED_CONTROL_PLANE), _manage_mutation),
    _CommandSpec(CommandMetadata("manage schedule", "Preview or update the local management schedule", CommandWriteClass.GUARDED_CONTROL_PLANE), _manage_schedule),
)
```

`manage reconcile --scheduled` is callable only by the installed script, exits successfully for a no-change hold, and is never a model tool. All other mutating leaves require exact `--apply --expect-hash`; previews bind full authority revision, management authority revision, control generation, requested action, schedule, pack fingerprint/path, daily limit, and active cron job ID. `manage inventory`, `ranking`, `status`, and `history` must emit IDs, fingerprints, reason codes, counts, and timestamps only.

- [ ] **Step 4: Install/update one profile-local no-agent cron script and job**

```python
def install_management_cron(*, home: Path, schedule: str, previous_job_id: str | None) -> ManagementCronInstall:
    scripts = home / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    script = scripts / "auto-routing-management.py"
    script.write_text(
        "import shutil\n"
        "import subprocess\n"
        "import sys\n"
        "hermes = shutil.which('hermes')\n"
        "if hermes is None:\n"
        "    raise SystemExit('hermes executable not found')\n"
        "result = subprocess.run([hermes, 'auto-routing', 'manage', 'reconcile', '--scheduled', '--json'], check=False, text=True, stdout=sys.stdout, stderr=sys.stderr)\n"
        "raise SystemExit(result.returncode)\n",
        encoding="utf-8",
    )
    if previous_job_id is None:
        job = create_job(prompt="", schedule=schedule, name="auto-routing-management", script=str(script.relative_to(scripts)), no_agent=True, deliver="local")
    else:
        job = update_job(previous_job_id, {"schedule": schedule, "script": str(script.relative_to(scripts)), "no_agent": True})
    return ManagementCronInstall(job_id=str(job["id"]), script_path=script)
```

On enable, write the script using atomic replace, provision/update exactly one named job through the existing cron API, and record the job ID in `ManagementControl`. On disable, remove the stored job after disabling the configuration; if removal fails, leave a frozen/disabled control record and return an explicit repair error. On schedule update, verify the cron expression with the existing cron path before changing either job or control. The script must honor the active profile home and must not print secrets, config, ranking rows, or task content.

- [ ] **Step 5: Run command, cron, and prior adaptation CLI tests, then commit**

Run: `uv run --with pytest pytest tests/plugins/auto_routing/test_management_cli.py tests/plugins/auto_routing/test_management_cron.py tests/plugins/auto_routing/test_adaptation_cli.py tests/plugins/auto_routing/test_advisor_cli.py -q`

Expected: PASS.

```bash
git add plugins/auto_routing/auto_routing/cli.py plugins/auto_routing/auto_routing/management_cron.py plugins/auto_routing/auto_routing/service.py tests/plugins/auto_routing/test_management_cli.py tests/plugins/auto_routing/test_management_cron.py
git commit -m "feat(auto-routing): add guarded management operations"
```

### Task 8: Security, end-to-end compatibility, and user/assistant documentation

**Files:**
- Modify: `plugins/auto_routing/README.md`
- Modify: `plugins/auto_routing/skills/auto-routing/SKILL.md`
- Test: `tests/plugins/auto_routing/test_management_security.py`
- Test: `tests/plugins/auto_routing/test_stage5_management_e2e.py`

**Interfaces:**
- Consumes: all prior task public APIs and existing Stage 2–4 test support fixtures.
- Produces: documented global opt-in workflow, local ranking-pack schema, guarded operations, scheduling/recovery procedure, and final regression proof.

- [ ] **Step 1: Add security tests that make forbidden paths observable**

```python
def test_reconciliation_never_calls_network_probe_catalog_or_moa(service, monkeypatch) -> None:
    for target in (
        "InventoryService.refresh", "CatalogService.refresh", "requests.get",
        "httpx.Client.request", "AutoRoutingService.verify_runtime", "run_moa",
    ):
        monkeypatch.setattr(target, forbidden_side_effect)
    report = service.reconcile_management(now=NOW)
    assert report.changed in {True, False}


def test_replay_gateway_and_tui_keep_original_snapshot(stage5_active_service) -> None:
    original = stage5_active_service.create_runtime_decision(fresh_request())
    stage5_active_service.reconcile_management(now=NOW_PLUS_ONE_HOUR)
    assert replay_via_gateway(original).runtime_key == original.runtime_key
    assert replay_via_tui(original).fallbacks == original.fallbacks


def test_management_reports_and_sql_records_are_content_free(stage5_active_service) -> None:
    report = stage5_active_service.management_history()
    assert "sk-secret-sentinel" not in json.dumps(report)
    assert "https://endpoint.example" not in dump_management_tables(stage5_active_service.store)
```

- [ ] **Step 2: Run security/E2E tests and confirm remaining compatibility failures before documentation edits**

Run: `uv run --with pytest pytest tests/plugins/auto_routing/test_management_security.py tests/plugins/auto_routing/test_stage5_management_e2e.py -q`

Expected: Any remaining failures identify a missing invariant in Tasks 1–7; repair the implementation and rerun until PASS before changing docs.

- [ ] **Step 3: Document exact setup, control, and recovery workflows**

Add this configuration example to the README, using only placeholder key material and a profile-local pack path:

```yaml
autonomous_profile_management:
  enabled: true
  ranking_pack:
    ranking_pack_path: auto-routing/ranking-packs/current.json
    trusted_ed25519_public_keys:
      - BASE64_ED25519_PUBLIC_KEY
  daily_change_limit: 1
  schedule: "17 */6 * * *"
```

Document the signed envelope fields `schema_version`, `pack_id`, `issued_at`, `expires_at`, `key_id`, `rankings`, and `signature`; the deterministic normalized metrics; that packs are copied locally by the user; and the rejection/hold behavior. State plainly that this feature never downloads or enables a runtime, never makes a paid verification request, never uses MoA/evaluators/telemetry, and never changes profile topology or existing adaptation controls.

Document these exact guarded command sequences:

```text
hermes auto-routing manage ranking --json
hermes auto-routing manage enable --json
hermes auto-routing manage enable --apply --expect-hash SHA256 --json
hermes auto-routing manage status --json
hermes auto-routing manage freeze --json
hermes auto-routing manage freeze --apply --expect-hash SHA256 --json
hermes auto-routing manage history --profile-id coding --json
```

In the skill, instruct the agent to inspect persisted inventory and ranking status first, ask the user to configure/copy/sign a local pack and explicitly approve each preview, never offer unconfigured remote models, and use status/history/hold codes to explain an automatic change. Include the exact recovery procedure: freeze, inspect receipt/history, perform guarded rollback/repair, then unfreeze only after an updated preview.

- [ ] **Step 4: Run the full Stage 2–5 regression matrix**

Run: `uv run --with pytest pytest tests/plugins/auto_routing/test_management_models.py tests/plugins/auto_routing/test_ranking_pack.py tests/plugins/auto_routing/test_management_planner.py tests/plugins/auto_routing/test_management_storage.py tests/plugins/auto_routing/test_management_storage_concurrency.py tests/plugins/auto_routing/test_management_reconciler.py tests/plugins/auto_routing/test_management_assignment.py tests/plugins/auto_routing/test_management_cli.py tests/plugins/auto_routing/test_management_cron.py tests/plugins/auto_routing/test_management_security.py tests/plugins/auto_routing/test_stage5_management_e2e.py tests/plugins/auto_routing/test_stage2_fresh_session_e2e.py tests/plugins/auto_routing/test_stage2_delegation_e2e.py tests/plugins/auto_routing/test_stage2_gateway_tui.py tests/plugins/auto_routing/test_stage2_pre_call_fallback.py tests/plugins/auto_routing/test_stage2_security.py tests/plugins/auto_routing/test_stage3_evidence_e2e.py tests/plugins/auto_routing/test_stage3_security.py tests/plugins/auto_routing/test_stage4_adaptation_e2e.py tests/plugins/auto_routing/test_adaptation_security.py -q`

Expected: PASS with no network/provider/moa calls and no changed Stage 2–4 snapshot behavior.

Run: `cd ui-tui; npm run typecheck; npm test -- --run`

Expected: PASS; Stage 5 changes must not break the existing TUI client/gateway contract.

- [ ] **Step 5: Check the documentation and plan references, then commit**

Run: `git diff --check`

Expected: no output.

```bash
git add plugins/auto_routing/README.md plugins/auto_routing/skills/auto-routing/SKILL.md tests/plugins/auto_routing/test_management_security.py tests/plugins/auto_routing/test_stage5_management_e2e.py
git commit -m "docs(auto-routing): document autonomous profile management"
```

## Plan Self-Review

- **Spec coverage:** Tasks 1 and 7 implement disabled-by-default global activation and guarded controls. Tasks 2 and 3 enforce verified-local inventory plus signed local rankings and deterministic eligibility. Tasks 4 and 5 provide immutable revisions, exact receipt-backed rollback, strict schema migration, manual-edit precedence, leases, CAS, and daily caps. Task 6 creates an independent management canary state and preserves every existing routing boundary/snapshot. Task 7 uses only the existing local no-agent scheduler. Task 8 proves forbidden side effects, content hygiene, Windows/local capability, gateway/TUI compatibility, recovery, and documents user/assistant operation.
- **Placeholder scan:** No unfinished markers, vague deferrals, or undefined follow-up steps are present. Each code-changing task defines the public interfaces it uses and produces, a failing test, a test command, implementation code, a passing command, and a commit.
- **Type consistency:** `AutonomousProfileManagementSettings`, `RankingPackTrust`, `ManagementPlan`, `ManagementRevision`, `ManagementProfileState`, `ManagementCanaryAssignment`, `ManagementDecisionSnapshot`, `ManagementReconcileReport`, and `ManagementAdvance` are introduced in Task 1 or Task 3 before their later uses. Task 4 storage APIs are defined before Task 5’s saga, Task 6’s routing boundary, and Task 7’s controls use them.
