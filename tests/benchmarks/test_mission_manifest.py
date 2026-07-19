"""Contract tests for the mission-transaction benchmark preregistration.

The manifest freezes the denominator (corpus, sample gates, current-Hermes
baseline, p50/p95, Wilson CIs, costs) BEFORE any production code is written.
This file is the executable contract — anyone bumping a gate, an archetype
count, or a sample size MUST update both the manifest and these asserts in
the same change.
"""

from __future__ import annotations

import math
from pathlib import Path

import yaml

from gateway.mission_outbox import MissionOutboxStore
from hades_state import SessionDB
from hades_cli import missions_db as mdb
from hades_cli import workflows_db as wfdb
from hades_cli import workflows_dispatcher
from hades_cli.workflows_capabilities import implemented_primitive_errors
from hades_cli.workflows_engine import run_in_memory_until_waiting
from hades_cli.workflows_spec import WorkflowSpec, load_spec_from_object

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "benchmarks" / "missions" / "manifest.yaml"
FIXTURE = ROOT / "benchmarks" / "missions" / "fixtures" / "three-effect-mission.yaml"

REQUIRED_FAULTS = [
    "after_prepare",
    "after_preview",
    "after_commit_started",
    "after_handler_return",
    "after_delivery_dispatch",
]

# Exact ordered list of the initial event sources. Order is contractual —
# appending a new source requires a deliberate manifest bump and matching
# test edit. Duplicate source entries must fail the contract.
EXPECTED_EVENT_SOURCES = [
    "cron",
    "filesystem_git",
    "webhook",
    "gateway_channel",
]

# Exact ordered list of the six required mission categories. Every
# archetype must declare `category` explicitly; falling back to `id`
# would let a drift in id mask a missing/typo'd category.
REQUIRED_CATEGORIES = [
    "software_maintenance",
    "sourced_research",
    "data_artifact_pipeline",
    "repeated_web_operation",
    "personal_knowledge_lifecycle",
    "proactive_recovery",
]

# Description metadata keys the fixture must declare exactly once.
# A repeat of either key (e.g. wrong value then correct value)
# must fail — silent overwrite hides the wrong value.
SINGLETON_DESCRIPTION_KEYS = ("artifact_path", "verification_command")

EXPECTED_ARTIFACT_PATH = "benchmarks/missions/artifacts/three-effect/output.md"
EXPECTED_VERIFICATION_COMMAND = "test -s benchmarks/missions/artifacts/three-effect/output.md"
EXPECTED_DELIVERY_CHANNEL = "hermes-test-channel"
EXPECTED_EDGES = [
    ("agent_task", "wait"),
    ("wait", "send_message"),
]

# Field names that would leak credentials, addresses, or real recipients onto
# the benchmark fixture. The fixture must stay hermetic — Task 7 wires
# send_message; until then we have nothing real to point at.
FORBIDDEN_FIXTURE_FIELDS = (
    "bot_token",
    "api_key",
    "token",
    "password",
    "secret",
    "to",
    "recipient",
    "address",
    "phone",
    "email",
    "user_id",
    "chat_id",
    "channel_id",
    "webhook_url",
    "url",
)


def _load_manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def _load_fixture_raw() -> dict:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def _load_spec() -> WorkflowSpec:
    return load_spec_from_object(_load_fixture_raw())


def test_manifest_exists_and_parses() -> None:
    assert MANIFEST.is_file(), f"manifest missing at {MANIFEST}"
    raw = _load_manifest()
    assert isinstance(raw, dict), "manifest must be a YAML mapping"


def test_manifest_schema_version() -> None:
    manifest = _load_manifest()
    assert manifest["schema"] == "hermes.mission-benchmark.v1"


def test_manifest_corpus_minimum_missions() -> None:
    manifest = _load_manifest()
    corpus = manifest["corpus"]
    assert corpus["minimum_missions"] == 30


