# Effect Adapter SDK

An effect adapter owns the effect-specific behavior of one action
family inside a reversible action transaction:
`normalize → prepare → preview → commit → verify → reconcile →
compensate`. The generic coordinator owns everything else — graph
revisions, authority rechecks, journal ordering, dependency-aware
compensation, receipts, and recovery.

Vendor integrations belong in standalone plugin repositories or MCP
servers; the abstract base class widens only when a concrete consumer
needs it.

## Descriptor: declared capabilities are verified

```python
from agent.effects import AdapterDescriptor, EffectAdapter, register_effect_adapter

class MyAdapter(EffectAdapter):
    descriptor = AdapterDescriptor(
        adapter_id="myservice.v1",       # versioned id, required
        actions=frozenset({"annotate"}),
        idempotency="keyed",             # none | keyed | native
        reconciliation="query",          # none | query
        compensation="semantic",         # exact | semantic | none
        irreversible_after="dispatch",   # never | dispatch | commit
        compensation_window_seconds=3600,
    )
```

Registration (`register_effect_adapter(MyAdapter())`) rejects, loudly:

- an empty or unversioned adapter id, or an empty action set;
- `reconciliation="query"` when `reconcile()` is inherited unchanged;
- any compensation claim without a concrete `compensate()` override;
- `compensation_window_seconds` with `compensation="none"`;
- `irreversible_after="never"` with no compensation path;
- duplicate adapter ids.

An adapter cannot manufacture guarantees: exact reversal, semantic
compensation, native idempotency, query reconciliation, and the
irreversible boundary are separate declared capabilities, each proven
by a real method.

## Method contracts

All requests and results are frozen dataclasses from
`agent.effects.models`. Adapters never mutate transaction status.

- `normalize(node, context) -> NormalizedEffect` — validate and
  canonicalize arguments; raise `EffectBlocked` for anything the
  adapter will not do.
- `prepare(effect, context) -> PreparedEffect` — capture the durable
  before-state, expected after-state, canonical resource keys, the
  dotted `action_class`, declared `data_classes`, and semantics.
  Prepare performs **no outward effect**.
- `preview(prepared, context) -> EffectPreview` — a redacted,
  human-facing summary. Never include credentials or raw secrets.
- `commit(request, context) -> CommitOutcome` — perform the effect
  exactly once. `request.invoke` optionally carries the existing tool
  handler (single use). `request.operation_id` /
  `request.idempotency_key` are stable across retries.
- `verify(outcome, context) -> VerificationResult` — reread durable
  state and record evidence (e.g. after-hashes).
- `reconcile(effect, context) -> ReconciliationResult` — classify a
  possibly-crashed effect from durable evidence only: `landed`,
  `not_landed`, or `unknown`. Never re-invoke the handler; never guess.
- `compensate(request, context) -> CompensationResult` — apply the
  declared counter-action, or return `status="blocked"` with the exact
  reason (drift, boundary, expired window). Fidelity in the result must
  match the descriptor claim.

## Non-negotiables

- **Thread/process safety:** adapters may be called from CLI, TUI
  gateway, and recovery paths; derive state from durable stores, not
  instance attributes.
- **Profile isolation:** every path resolves inside the active profile
  home; a transaction never crosses it.
- **Stable operation ids:** commit uses the coordinator-supplied
  operation id; compensation ids derive from
  `sha256("compensate\0" + effect_id + "\0" + verified_result_hash)`.
- **Unknown is unknown:** if the effect may have landed but cannot be
  proven, return `unknown` — the coordinator freezes the frontier and
  requires reconciliation. No exception in `commit` is a license to
  retry.
- **Redaction:** previews, results, and evidence carry hashes and
  bounded summaries, never credentials.
- **Real-path tests required:** exercise a temporary profile home, real
  SQLite, real files/stores, and a process-restart (fresh object graph)
  recovery case. Mock only the final network boundary.

## Complete standalone-plugin example

A plugin that annotates a hypothetical tracker with one semantic
compensation action, registered without modifying core:

```python
# hermes_plugins/tracker_adapter/__init__.py
from dataclasses import replace

from agent.effects import (
    AdapterDescriptor, CommitOutcome, EffectAdapter, EffectPreview,
    EffectSemantics, NormalizedEffect, PreparedEffect,
    ReconciliationResult, CompensationResult, VerificationResult,
    register_effect_adapter,
)


class TrackerAnnotateAdapter(EffectAdapter):
    descriptor = AdapterDescriptor(
        adapter_id="tracker-annotate.v1",
        actions=frozenset({"annotate"}),
        idempotency="keyed",
        reconciliation="query",
        compensation="semantic",
        irreversible_after="never",
    )

    def __init__(self, client):
        self._client = client  # your bounded API client

    def normalize(self, node, context):
        args = dict(node.args)
        if not args.get("issue_id") or not args.get("note"):
            raise ValueError("annotate requires issue_id and note")
        return NormalizedEffect(
            node_id=node.node_id, adapter_id=self.descriptor.adapter_id,
            action="annotate", args=args,
            resource_keys=(f"tracker:{args['issue_id']}",),
        )

    def prepare(self, effect, context):
        return PreparedEffect(
            node_id=effect.node_id, adapter_id=effect.adapter_id,
            action="annotate", action_class="tracker.annotate",
            args=dict(effect.args), resources=effect.resource_keys,
            semantics=EffectSemantics(
                fidelity="semantic", reconciliation="query",
                idempotency="keyed", irreversible_after="never",
            ),
            before={"issue_id": effect.args["issue_id"]},
            data_classes=("internal",),
        )

    def preview(self, prepared, context):
        return EffectPreview(
            node_id=prepared.node_id,
            summary=f"annotate issue {prepared.args['issue_id']}",
            before=dict(prepared.before), after={"annotated": True},
            resources=prepared.resources, semantics=prepared.semantics,
            requires_approval=False,
        )

    def commit(self, request, context):
        note_id = self._client.annotate(
            request.prepared.args["issue_id"],
            request.prepared.args["note"],
            idempotency_key=request.idempotency_key,
        )
        return CommitOutcome(
            status="committed", result={"note_id": note_id},
            evidence={"note_id": note_id},
        )

    def verify(self, outcome, context):
        exists = self._client.note_exists(outcome.evidence["note_id"])
        return VerificationResult(
            verified=bool(exists), evidence=dict(outcome.evidence),
        )

    def reconcile(self, effect, context):
        note_id = ((effect.result or {}).get("evidence") or {}).get("note_id")
        if not note_id:
            return ReconciliationResult(disposition="unknown", evidence={})
        exists = self._client.note_exists(note_id)
        return ReconciliationResult(
            disposition="landed" if exists else "not_landed",
            evidence={"note_id": note_id},
        )

    def compensate(self, request, context):
        # Semantic: append a retraction note; the original stays visible.
        self._client.annotate(
            request.prepared.args["issue_id"],
            "[retracted by transaction compensation]",
            idempotency_key=f"comp-{request.verified_result_hash[:16]}",
        )
        return CompensationResult(
            fidelity="semantic", status="compensated",
            evidence={"retraction": True},
        )


def register(client):
    register_effect_adapter(TrackerAnnotateAdapter(client))
```

Then attach the adapter's action to a plan node with
`adapter_id: tracker-annotate.v1` / `action: annotate`. The
coordinator handles everything else.
