"""Pure deterministic planning for local autonomous profile management."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

import pytest

from agent.reasoning_support import ReasoningSupport
from plugins.auto_routing.auto_routing.inventory import ManagementInventoryCandidate
from plugins.auto_routing.auto_routing.management import (
    plan_management_revision,
    rank_management_candidates,
)
from plugins.auto_routing.auto_routing.models import (
    AccessEconomics,
    ObjectiveWeights,
    ProfileLimits,
    ProfileMatch,
    ReasoningBounds,
    RouteProfile,
    RoutingTarget,
    RuntimeKey,
)
from plugins.auto_routing.auto_routing.ranking_pack import (
    RankingPackRow,
    VerifiedRankingPack,
    VerifiedRankingPackMetadata,
)


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _key(name: str) -> RuntimeKey:
    return RuntimeKey(
        provider="openai",
        model=name,
        auth_identity="api-key:work",
        credential_pool_identity="pool:work",
        endpoint_identity="endpoint:work",
        api_mode="chat_completions",
        inventory_revision="inventory-current",
    )


def _target(name: str, *, status: str) -> RoutingTarget:
    return RoutingTarget(
        runtime=_key(name),
        reasoning=ReasoningBounds(default="medium", min="low", max="high"),
        supported_reasoning_efforts=("low", "medium", "high"),
        max_estimated_task_cost_usd=1.0,
        max_estimated_latency_seconds=10.0,
        revision_status=status,  # type: ignore[arg-type]
    )


@pytest.fixture
def profile() -> RouteProfile:
    return RouteProfile(
        profile_id="coding",
        description="Code-oriented routes.",
        match=ProfileMatch(
            domains=("coding",),
            complexity=("moderate",),
            modalities=("text",),
            capabilities=("tools",),
        ),
        objectives=ObjectiveWeights(
            quality=0.4,
            reliability=0.3,
            latency=0.2,
            cost=0.1,
        ),
        limits=ProfileLimits(
            max_estimated_task_cost_usd=1.0,
            max_estimated_latency_seconds=10.0,
        ),
        primary=_target("current", status="active"),
        fallbacks=(_target("existing-fallback", status="fallback"),),
        provenance=("user-approved",),
    )


def _candidate(
    runtime_id: str,
    name: str,
    *,
    verification_source: str = "authenticated_live",
    verification_expires_at: str = "2026-07-18T13:00:00Z",
    resolver_name: str = "openai:work",
    **capabilities: object,
) -> ManagementInventoryCandidate:
    return ManagementInventoryCandidate(
        runtime_id=runtime_id,
        key=_key(name),
        resolver_name=resolver_name,
        economics=AccessEconomics(
            billing_kind="metered",
            effective_marginal_cost_usd_per_task=0.5,
            source_id="verified-economics",
            provenance="inventory",
            observed_at="2026-07-18T12:00:00Z",
        ),
        reasoning_support=ReasoningSupport(
            efforts=("low", "medium", "high"),
            provider_aliases=(),
            provenance="verified:reasoning",
            exact=True,
        ),
        verification_source=verification_source,
        verification_expires_at=verification_expires_at,
        capabilities={
            "supports_tools": True,
            "context_window": 128_000,
            "estimated_latency_seconds": 1.0,
            **capabilities,
        },
    )


@pytest.fixture
def candidates() -> tuple[ManagementInventoryCandidate, ...]:
    return (
        _candidate("b" * 64, "candidate-b"),
        _candidate("a" * 64, "candidate-a"),
    )


@pytest.fixture
def pack() -> VerifiedRankingPack:
    return VerifiedRankingPack(
        metadata=VerifiedRankingPackMetadata(
            ranking_pack_id="ranking-pack-1",
            ranking_pack_sha256="f" * 64,
            schema_version="1",
            verified_at="2026-07-18T12:00:00Z",
        ),
        rankings=(
            RankingPackRow("a" * 64, 0.9, 0.8, 0.2, 0.3),
            RankingPackRow("b" * 64, 0.9, 0.8, 0.2, 0.3),
        ),
    )


def test_rank_uses_profile_objectives_and_runtime_id_tiebreaker(
    profile: RouteProfile,
    candidates: tuple[ManagementInventoryCandidate, ...],
    pack: VerifiedRankingPack,
) -> None:
    ranked = rank_management_candidates(profile, candidates, pack)

    assert [item.runtime_id for item in ranked if item.eligible] == [
        "a" * 64,
        "b" * 64,
    ]
    assert ranked[0].score == pytest.approx(0.83)


def test_primary_upgrade_becomes_challenger_not_immediate_primary(
    profile: RouteProfile,
    candidates: tuple[ManagementInventoryCandidate, ...],
    pack: VerifiedRankingPack,
) -> None:
    improved_pack = VerifiedRankingPack(
        metadata=pack.metadata,
        rankings=(
            RankingPackRow("a" * 64, 0.8, 0.8, 0.2, 0.3),
            RankingPackRow("b" * 64, 0.95, 0.9, 0.1, 0.1),
        ),
    )

    plan = plan_management_revision(
        profile=profile,
        candidates=candidates,
        pack=improved_pack,
        active_assignments=(),
        now=NOW,
    )

    assert plan.action == "propose_canary"
    assert plan.after_profile.primary == profile.primary
    assert (
        plan.after_profile.primary_challengers[0].runtime.stable_id()
        == _key("candidate-b").stable_id()
    )
    assert plan.patch.before_runtime_ids == plan.before_runtime_ids
    assert plan.patch.after_runtime_ids == plan.after_runtime_ids


@dataclass(frozen=True)
class _ActiveAssignment:
    runtime_id: str


def test_planner_preserves_viable_route_and_unfinished_assignment(
    profile: RouteProfile,
    candidates: tuple[ManagementInventoryCandidate, ...],
    pack: VerifiedRankingPack,
) -> None:
    assignment = _ActiveAssignment(profile.fallbacks[0].runtime.stable_id())
    plan = plan_management_revision(
        profile=profile,
        candidates=candidates,
        pack=pack,
        active_assignments=(assignment,),  # type: ignore[arg-type]
        now=NOW,
    )

    assert plan.action == "hold"
    assert plan.reason_codes == ("unsafe_mutation",)


def test_planner_removes_unranked_fallbacks_from_managed_membership(
    profile: RouteProfile,
    pack: VerifiedRankingPack,
) -> None:
    second_fallback = _target("another-fallback", status="fallback")
    profile = profile.model_copy(
        update={"fallbacks": (profile.fallbacks[0], second_fallback)}
    )
    primary_id = profile.primary.runtime.stable_id()
    current_primary = replace(
        _candidate(primary_id, "current"),
        key=profile.primary.runtime,
    )
    primary_pack = VerifiedRankingPack(
        metadata=pack.metadata,
        rankings=(RankingPackRow(primary_id, 0.9, 0.9, 0.1, 0.1),),
    )

    plan = plan_management_revision(
        profile=profile,
        candidates=(current_primary,),
        pack=primary_pack,
        active_assignments=(),
        now=NOW,
    )

    assert plan.action == "fallback_reorder"
    assert plan.after_profile is not None
    assert plan.after_profile.fallbacks == ()


def test_planner_inserts_qualified_lower_ranked_candidate_as_fallback(
    profile: RouteProfile,
    pack: VerifiedRankingPack,
) -> None:
    primary_id = profile.primary.runtime.stable_id()
    current_primary = replace(
        _candidate(primary_id, "current"),
        key=profile.primary.runtime,
    )
    new_fallback = _candidate(
        _key("qualified-fallback").stable_id(), "qualified-fallback"
    )
    ranked_pack = VerifiedRankingPack(
        metadata=pack.metadata,
        rankings=(
            RankingPackRow(primary_id, 0.99, 0.99, 0.01, 0.01),
            RankingPackRow(new_fallback.runtime_id, 0.80, 0.80, 0.20, 0.20),
        ),
    )

    plan = plan_management_revision(
        profile=profile,
        candidates=(current_primary, new_fallback),
        pack=ranked_pack,
        active_assignments=(),
        now=NOW,
    )

    assert plan.action == "fallback_reorder"
    assert plan.after_profile is not None
    assert tuple(
        target.runtime.stable_id() for target in plan.after_profile.fallbacks
    ) == (new_fallback.runtime_id,)


def test_existing_fallback_that_becomes_leader_is_elevated_for_canary(
    profile: RouteProfile,
    pack: VerifiedRankingPack,
) -> None:
    primary_id = profile.primary.runtime.stable_id()
    fallback = profile.fallbacks[0]
    fallback_id = fallback.runtime.stable_id()
    current_primary = replace(
        _candidate(primary_id, "current"),
        key=profile.primary.runtime,
    )
    ranked_fallback = replace(
        _candidate(fallback_id, "existing-fallback"),
        key=fallback.runtime,
    )
    ranked_pack = VerifiedRankingPack(
        metadata=pack.metadata,
        rankings=(
            RankingPackRow(fallback_id, 0.99, 0.99, 0.01, 0.01),
            RankingPackRow(primary_id, 0.70, 0.70, 0.30, 0.30),
        ),
    )

    plan = plan_management_revision(
        profile=profile,
        candidates=(current_primary, ranked_fallback),
        pack=ranked_pack,
        active_assignments=(),
        now=NOW,
    )

    assert plan.action == "propose_canary"
    assert plan.after_profile is not None
    assert plan.after_profile.primary_challengers[0].runtime == fallback.runtime
    assert plan.after_profile.fallbacks == ()


@pytest.mark.parametrize(
    ("candidate", "profile_update", "local", "rank_present", "reason_code"),
    [
        (
            _candidate("c" * 64, "unverified", verification_source=""),
            {},
            False,
            False,
            "inventory_not_verified",
        ),
        (
            _candidate("c" * 64, "no-tools", supports_tools=False),
            {},
            False,
            False,
            "local_capability_missing",
        ),
        (_candidate("c" * 64, "not-ranked"), {}, False, False, "ranking_missing"),
        (
            _candidate("c" * 64, "license", license_id="proprietary"),
            {"limits": ProfileLimits(allowed_licenses=("apache-2.0",))},
            True,
            True,
            "license_rejected",
        ),
        (
            _candidate("c" * 64, "context", context_window=1),
            {"limits": ProfileLimits(minimum_context_tokens=2)},
            False,
            True,
            "context_limit_rejected",
        ),
    ],
)
def test_rank_uses_first_matching_ordered_rejection_code(
    profile: RouteProfile,
    pack: VerifiedRankingPack,
    candidate: ManagementInventoryCandidate,
    profile_update: dict[str, object],
    local: bool,
    rank_present: bool,
    reason_code: str,
) -> None:
    if local:
        candidate = replace(
            candidate,
            key=candidate.key.model_copy(update={"local_backend": "ollama"}),
        )
    if rank_present:
        pack = VerifiedRankingPack(
            metadata=pack.metadata,
            rankings=(*pack.rankings, RankingPackRow("c" * 64, 0.5, 0.5, 0.5, 0.5)),
        )
    ranked = rank_management_candidates(
        profile.model_copy(update=profile_update),
        (candidate,),
        pack,
    )

    assert ranked[0].eligible is False
    assert ranked[0].reason_codes == (reason_code,)


def test_planner_holds_when_new_challenger_would_evict_an_active_assignment(
    profile: RouteProfile,
    candidates: tuple[ManagementInventoryCandidate, ...],
    pack: VerifiedRankingPack,
) -> None:
    challengers = tuple(
        _target(f"challenger-{index}", status="challenger") for index in range(64)
    )
    fallbacks = tuple(
        _target(f"fallback-{index}", status="fallback") for index in range(64)
    )
    full_profile = profile.model_copy(
        update={"primary_challengers": challengers, "fallbacks": fallbacks}
    )
    assigned = _ActiveAssignment(fallbacks[-1].runtime.stable_id())

    plan = plan_management_revision(
        profile=full_profile,
        candidates=candidates,
        pack=pack,
        active_assignments=(assigned,),  # type: ignore[arg-type]
        now=NOW,
    )

    assert plan.action == "hold"
    assert plan.reason_codes == ("unsafe_mutation",)


def test_new_challenger_demotes_only_an_unassigned_challenger_to_fallback(
    profile: RouteProfile,
    candidates: tuple[ManagementInventoryCandidate, ...],
    pack: VerifiedRankingPack,
) -> None:
    challengers = tuple(
        _target(f"challenger-{index}", status="challenger") for index in range(64)
    )
    profile = profile.model_copy(
        update={"primary_challengers": challengers, "fallbacks": ()}
    )

    plan = plan_management_revision(
        profile=profile,
        candidates=candidates,
        pack=pack,
        active_assignments=(),
        now=NOW,
    )

    assert plan.action == "propose_canary"
    assert plan.after_profile.fallbacks[0].runtime == challengers[-1].runtime
    assert plan.after_profile.fallbacks[0].revision_status == "fallback"


def test_planner_holds_for_conflicting_duplicate_candidate_runtime_ids(
    profile: RouteProfile,
    candidates: tuple[ManagementInventoryCandidate, ...],
    pack: VerifiedRankingPack,
) -> None:
    valid_leader = candidates[0]
    conflicting_duplicate = replace(
        valid_leader,
        key=_key("conflicting-duplicate"),
        reasoning_support=ReasoningSupport(
            efforts=(),
            provider_aliases=(),
            provenance="conflicting:reasoning",
            exact=False,
        ),
    )

    plan = plan_management_revision(
        profile=profile,
        candidates=(valid_leader, conflicting_duplicate),
        pack=pack,
        active_assignments=(),
        now=NOW,
    )

    assert plan.action == "hold"
    assert plan.patch is None
    assert plan.reason_codes == ("unsafe_mutation",)