def test_manifest_corpus_has_six_archetypes() -> None:
    manifest = _load_manifest()
    archetypes = manifest["corpus"]["archetypes"]
    assert isinstance(archetypes, list), "archetypes must be a list"
    assert len(archetypes) == 6, f"expected 6 archetypes, got {len(archetypes)}"


def test_manifest_corpus_archetypes_have_real_tasks() -> None:
    manifest = _load_manifest()
    archetypes = manifest["corpus"]["archetypes"]
    for arch in archetypes:
        assert isinstance(arch, dict), (
            f"archetypes must be mappings with id/category/real_tasks; got bare value: {arch!r}"
        )
        for key in ("id", "category", "real_tasks"):
            assert key in arch, f"archetype missing required key {key!r}: {arch!r}"
        tasks = arch["real_tasks"]
        assert isinstance(tasks, list), (
            f"archetype {arch['id']!r} real_tasks must be a list"
        )
        assert len(tasks) >= 2, (
            f"archetype {arch['id']!r} needs >=2 real tasks, got {len(tasks)}"
        )
        for task in tasks:
            assert isinstance(task, dict), (
                f"archetype {arch['id']!r} real_tasks entries must be mappings, got {task!r}"
            )
            assert "id" in task and "title" in task, (
                f"archetype {arch['id']!r} real task missing id/title: {task!r}"
            )


def test_manifest_corpus_archetypes_cover_required_categories() -> None:
    manifest = _load_manifest()
    seen: list[str] = []
    for arch in manifest["corpus"]["archetypes"]:
        assert isinstance(arch, dict), (
            f"archetype entries must be mappings: {arch!r}"
        )
        # Require `category` explicitly — no fallback to `id`. An empty or
        # missing category on a future archetype must fail loud; an id-only
        # archetype cannot stand in for a missing category.
        assert "category" in arch, (
            f"archetype {arch.get('id')!r} missing required key 'category'"
        )
        category = arch["category"]
        assert isinstance(category, str) and category.strip(), (
            f"archetype {arch.get('id')!r} has empty/non-string category: {category!r}"
        )
        seen.append(category)

    # Exact ordered list of required categories, with no duplicates.
    assert seen == REQUIRED_CATEGORIES, (
        f"archetype categories must be exactly {REQUIRED_CATEGORIES!r} in order; "
        f"got {seen!r}"
    )
    assert len(set(seen)) == len(REQUIRED_CATEGORIES), (
        f"archetype categories must be unique; got {seen!r}"
    )


def test_manifest_event_sources() -> None:
    manifest = _load_manifest()
    sources = manifest["event_sources"]
    # ponytail: exact ordered list — set comparison would silently accept
    # duplicates like ["cron", "cron", "filesystem_git", ...].
    assert isinstance(sources, list), f"event_sources must be a list; got {type(sources).__name__}"
    assert sources == EXPECTED_EVENT_SOURCES, (
        f"event_sources must be exactly {EXPECTED_EVENT_SOURCES!r} (in order, "
        f"no duplicates); got {sources!r}"
    )
    assert len(sources) == len(set(sources)), (
        f"event_sources must contain no duplicates; got {sources!r}"
    )


def test_manifest_vertical_slice_effects() -> None:
    manifest = _load_manifest()
    effects = manifest["vertical_slice"]["effect_types"]
    assert effects == ["workspace", "hades_state", "delayed_message"], (
        f"vertical slice effect_types changed; got {effects}"
    )


def test_manifest_fault_points_complete() -> None:
    manifest = _load_manifest()
    faults = manifest["faults"]
    # ponytail: exact ordered list — set comparison would silently accept
    # duplicate fault entries like ["after_prepare", "after_prepare", ...].
    assert isinstance(faults, list), f"faults must be a list; got {type(faults).__name__}"
    assert faults == REQUIRED_FAULTS, (
        f"faults must be exactly {REQUIRED_FAULTS!r} (in order, no duplicates); "
        f"got {faults!r}"
    )
    assert len(faults) == len(set(faults)), (
        f"faults must contain no duplicates; got {faults!r}"
    )


