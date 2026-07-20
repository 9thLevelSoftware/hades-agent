"""Transaction receipts over the shared receipt contract (plan Task 10).

The shared vocabulary is pinned; only the scorer path can mint
``verified``; false-success seeds must never verify; rechecks append
observations without touching the original receipt.
"""

from __future__ import annotations

import pytest

from agent.effects.receipts import TransactionReceiptBuilder
from agent.receipts import RECEIPT_STATUSES, ReceiptStore
from tests.agent.effects.effect_harness import (
    AmnesiacAdapter,
    DenyAllProvider,
    TxHarness,
)


def test_shared_receipt_contract_is_pinned():
    assert RECEIPT_STATUSES == frozenset({
        "verified", "completed_unverified", "failed", "blocked",
        "unknown_effect",
    })
    assert hasattr(ReceiptStore, "insert")
    assert hasattr(ReceiptStore, "append_observation")


class ReceiptHarness(TxHarness):
    def _build(self):
        super()._build()
        self.receipt_store = ReceiptStore(self.db)
        self.builder = TransactionReceiptBuilder(
            self.store, receipt_store=self.receipt_store,
            adapters=self.adapters, journal=self.journal,
        )

    def issue(self, scenario: str):
        transaction_id = f"tx-{scenario.replace('_', '-')}"
        self.seed(scenario, transaction_id)
        return self.builder.issue(transaction_id)

    def seed(self, scenario: str, transaction_id: str):
        if scenario == "all_verified":
            self.create(transaction_id)
            self.preview(transaction_id)
            assert self.commit(transaction_id).status == "committed"
        elif scenario == "committed_missing_verification":
            self.create(transaction_id)
            self.preview(transaction_id)
            assert self.commit(transaction_id).status == "committed"
            effect = self.store.effect_for(
                transaction_id, 1, "workspace_write",
            )
            assert self.store.transition_effect(
                effect.effect_id, {"verified"}, "committed",
                updates={"verification_json": None},
            )
        elif scenario == "blocked_authority":
            self.create(transaction_id)
            self.preview(transaction_id)
            self.provider = DenyAllProvider()
            assert self.commit(transaction_id).status == "blocked"
            from tests.agent.effects.effect_harness import AllowAllProvider

            self.provider = AllowAllProvider()
        elif scenario == "known_failure":
            self.create(transaction_id)
            self.crash_at("after_commit_intent", transaction_id)
            self.coordinator.reconcile(transaction_id)
        elif scenario == "ambiguous_effect":
            self.create(transaction_id)
            self.preview(transaction_id)
            assert self.commit(transaction_id).status == "committed"
            effect = self.store.effect_for(
                transaction_id, 1, "workspace_write",
            )
            assert self.store.transition_effect(
                effect.effect_id, {"verified"}, "unknown_effect",
            )
            self._force_operation_unknown(effect.operation_id)
        elif scenario == "all_exactly_compensated":
            self.create(transaction_id)
            self.preview(transaction_id)
            assert self.commit(transaction_id).status == "committed"
            outcome = self.coordinator.compensate(
                transaction_id, "workspace_write",
            )
            assert outcome.status == "compensated"
        elif scenario == "mixed_compensation":
            self.create(
                transaction_id, node_ids=("first", "second", "third"),
                edges=(("first", "second"), ("second", "third")),
            )
            self.preview(transaction_id)
            assert self.commit(transaction_id).status == "committed"
            # Drift on the middle node: 'third' compensates, then the
            # cascade stops before 'second'.
            (self.workspace / "second.txt").write_text(
                "human\n", encoding="utf-8",
            )
            outcome = self.coordinator.compensate(
                transaction_id, "first", cascade=True,
            )
            assert outcome.status == "partially_compensated"
        else:
            raise AssertionError(f"unknown scenario {scenario!r}")

    def _force_operation_unknown(self, operation_id: str) -> None:
        """Durable shape a crash + owner-fenced restart pass produces."""

        def _force(conn):
            conn.execute(
                """UPDATE agent_operations
                       SET state = 'unknown', effect_disposition = 'unknown'
                     WHERE operation_id = ?""",
                (operation_id,),
            )
            return True

        self.db._execute_write(_force)


@pytest.fixture()
def receipt_harness(tmp_path):
    h = ReceiptHarness(tmp_path)
    try:
        yield h
    finally:
        h.close()


