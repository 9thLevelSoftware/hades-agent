"""Preferences & Autonomy Center — stable public authority contract.

Consumers (action transactions, middleware, CLI/TUI/Dashboard surfaces)
import from this package, never from submodules' private helpers. The
records here are frozen; ``AUTONOMY_CONTRACT_SCHEMA`` names the wire/
storage schema version.
"""

from agent.autonomy.models import (
    ACTION_CLASSES,
    AUTONOMY_CONTRACT_SCHEMA,
    ActionContext,
    AuthorityDecision,
    AuthorityDecisionDraft,
    AutonomyContract,
    AutonomyRule,
    BudgetReservation,
    ClarificationRequest,
    CostConstraint,
    DataClass,
    DecisionStage,
    DecisionVerdict,
    EvidenceRequirement,
    EvidenceStage,
    Reversibility,
    RuleEffect,
    RuleProvenance,
    RuleScope,
    RuleSource,
    RuleState,
    TimeConstraint,
)

__all__ = [
    "ACTION_CLASSES",
    "AUTONOMY_CONTRACT_SCHEMA",
    "ActionContext",
    "AuthorityDecision",
    "AuthorityDecisionDraft",
    "AutonomyContract",
    "AutonomyRule",
    "BudgetReservation",
    "ClarificationRequest",
    "CostConstraint",
    "DataClass",
    "DecisionStage",
    "DecisionVerdict",
    "EvidenceRequirement",
    "EvidenceStage",
    "Reversibility",
    "RuleEffect",
    "RuleProvenance",
    "RuleScope",
    "RuleSource",
    "RuleState",
    "TimeConstraint",
]