def test_manifest_gates_locked() -> None:
    manifest = _load_manifest()
    gates = manifest["gates"]
    assert gates["duplicate_effects"] == 0
    assert gates["false_verified"] == 0
    assert gates["mission_correct_state_rate"] >= 0.90
    assert gates["transaction_median_overhead_ratio"] <= 0.15


def test_manifest_sample_sizes() -> None:
    manifest = _load_manifest()
    samples = manifest["samples"]
    # ponytail: assert sizes exist; semantic floors live in the gates test
    assert samples["missions"] == 30
    assert samples["missions"] == manifest["corpus"]["minimum_missions"], (
        "samples.missions must match corpus.minimum_missions"
    )
    assert samples["faults"] == 100
    assert samples["false_successes"] == 50


def test_manifest_records_required_baseline_fields() -> None:
    manifest = _load_manifest()
    baseline = manifest["baseline"]
    for key in (
        "p50_latency_seconds",
        "p95_latency_seconds",
        "verified_success_rate",
        "user_attention_per_mission",
        "recovery_burden_per_mission",
    ):
        assert key in baseline, f"baseline missing required key {key!r}"


def _wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% interval — stdlib math only, no extra dependency."""
    if total <= 0:
        raise ValueError("total must be positive")
    if successes < 0 or successes > total:
        raise ValueError("successes out of range")
    phat = successes / total
    z2 = z * z
    denom = 1.0 + z2 / total
    center = (phat + z2 / (2.0 * total)) / denom
    margin = (z * math.sqrt(phat * (1.0 - phat) / total + z2 / (4.0 * total * total))) / denom
    lower = max(0.0, center - margin)
    upper = min(1.0, center + margin)
    return lower, upper


def test_manifest_records_baseline_provenance_and_raw_counts() -> None:
    manifest = _load_manifest()
    baseline = manifest["baseline"]

    # 1. Provenance — explicit run method so the baseline is reproducible.
    provenance = baseline.get("provenance")
    assert isinstance(provenance, dict), (
        "baseline.provenance must be a mapping describing the run that produced the numbers"
    )
    for key in ("method", "sample_size", "measured_at"):
        assert key in provenance, f"baseline.provenance missing required key {key!r}"
    assert isinstance(provenance["method"], str) and provenance["method"].strip(), (
        "baseline.provenance.method must be a non-empty string"
    )
    sample_size = provenance["sample_size"]
    assert isinstance(sample_size, int) and sample_size > 0, (
        "baseline.provenance.sample_size must be a positive integer"
    )
    assert isinstance(provenance["measured_at"], str) and provenance["measured_at"].strip(), (
        "baseline.provenance.measured_at must be a non-empty ISO-8601 timestamp string"
    )

    # 2. Raw counts for every rate metric in the Wilson CI table.
    #    A Wilson CI without counts, or counts without a CI, both fail.
    raw_counts = baseline.get("raw_counts")
    assert isinstance(raw_counts, dict), (
        "baseline.raw_counts must record numerators + denominators for rate metrics"
    )
    wilson = manifest.get("wilson_confidence_intervals")
    assert isinstance(wilson, dict) and wilson, (
        "manifest must record wilson_confidence_intervals computed from raw_counts"
    )

    rate_keys = set(wilson.keys())
    count_keys = set(raw_counts.keys())
    assert rate_keys == count_keys, (
        "wilson_confidence_intervals and baseline.raw_counts must cover the exact same rate "
        f"metrics; wilson-only={sorted(rate_keys - count_keys)}, "
        f"counts-only={sorted(count_keys - rate_keys)}"
    )

    for key in rate_keys:
        entry = raw_counts[key]
        assert isinstance(entry, dict), (
            f"baseline.raw_counts[{key!r}] must be a mapping"
        )
        assert "numerator" in entry and "denominator" in entry, (
            f"baseline.raw_counts[{key!r}] must record numerator + denominator"
        )
        numerator = entry["numerator"]
        denominator = entry["denominator"]
        assert isinstance(numerator, int) and isinstance(denominator, int), (
            f"baseline.raw_counts[{key!r}] numerator/denominator must be ints"
        )
        assert 0 <= numerator <= denominator, (
            f"baseline.raw_counts[{key!r}] must satisfy 0 <= numerator <= denominator"
        )

        # Every raw-count denominator must match the declared baseline sample
        # size. A per-metric denominator that drifts (e.g. one metric measured
        # on a 200-run sample, another on 50) would invalidate Wilson-CI
        # comparability across metrics.
        assert denominator == sample_size, (
            f"baseline.raw_counts[{key!r}] denominator={denominator} must equal "
            f"baseline.provenance.sample_size={sample_size}; mismatched sample "
            f"sizes break Wilson-CI comparability across rate metrics"
        )

        # 3. Wilson CI must match the calculation from the recorded counts.
        successes = numerator
        total = denominator
        expected_lower, expected_upper = _wilson_interval(successes, total)
        recorded = wilson[key]
        assert isinstance(recorded, (list, tuple)) and len(recorded) == 2, (
            f"wilson_confidence_intervals[{key!r}] must be [lower, upper]"
        )
        assert math.isclose(float(recorded[0]), expected_lower, abs_tol=1e-9), (
            f"wilson lower bound for {key!r} {recorded[0]!r} does not match calculation "
            f"{expected_lower!r} from successes={successes}, total={total}"
        )
        assert math.isclose(float(recorded[1]), expected_upper, abs_tol=1e-9), (
            f"wilson upper bound for {key!r} {recorded[1]!r} does not match calculation "
            f"{expected_upper!r} from successes={successes}, total={total}"
        )

        # 4. The baseline point estimate must agree with the recorded counts.
        if key in baseline:
            expected_rate = successes / total
            assert math.isclose(
                float(baseline[key]), expected_rate, abs_tol=1e-9
            ), (
                f"baseline[{key!r}]={baseline[key]!r} disagrees with raw_counts "
                f"({successes}/{total} = {expected_rate!r})"
            )


def test_manifest_records_costs() -> None:
    manifest = _load_manifest()
    costs = manifest["costs"]
    for key in ("tokens_per_mission", "usd_per_mission"):
        assert key in costs, f"costs missing required dimension {key!r}"


def test_manifest_records_excluded_cases() -> None:
    manifest = _load_manifest()
    excluded = manifest["excluded_cases"]
    assert isinstance(excluded, list), "excluded_cases must be a list"
    assert excluded, "excluded_cases must be non-empty — if nothing is excluded, say so explicitly"


def test_manifest_no_aggregate_score() -> None:
    """Aggregate scores hide which dimension regressed. The contract forbids them.

    The ban is recursive: ``aggregate_score``, ``overall_score``, and
    ``score`` are rejected at every mapping depth, including nested blocks
    like ``baseline``. A top-level check would let a future contributor
    smuggle a single composite metric inside ``baseline`` and pretend it
    is one of the existing rate fields.
    """
    manifest = _load_manifest()
    forbidden_keys = {"aggregate_score", "overall_score", "score"}

    def _walk(obj: object, path: str) -> list[str]:
        offenders: list[str] = []
        if isinstance(obj, dict):
            for key, value in obj.items():
                child_path = f"{path}.{key}"
                if isinstance(key, str) and key in forbidden_keys:
                    offenders.append(f"{child_path}={value!r}")
                offenders.extend(_walk(value, child_path))
        elif isinstance(obj, list):
            for index, value in enumerate(obj):
                offenders.extend(_walk(value, f"{path}[{index}]"))
        return offenders

    offenders = _walk(manifest, "manifest")
    assert not offenders, (
        "manifest forbids aggregate scoring fields (aggregate_score, "
        "overall_score, score) at every mapping depth; found: "
        + ", ".join(offenders)
    )


def test_fixture_loads_via_strict_ingestion_path() -> None:
    assert FIXTURE.is_file(), f"fixture missing at {FIXTURE}"
    spec = _load_spec()
    node_types = [node.type for node in spec.nodes.values()]
    assert node_types == ["agent_task", "wait", "send_message"], (
        f"fixture must declare chain agent_task -> wait -> send_message, got {node_types}"
    )


def test_fixture_edges_chain_agent_task_to_send_message() -> None:
    spec = _load_spec()
    ordered_pairs = [(edge.from_, edge.to) for edge in spec.edges]
    assert ordered_pairs == EXPECTED_EDGES, (
        f"fixture edges must match the exact ordered list {EXPECTED_EDGES!r}; "
        f"got {ordered_pairs!r}"
    )


def test_fixture_wait_node_has_seconds_30() -> None:
    spec = _load_spec()
    wait_node = spec.nodes["wait"]
    assert wait_node.type == "wait", f"node 'wait' must be a wait node; got {wait_node.type!r}"
    assert wait_node.seconds == 30, (
        f"fixture wait node must carry seconds=30 (not_before_seconds: 30); got {wait_node.seconds!r}"
    )


def _parse_description(description: str) -> tuple[dict[str, str], list[str]]:
    """Parse the fixture description into key/value entries.

    Description lines look like ``  - artifact_path: benchmarks/.../output.md``.

    Returns ``(parsed, duplicates)``. ``parsed`` keeps the FIRST-seen value
    for each key (so a test can still assert the surviving value), and
    ``duplicates`` lists every key that appeared more than once — so the
    caller can fail loud on a stray repeat instead of silently merging it.
    """
    parsed: dict[str, str] = {}
    seen: set[str] = set()
    duplicates: list[str] = []
    for line in description.splitlines():
        stripped = line.lstrip(" \t")
        if not stripped.startswith("- "):
            continue
        payload = stripped[2:]
        if ": " not in payload:
            continue
        key, value = payload.split(": ", 1)
        key = key.strip()
        if key in seen:
            duplicates.append(key)
            continue
        seen.add(key)
        parsed[key] = value.rstrip()
    return parsed, duplicates


def test_fixture_declares_exact_artifact_path_and_verification_command() -> None:
    spec = _load_spec()

    # Metadata lives on the workflow description (NodeSpec extras are rejected
    # by `reject_unknown_spec_fields` at validate/deploy/draft time, so the
    # benchmark cannot smuggle benchmark metadata onto a node). Parse exact
    # values — substring containment would accept `output.md.extra` or
    # `output.md && false` without complaint.
    description = spec.description or ""
    parsed, duplicates = _parse_description(description)

    # Duplicate metadata keys must fail loud. An earlier wrong value
    # followed by a correct value (silent overwrite) is exactly the
    # regression this guard exists to catch.
    if duplicates:
        offending_singletons = sorted(set(duplicates) & set(SINGLETON_DESCRIPTION_KEYS))
        assert not offending_singletons, (
            f"fixture description declares metadata key(s) more than once — "
            f"singleton violation(s) {offending_singletons!r}; "
            f"all duplicates={sorted(set(duplicates))!r}"
        )

    # Both singleton keys must be declared, and exactly once.
    for key in SINGLETON_DESCRIPTION_KEYS:
        assert key in parsed, (
            f"fixture description missing required singleton key {key!r}; "
            f"description: {description!r}"
        )

    assert parsed["artifact_path"] == EXPECTED_ARTIFACT_PATH, (
        f"fixture description must pin exact artifact_path {EXPECTED_ARTIFACT_PATH!r}; "
        f"got {parsed.get('artifact_path')!r} from description: {description!r}"
    )
    assert parsed["verification_command"] == EXPECTED_VERIFICATION_COMMAND, (
        f"fixture description must pin exact verification_command "
        f"{EXPECTED_VERIFICATION_COMMAND!r}; got {parsed.get('verification_command')!r} "
        f"from description: {description!r}"
    )

    # The agent_task result_contract must carry the same artifact_path so the
    # runtime knows where to write.
    contract_paths = spec.nodes["agent_task"].result_contract.get("artifact_path")
    assert contract_paths == "string", (
        f"agent_task result_contract must include artifact_path: string; got {contract_paths!r}"
    )


def test_fixture_targets_designated_test_channel() -> None:
    """Pin only the strict-ingestion fields needed by the delayed node."""
    spec = _load_spec()
    send_node = spec.nodes["send_message"]
    assert send_node.platform == "local", (
        f"fixture send_message.platform must be exactly 'local'; "
        f"got {send_node.platform!r}"
    )
    assert send_node.target == EXPECTED_DELIVERY_CHANNEL, (
        f"fixture send_message.target must be exactly "
        f"{EXPECTED_DELIVERY_CHANNEL!r}; got {send_node.target!r}"
    )
    assert send_node.message == "Vertical-slice delivery for three-effect mission.", (
        "fixture send_message.message must be exactly "
        "'Vertical-slice delivery for three-effect mission.'; "
        f"got {send_node.message!r}"
    )
    assert send_node.not_before_seconds == 30, (
        "fixture send_message.not_before_seconds must be 30; "
        f"got {send_node.not_before_seconds!r}"
    )


def test_fixture_has_no_credential_address_or_recipient_fields() -> None:
    """The fixture must stay hermetic: no real tokens, addresses, or recipients.

    Walk every mapping recursively and reject every field in
    ``FORBIDDEN_FIXTURE_FIELDS`` wherever it appears — including inside
    nested ``agent_task`` output, prompt text, result contracts, and
    trigger config. send_message.output gets no special carve-out.

    The structural key ``to`` is **only** exempt at the exact path
    ``fixture.edges[*].to`` (routing primitives inherent to EdgeSpec).
    Anywhere else — including ``nodes.send_message.output.to`` — is
    forbidden. ``from`` and ``id`` are not in ``FORBIDDEN_FIXTURE_FIELDS``
    so they need no path-based exemption.
    """
    raw = _load_fixture_raw()
    load_spec_from_object(raw)  # fail loud if the fixture drifts from strict ingestion

    forbidden = {field.lower() for field in FORBIDDEN_FIXTURE_FIELDS}

    def _walk(obj: object, path: str) -> list[str]:
        offenders: list[str] = []
        if isinstance(obj, dict):
            for key, value in obj.items():
                child_path = f"{path}.{key}"
                if isinstance(key, str) and key.lower() in forbidden:
                    # ``to`` is allowed ONLY at fixture.edges[*].to; anywhere
                    # else (including nodes.send_message.output.to) is
                    # forbidden. Path-based exemption, not name-based.
                    is_edge_to = (
                        key.lower() == "to"
                        and child_path.startswith("fixture.edges[")
                        and child_path.endswith(".to")
                    )
                    if not is_edge_to:
                        offenders.append(f"{child_path}={value!r}")
                offenders.extend(_walk(value, child_path))
        elif isinstance(obj, list):
            for index, value in enumerate(obj):
                offenders.extend(_walk(value, f"{path}[{index}]"))
        return offenders

    offenders = _walk(raw, "fixture")
    assert not offenders, (
        "fixture must not embed credential/address/recipient fields anywhere; found: "
        + ", ".join(offenders)
    )


def test_implemented_primitive_errors_accepts_send_message() -> None:
    """Task 7 materializes send_message through the authorized outbox path."""
    spec = _load_spec()

    assert implemented_primitive_errors(spec) == []


def test_three_effect_fixture_materializes_one_replay_safe_outbox_row(tmp_path, monkeypatch) -> None:
    # The mission is created with profile="implementer"; _active_profile_name()
    # derives the active profile from HADES_HOME's layout (<home>/profiles/<name>),
    # so the fixture home must follow that convention for the profile check in
    # _materialize_send_message to match.
    home = tmp_path / ".hades" / "profiles" / "implementer"
    monkeypatch.setenv("HADES_HOME", str(home))
    state_db_path = home / "state.db"
    spec = _load_spec()

    wfdb.init_db()
    with wfdb.connect() as conn:
        wfdb.deploy_definition(conn, spec, created_by="benchmark")
        mission, execution = mdb.create_mission_and_execution(
            conn,
            workflow_id=spec.id,
            objective="exercise the three-effect benchmark fixture",
            constraints=[],
            authority={
                "allowed_effects": ["delayed_message"],
                "message_targets": [EXPECTED_DELIVERY_CHANNEL],
                "expires_at": 1_000,
            },
            evidence={"checks": ["workflow_succeeded"]},
            input_data={},
            profile="implementer",
            now=10,
        )
        first_token = "benchmark-first-claim"
        conn.execute(
            """
            UPDATE workflow_executions
               SET claim_lock = ?, claim_expires = ?, updated_at = ?
             WHERE execution_id = ?
            """,
            (first_token, 200, 100, execution.execution_id),
        )
        result = run_in_memory_until_waiting(
            spec,
            input_data={},
            completed_node_outputs={"agent_task": {"summary": "complete"}},
            completed_wait_nodes={"wait"},
        )
        assert result.status == "waiting"
        assert result.waiting_nodes == ["send_message"]
        assert workflows_dispatcher._finish(
            conn,
            execution_id=execution.execution_id,
            token=first_token,
            result=result,
            spec=spec,
            now=100,
            state_db_path=state_db_path,
        )
        node_run = conn.execute(
            """
            SELECT outbox_id FROM workflow_node_runs
             WHERE execution_id = ? AND node_id = 'send_message'
            """,
            (execution.execution_id,),
        ).fetchone()
    assert node_run is not None
    assert node_run["outbox_id"]

    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        outbox = store.get(execution.execution_id, "send_message")
        expected_outbox_id, expected_delivery_id = store._stable_ids(
            execution.execution_id, "send_message"
        )
        assert outbox is not None
        assert outbox.outbox_id == expected_outbox_id == node_run["outbox_id"]
        assert outbox.delivery_id == expected_delivery_id
        assert outbox.mission_id == mission.mission_id
        assert outbox.platform == "local"
        assert outbox.target == EXPECTED_DELIVERY_CHANNEL
        assert outbox.content == "Vertical-slice delivery for three-effect mission."
        assert outbox.not_before == 130
        assert outbox.status == "scheduled"
        assert state_db.get_effect_transaction(f"{outbox.outbox_id}:transaction") is not None
    finally:
        state_db.close()

    with wfdb.connect() as conn:
        replay_token = "benchmark-replay-claim"
        conn.execute(
            """
            UPDATE workflow_executions
               SET status = 'queued', claim_lock = ?, claim_expires = ?, updated_at = ?
             WHERE execution_id = ?
            """,
            (replay_token, 300, 101, execution.execution_id),
        )
        assert workflows_dispatcher._finish(
            conn,
            execution_id=execution.execution_id,
            token=replay_token,
            result=result,
            spec=spec,
            now=101,
            state_db_path=state_db_path,
        )

    state_db = SessionDB(db_path=state_db_path)
    try:
        replayed = MissionOutboxStore(state_db).get(execution.execution_id, "send_message")
        assert state_db._conn is not None
        rows = state_db._conn.execute(
            "SELECT outbox_id, delivery_id FROM mission_outbox WHERE execution_id = ? AND node_id = ?",
            (execution.execution_id, "send_message"),
        ).fetchall()
    finally:
        state_db.close()
    assert replayed is not None
    assert replayed.outbox_id == expected_outbox_id
    assert replayed.delivery_id == expected_delivery_id
    assert [(row["outbox_id"], row["delivery_id"]) for row in rows] == [
        (expected_outbox_id, expected_delivery_id)
    ]