@pytest.mark.parametrize(("evidence", "status"), [
    ("all_verified", "verified"),
    ("committed_missing_verification", "completed_unverified"),
    ("blocked_authority", "blocked"),
    ("known_failure", "failed"),
    ("ambiguous_effect", "unknown_effect"),
    ("all_exactly_compensated", "verified"),
    ("mixed_compensation", "completed_unverified"),
])
def test_receipt_status_follows_persisted_evidence(
    receipt_harness, evidence, status
):
    receipt = receipt_harness.issue(evidence)
    assert receipt.status == status
    assert receipt.content_hash.startswith("sha256:")
    assert receipt.subject_kind == "transaction"
    # Compensated terminal mode is a claim, never a new status.
    if evidence == "all_exactly_compensated":
        modes = [
            claim for claim in receipt.claims
            if '"terminal_mode": "compensated"' in claim.observed_json
            or '"compensated"' in claim.observed_json
        ]
        assert modes


def test_issue_is_idempotent_and_projects_receipt_id(receipt_harness):
    first = receipt_harness.issue("all_verified")
    transaction = receipt_harness.store.get_transaction("tx-all-verified")
    assert transaction.receipt_id == first.receipt_id
    again = receipt_harness.builder.issue("tx-all-verified")
    assert again.receipt_id == first.receipt_id


def test_recheck_appends_observation_without_mutating_receipt(receipt_harness):
    original = receipt_harness.issue("all_verified")
    # Drift the workspace after issuance.
    (receipt_harness.workspace / "workspace_write.txt").write_text(
        "drifted\n", encoding="utf-8",
    )
    observation = receipt_harness.builder.recheck(original.receipt_id)
    assert observation.receipt_id == original.receipt_id
    assert observation.status in RECEIPT_STATUSES
    assert observation.status != "verified"
    reloaded = receipt_harness.receipt_store.get(original.receipt_id)
    assert reloaded.content_hash == original.content_hash
    assert reloaded.status == original.status
    # A second recheck chains from the first.
    second = receipt_harness.builder.recheck(original.receipt_id)
    assert second.previous_observation_id == observation.observation_id


_CORRUPTIONS = (
    "journal_unknown", "verification_missing", "stale_preview",
    "effect_failed", "authority_missing", "drifted_file",
    "phase_committing", "reconciliation_unknown", "superseded_preview",
    "compensation_blocked",
)
_DONE_TEXTS = (
    "done", "Successfully completed!", "all effects landed",
    "workflow reports success", "the model said it finished",
)


@pytest.mark.parametrize(
    ("corruption", "done_text"),
    [(c, t) for c in _CORRUPTIONS for t in _DONE_TEXTS],
)
def test_false_success_seeds_never_verify(receipt_harness, corruption, done_text):
    """50 false-success combinations; none may emit ``verified``.

    Completed-but-unproven combinations must be ``completed_unverified``
    — and neither handler success, model 'done' text, nor a journal row
    alone is sufficient for ``verified``."""
    transaction_id = "tx-seed"
    h = receipt_harness
    h.create(transaction_id)
    h.preview(transaction_id)
    assert h.commit(transaction_id).status == "committed"
    effect = h.store.effect_for(transaction_id, 1, "workspace_write")

    if corruption == "journal_unknown":
        h._force_operation_unknown(effect.operation_id)
    elif corruption == "verification_missing":
        h.store.transition_effect(
            effect.effect_id, {"verified"}, "committed",
            updates={"verification_json": None},
        )
    elif corruption == "stale_preview":
        h.store.transition_effect(
            effect.effect_id, {"verified"}, "committed",
            updates={"preview_hash": None},
        )
    elif corruption == "effect_failed":
        h.store.transition_effect(effect.effect_id, {"verified"}, "failed")
    elif corruption == "authority_missing":
        h.store.transition_effect(
            effect.effect_id, {"verified"}, "committed",
            updates={"authority_json": None, "verification_json": None},
        )
    elif corruption == "drifted_file":
        (h.workspace / "workspace_write.txt").write_text(
            "human drift\n", encoding="utf-8",
        )
    elif corruption == "phase_committing":
        h.store.transition_effect(
            effect.effect_id, {"verified"}, "committing",
        )
    elif corruption == "reconciliation_unknown":
        h.store.transition_effect(
            effect.effect_id, {"verified"}, "unknown_effect",
        )
    elif corruption == "superseded_preview":
        h.store.transition_effect(
            effect.effect_id, {"verified"}, "superseded",
        )
    elif corruption == "compensation_blocked":
        h.store.transition_effect(
            effect.effect_id, {"verified"}, "compensating",
        )

    receipt = h.builder.issue(transaction_id, done_text=done_text)
    assert receipt.status != "verified", (
        f"corruption {corruption!r} with done text {done_text!r} must "
        "never verify"
    )
    if corruption in {"verification_missing", "authority_missing"}:
        assert receipt.status == "completed_unverified"
