"""Shared CLI surface for verified outcome and artifact receipts.

One parser, one profile-local service wiring, and one set of truthful
renderers behind every receipt control surface:

- top-level ``hades receipt ...`` (with the ``receipts`` alias) through
  :func:`build_parser` / :func:`receipt_command` in ``hades_cli/main.py``;
- classic ``/receipt`` (and ``/receipts``) in ``cli.py`` through
  :func:`run_slash`;
- programmatic/native invocation through :func:`run_argv`, which returns
  structured records independent of Rich for the TUI RPC and tests.

Truthfulness rules baked into rendering: ``completed_unverified`` is
never presented as verified truth (the word for it is "claimed, not
independently verified"), and ``unknown_effect`` is never presented as
a failure or as retry-safe — the effect may or may not have happened,
so the only safe follow-up is recheck/reconcile. Attestations are
labeled "provenance only": a signature proves who produced a content
hash, never that its claims are true.

Exit codes: 0 ok, 2 validation/unknown-ID/plan-mismatch,
3 signing provider unavailable when required, 4 storage failure.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shlex
import sqlite3
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Literal, Optional, Sequence

import yaml

from hades_cli.workspace_context import get_workspace_root

__all__ = [
    "ReceiptCommandResult",
    "build_parser",
    "receipt_command",
    "run_argv",
    "run_slash",
]

EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_UNAVAILABLE = 3
EXIT_STORAGE = 4

# Bounded input: at most 64 UTF-8 arguments and 64 KiB total.
_MAX_ARGS = 64
_MAX_TOTAL_ARG_BYTES = 65_536

_RECEIPT_ACTIONS = (
    "list",
    "show",
    "claims",
    "recheck",
    "export",
    "verify-signature",
    "retention-plan",
    "prune",
)

_SUBJECT_KINDS = ("turn", "mission", "transaction", "external")

_PROVIDER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_MAX_LIST_LIMIT = 500

# Plain-language status notes. Never say "success" for
# completed_unverified; never say failure or imply retry safety for
# unknown_effect.
_STATUS_NOTES = {
    "verified": "independently scored end state",
    "completed_unverified": (
        "completion claimed by the producer — not independently verified"
    ),
    "failed": "the requested end state does not hold",
    "blocked": "execution was blocked before the end state could hold",
    "unknown_effect": (
        "the effect may or may not have happened. Do not retry the "
        "effect; recheck and reconcile evidence"
    ),
}

_DEFAULT_RECEIPTS_CONFIG: dict = {
    "mode": "off",
    "retention_days": 365,
    "artifact_locator_retention_days": 90,
    "export_redaction": "public",
    "signing": {"provider": "", "required": False},
}

_SLASH_HELP = """receipt — inspect verified outcome and artifact receipts
usage: /receipt <subcommand> [options]

  list [--status S] [--subject K] [--limit N] [--json]
  show RECEIPT_ID [--observation latest|all|OBS_ID] [--json]
  claims RECEIPT_ID [--json]
  recheck RECEIPT_ID [--json]
  export RECEIPT_ID --output PATH [--redaction public|local]
                    [--bundle-artifacts] [--sign]
  verify-signature RECEIPT_ID [--json]
  retention-plan [--at RFC3339] [--json]
  prune --confirm-plan PLAN_HASH [--json]

A receipt records what was asked, what evidence exists, and what was
independently verified. A recheck appends a new observation; it never
rewrites the original. Alias: /receipts. Same grammar as
`hades receipt ...`."""


class _CliUsageError(Exception):
    """Argument/validation failure — renders a message and exits 2."""


class _ReceiptArgumentParser(argparse.ArgumentParser):
    """Argparse that raises instead of calling ``sys.exit`` on errors."""

    def error(self, message: str) -> None:  # noqa: D401 - argparse contract
        raise _CliUsageError(
            f"{self.format_usage()}{self.prog}: error: {message}"
        )


@dataclass(frozen=True)
class ReceiptCommandResult:
    """One executed receipt command: exit code, rendered text, payload."""

    exit_code: int
    output: str
    payload: Optional[dict] = None

    @property
    def stdout(self) -> str:
        return self.output

    @property
    def json(self) -> Optional[dict]:
        return self.payload


@dataclass(frozen=True)
class _Outcome:
    exit_code: int
    payload: dict
    lines: tuple[str, ...]


def _usage(message: str) -> _CliUsageError:
    return _CliUsageError(f"error: {message}")


def _clip(value: object, limit: int = 200) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _status_label(status: str) -> str:
    note = _STATUS_NOTES.get(status)
    return f"{status} ({note})" if note else str(status)


# ── Profile-local service wiring ────────────────────────────────────────────


def _load_receipts_config(home: Path) -> dict:
    """Read the ``receipts:`` section of the profile's ``config.yaml``.

    Invalid or missing values fall back to the safe defaults; YAML 1.1
    parses a bare ``off`` as ``False``, which is normalized back.
    """
    merged: dict = json.loads(json.dumps(_DEFAULT_RECEIPTS_CONFIG))
    try:
        raw = yaml.safe_load((home / "config.yaml").read_text("utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        raw = None
    section = raw.get("receipts") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return merged
    mode = section.get("mode")
    if mode is False:
        mode = "off"
    if isinstance(mode, str) and mode in ("off", "capture", "require"):
        merged["mode"] = mode
    retention_days = section.get("retention_days")
    if isinstance(retention_days, int) and 1 <= retention_days <= 3650:
        merged["retention_days"] = retention_days
    locator_days = section.get("artifact_locator_retention_days")
    if (
        isinstance(locator_days, int)
        and 1 <= locator_days <= merged["retention_days"]
    ):
        merged["artifact_locator_retention_days"] = locator_days
    redaction = section.get("export_redaction")
    if redaction in ("public", "local"):
        merged["export_redaction"] = redaction
    signing = section.get("signing")
    if isinstance(signing, dict):
        provider = signing.get("provider")
        if isinstance(provider, str) and (
            provider == "" or _PROVIDER_RE.match(provider)
        ):
            merged["signing"]["provider"] = provider
        merged["signing"]["required"] = bool(signing.get("required", False))
    return merged


class _Services:
    """Lazily wired receipt services over one profile's ``SessionDB``."""

    def __init__(self, home: Path, db: Any) -> None:
        self.home = home
        self.workspace_root = get_workspace_root()
        self.db = db
        from agent.receipt_store import ReceiptStore

        self.store = ReceiptStore(db)
        self.config = _load_receipts_config(home)

    @property
    def mode(self) -> str:
        return str(self.config.get("mode", "off"))

    def signing_service(self):
        from agent.receipt_security import ReceiptSigningService

        return ReceiptSigningService.from_config(
            {"receipts": self.config}, store=self.store
        )

    def exporter(self):
        from agent.receipt_security import ReceiptExporter

        return ReceiptExporter(
            self.store,
            default_redaction=str(self.config.get("export_redaction", "public")),
            allowed_roots=(self.workspace_root,),
            signing=self.signing_service(),
        )

    def retention_service(self):
        from agent.receipt_security import ReceiptRetentionService

        return ReceiptRetentionService(
            self.store,
            retention_days=int(self.config["retention_days"]),
            locator_retention_days=int(
                self.config["artifact_locator_retention_days"]
            ),
        )

    def issuer(self):
        from agent.receipt_ingest import build_receipt_issuer

        return build_receipt_issuer(
            self.db, allowed_roots=(self.workspace_root,)
        )


@contextmanager
def _services() -> Iterator[_Services]:
    """Open the active profile's ``SessionDB`` for one invocation."""
    from hades_constants import get_hades_home
    from hades_state import SessionDB

    home = Path(get_hades_home())
    db = SessionDB(db_path=home / "state.db")
    try:
        yield _Services(home, db)
    finally:
        try:
            db.close()
        except Exception:  # pragma: no cover - close is best-effort
            pass


def _require_receipt(services: _Services, receipt_id: str):
    receipt = services.store.get(str(receipt_id))
    if receipt is None:
        raise _usage(
            f"unknown receipt {_clip(receipt_id, 80)!r} — run "
            "`hades receipt list` to see stored receipts"
        )
    return receipt


# ── Structured record builders (independent of Rich) ────────────────────────


def _claim_edges(claims) -> list[dict]:
    """Every claim→evidence→artifact edge as one structured record."""
    return [
        {
            "claim_id": claim.claim_id,
            "claim_kind": claim.claim_kind,
            "statement": claim.statement,
            "verdict": claim.verdict,
            "required": claim.required,
            "evidence_ids": list(claim.evidence_ids),
            "artifact_ids": list(claim.artifact_ids),
            "uncertainty": list(claim.uncertainty),
        }
        for claim in claims
    ]


def _attestations_for(services: _Services, receipt, observations) -> list:
    attestations = list(services.store.list_attestations(receipt.receipt_id))
    for observation in observations:
        attestations.extend(
            services.store.list_attestations(observation.observation_id)
        )
    return attestations


def _render_claim_lines(claims) -> list[str]:
    lines: list[str] = []
    for claim in claims:
        lines.append(f"  {claim.claim_id} [{claim.verdict}] {claim.statement}")
        lines.append(
            "    evidence: "
            + (", ".join(claim.evidence_ids) if claim.evidence_ids else "(none)")
        )
        lines.append(
            "    artifacts: "
            + (", ".join(claim.artifact_ids) if claim.artifact_ids else "(none)")
        )
        for note in claim.uncertainty:
            lines.append(f"    uncertainty: {note}")
    return lines


def _render_attestation_lines(attestations) -> list[str]:
    lines = [
        "Attestations (provenance only — a signature never proves truth):"
    ]
    if not attestations:
        lines.append("  (none recorded)")
        return lines
    for attestation in attestations:
        lines.append(
            f"  {attestation.attestation_id} provider={attestation.provider_id} "
            f"state={attestation.verification_state} "
            f"target={attestation.target_id}"
        )
    return lines


# ── Handlers (each returns an _Outcome) ─────────────────────────────────────


def _cmd_list(services: _Services, args: argparse.Namespace) -> _Outcome:
    from agent.receipt_models import ReceiptQuery

    limit = int(getattr(args, "limit", 50) or 50)
    if not 1 <= limit <= _MAX_LIST_LIMIT:
        raise _usage(f"--limit must be in 1..{_MAX_LIST_LIMIT}")
    query = ReceiptQuery(
        status=getattr(args, "status", None),
        subject_kind=getattr(args, "subject", None),
        limit=limit,
    )
    summaries = services.store.list(query)
    payload = {
        "ok": True,
        "action": "list",
        "receipts": [asdict(summary) for summary in summaries],
    }
    lines = [f"RECEIPTS ({len(summaries)})"]
    for summary in summaries:
        lines.append(
            f"  {summary.receipt_id}  {summary.subject_kind}:"
            f"{summary.subject_id}  {_status_label(summary.status)}  "
            f"decided {summary.decided_at}"
        )
    if not summaries:
        lines.append("  (no receipts match)")
    if services.mode == "off":
        note = (
            "note: receipts.mode is 'off' — capture is disabled; stored "
            "receipts remain readable"
        )
        lines.append(note)
        payload["warning"] = note
    return _Outcome(EXIT_OK, payload, tuple(lines))


def _select_observations(services: _Services, receipt, selector: str):
    observations = services.store.observations(receipt.receipt_id)
    if selector == "all" or selector == "latest":
        return observations
    chosen = [o for o in observations if o.observation_id == selector]
    if not chosen:
        raise _usage(
            f"unknown observation {_clip(selector, 80)!r} for receipt "
            f"{receipt.receipt_id}"
        )
    return tuple(chosen)


def _cmd_show(services: _Services, args: argparse.Namespace) -> _Outcome:
    receipt = _require_receipt(services, args.receipt_id)
    selector = str(getattr(args, "observation", None) or "latest")
    observations = _select_observations(services, receipt, selector)
    attestations = _attestations_for(services, receipt, observations)
    payload = {
        "ok": True,
        "action": "show",
        "receipt": asdict(receipt),
        "observations": [asdict(observation) for observation in observations],
        "claim_edges": _claim_edges(receipt.claims),
        "attestations": [asdict(attestation) for attestation in attestations],
    }

    outcome = receipt.requested_outcome
    lines = [
        f"Receipt {receipt.receipt_id}",
        f"Subject: {receipt.subject_kind} {receipt.subject_id}"
        + (f" (session {receipt.session_id})" if receipt.session_id else ""),
        f"Requested outcome: {outcome.outcome_kind} — {outcome.description}",
    ]
    if outcome.constraints:
        lines.append("  constraints: " + "; ".join(outcome.constraints))
    lines.append(
        f"Original: {_status_label(receipt.status)}"
        f" — decided {receipt.decided_at} by {receipt.scorer_id} "
        f"v{receipt.scorer_version}"
    )
    if observations:
        shown = observations if selector == "all" else observations[-1:]
        latest = observations[-1]
        if selector != "all":
            lines.append(
                f"Latest recheck: {_status_label(latest.status)}"
                f" — observed {latest.observed_at} by {latest.scorer_id} "
                f"v{latest.scorer_version}"
            )
        else:
            lines.append(f"Recheck observations ({len(observations)}):")
        for observation in shown:
            if selector == "all":
                lines.append(
                    f"  {observation.observation_id} "
                    f"{_status_label(observation.status)} "
                    f"observed {observation.observed_at}"
                )
            for note in observation.uncertainty:
                lines.append(f"  uncertainty: {note}")
    else:
        lines.append("Latest recheck: none recorded yet")
    lines.append("Claims (claim → evidence → artifacts):")
    lines.extend(_render_claim_lines(receipt.claims))
    if receipt.evidence:
        lines.append("Evidence:")
        for evidence in receipt.evidence:
            lines.append(
                f"  {evidence.evidence_id} {evidence.evidence_kind} "
                f"{evidence.source_ref} observed {evidence.observed_at}"
            )
    if receipt.artifacts:
        lines.append("Artifacts:")
        for artifact in receipt.artifacts:
            lines.append(
                f"  {artifact.artifact_id} {artifact.display_name} "
                f"{artifact.sha256} ({artifact.size_bytes} bytes)"
            )
    if receipt.uncertainty:
        lines.append("Uncertainty:")
        for note in receipt.uncertainty:
            lines.append(f"  - {note}")
    lines.extend(_render_attestation_lines(attestations))
    lines.append(
        f"Recheck now:  hades receipt recheck {receipt.receipt_id}"
    )
    lines.append(
        f"Export:       hades receipt export {receipt.receipt_id} "
        "--output receipt.json"
    )
    return _Outcome(EXIT_OK, payload, tuple(lines))


def _cmd_claims(services: _Services, args: argparse.Namespace) -> _Outcome:
    receipt = _require_receipt(services, args.receipt_id)
    payload = {
        "ok": True,
        "action": "claims",
        "receipt_id": receipt.receipt_id,
        "claim_edges": _claim_edges(receipt.claims),
    }
    lines = [
        f"Claims for {receipt.receipt_id} (claim → evidence → artifacts):"
    ]
    lines.extend(_render_claim_lines(receipt.claims))
    if not receipt.claims:
        lines.append("  (no claims recorded)")
    return _Outcome(EXIT_OK, payload, tuple(lines))


def _cmd_recheck(services: _Services, args: argparse.Namespace) -> _Outcome:
    receipt = _require_receipt(services, args.receipt_id)
    observation = services.issuer().recheck(receipt.receipt_id)
    payload = {
        "ok": True,
        "action": "recheck",
        "receipt_id": receipt.receipt_id,
        "status": observation.status,
        "observation": asdict(observation),
    }
    lines = [
        f"Recheck of {receipt.receipt_id} appended observation "
        f"{observation.observation_id}",
        f"Original: {_status_label(receipt.status)}",
        f"Current recheck: {_status_label(observation.status)} — observed "
        f"{observation.observed_at}",
    ]
    for note in observation.uncertainty:
        lines.append(f"  uncertainty: {note}")
    lines.append(
        "The original receipt is immutable; this recheck was appended as "
        "a linked observation."
    )
    return _Outcome(EXIT_OK, payload, tuple(lines))


def _resolve_output_path(raw: str) -> Path:
    """Bound relative ``--output`` paths to the active workspace."""
    path = Path(str(raw))
    if path.is_absolute():
        return path
    workspace = get_workspace_root()
    resolved = (workspace / path).resolve()
    if resolved != workspace and workspace not in resolved.parents:
        raise _usage(
            "relative --output paths must stay inside the active workspace; pass "
            "an absolute path to export elsewhere"
        )
    return resolved


def _cmd_export(services: _Services, args: argparse.Namespace) -> _Outcome:
    receipt = _require_receipt(services, args.receipt_id)
    output_path = _resolve_output_path(args.output)
    signing = services.signing_service()
    warning: str | None = None
    if getattr(args, "sign", False) and not signing.available:
        if signing.required:
            from agent.receipt_security import SigningUnavailableError

            raise SigningUnavailableError(
                "required signing provider is not available; refusing "
                "signed export"
            )
        warning = (
            "signing provider unavailable — the export is truthfully unsigned"
        )
    exported = services.exporter().export(
        receipt.receipt_id,
        output_path,
        redaction=getattr(args, "redaction", None),
        bundle_artifacts=bool(getattr(args, "bundle_artifacts", False)),
        sign=bool(getattr(args, "sign", False)),
    )
    redaction = getattr(args, "redaction", None) or services.config.get(
        "export_redaction", "public"
    )
    payload = {
        "ok": True,
        "action": "export",
        "receipt_id": receipt.receipt_id,
        "export_path": str(exported),
        "redaction": redaction,
    }
    lines = [
        f"Exported {receipt.receipt_id} to {exported} "
        f"({redaction} redaction)",
        "Hashes inside the export verify offline against sha256 canonical "
        "JSON.",
    ]
    if warning:
        payload["warning"] = warning
        lines.append(f"warning: {warning}")
    return _Outcome(EXIT_OK, payload, tuple(lines))


def _cmd_verify_signature(
    services: _Services, args: argparse.Namespace
) -> _Outcome:
    receipt = _require_receipt(services, args.receipt_id)
    observations = services.store.observations(receipt.receipt_id)
    attestations = _attestations_for(services, receipt, observations)
    signing = services.signing_service()
    records: list[dict] = []
    lines = [
        f"Signature verification for {receipt.receipt_id} "
        "(provenance only — a signature never proves truth):"
    ]
    for attestation in attestations:
        verification = signing.verify(attestation)
        records.append(
            {
                "attestation_id": attestation.attestation_id,
                "provider_id": attestation.provider_id,
                "target_id": attestation.target_id,
                "valid": bool(verification.valid),
                "state": verification.state,
                "detail": verification.detail,
            }
        )
        lines.append(
            f"  {attestation.attestation_id} provider="
            f"{attestation.provider_id} valid={verification.valid} "
            f"state={verification.state}"
        )
        lines.append(f"    {verification.detail}")
    if not attestations:
        lines.append("  (no attestations recorded)")
    payload = {
        "ok": True,
        "action": "verify-signature",
        "receipt_id": receipt.receipt_id,
        "attestations": records,
    }
    return _Outcome(EXIT_OK, payload, tuple(lines))


def _plan_payload(plan) -> dict:
    return asdict(plan)


def _cmd_retention_plan(
    services: _Services, args: argparse.Namespace
) -> _Outcome:
    at = getattr(args, "at", None)
    plan = services.retention_service().plan(at)
    payload = {
        "ok": True,
        "action": "retention-plan",
        "plan": _plan_payload(plan),
        "retention_plan_hash": plan.plan_hash,
    }
    lines = [
        f"Retention plan {plan.plan_id} (generated {plan.generated_at})",
        f"  plan hash: {plan.plan_hash}",
        f"  receipts to delete: {len(plan.receipt_ids)}",
        f"  observations to delete: {len(plan.observation_ids)}",
        f"  attestations to delete: {len(plan.attestation_ids)}",
        f"  artifact locators to delete: {len(plan.artifact_location_ids)}",
    ]
    if plan.blockers:
        lines.append("  blockers:")
        for hold in plan.blockers:
            lines.append(f"    {hold.receipt_id}: {hold.kind} ({hold.reason})")
    lines.append(
        "Prune exactly this plan: hades receipt prune --confirm-plan "
        f"{plan.plan_hash}"
    )
    return _Outcome(EXIT_OK, payload, tuple(lines))


def _cmd_prune(services: _Services, args: argparse.Namespace) -> _Outcome:
    from agent.receipt_hashing import hash_hex

    confirmed = str(args.confirm_plan)
    try:
        plan_id = f"rpl_{hash_hex(confirmed)}"
    except ValueError:
        raise _usage(
            "--confirm-plan must be the exact sha256: plan hash printed by "
            "`hades receipt retention-plan`"
        ) from None
    result = services.retention_service().prune(plan_id, confirmed)
    payload = {
        "ok": True,
        "action": "prune",
        "result": asdict(result),
    }
    lines = [
        f"Pruned retention plan {result.plan_id}",
        f"  receipts deleted: {result.deleted_receipts}",
        f"  observations deleted: {result.deleted_observations}",
        f"  attestations deleted: {result.deleted_attestations}",
        f"  artifact locators deleted: {result.deleted_artifact_locations}",
        f"  tombstones appended: {result.tombstones}",
    ]
    return _Outcome(EXIT_OK, payload, tuple(lines))


# ── Parser construction ─────────────────────────────────────────────────────


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit JSON output")


def build_parser(parent_subparsers) -> argparse.ArgumentParser:
    """Attach the full ``receipt`` grammar to *parent_subparsers*."""
    parser = parent_subparsers.add_parser(
        "receipt",
        aliases=["receipts"],
        help="Inspect verified outcome receipts (evidence, artifacts, rechecks)",
        description=(
            "Verified outcome & artifact receipts: immutable, independently "
            "scored records of what was asked, what evidence exists, and "
            "what remains uncertain. Rechecks append observations; they "
            "never rewrite a receipt."
        ),
    )
    sub = parser.add_subparsers(dest="receipt_command")

    p_list = sub.add_parser("list", help="List stored receipts")
    p_list.add_argument(
        "--status",
        choices=sorted(_STATUS_NOTES),
        help="Only receipts with this canonical status",
    )
    p_list.add_argument(
        "--subject",
        choices=_SUBJECT_KINDS,
        help="Only receipts about this subject kind",
    )
    p_list.add_argument("--limit", type=int, default=50, metavar="N")
    _add_json(p_list)
    p_list.set_defaults(_receipt_handler=_cmd_list)

    p_show = sub.add_parser(
        "show", help="Show one receipt with its recheck observations"
    )
    p_show.add_argument("receipt_id")
    p_show.add_argument(
        "--observation",
        default="latest",
        metavar="latest|all|OBS_ID",
        help="Which recheck observations to include (default: latest)",
    )
    _add_json(p_show)
    p_show.set_defaults(_receipt_handler=_cmd_show)

    p_claims = sub.add_parser(
        "claims", help="Every claim with its evidence and artifact edges"
    )
    p_claims.add_argument("receipt_id")
    _add_json(p_claims)
    p_claims.set_defaults(_receipt_handler=_cmd_claims)

    p_recheck = sub.add_parser(
        "recheck", help="Re-score current facts and append one observation"
    )
    p_recheck.add_argument("receipt_id")
    _add_json(p_recheck)
    p_recheck.set_defaults(_receipt_handler=_cmd_recheck)

    p_export = sub.add_parser(
        "export", help="Write a redacted, hash-verifiable export"
    )
    p_export.add_argument("receipt_id")
    p_export.add_argument("--output", required=True, metavar="PATH")
    p_export.add_argument("--redaction", choices=("public", "local"))
    p_export.add_argument("--bundle-artifacts", action="store_true")
    p_export.add_argument("--sign", action="store_true")
    p_export.set_defaults(_receipt_handler=_cmd_export)

    p_verify = sub.add_parser(
        "verify-signature",
        help="Verify recorded attestations (provenance only, never truth)",
    )
    p_verify.add_argument("receipt_id")
    _add_json(p_verify)
    p_verify.set_defaults(_receipt_handler=_cmd_verify_signature)

    p_plan = sub.add_parser(
        "retention-plan", help="Exact deletion candidates and blockers"
    )
    p_plan.add_argument("--at", metavar="RFC3339")
    _add_json(p_plan)
    p_plan.set_defaults(_receipt_handler=_cmd_retention_plan)

    p_prune = sub.add_parser(
        "prune", help="Delete exactly one confirmed retention plan"
    )
    p_prune.add_argument("--confirm-plan", required=True, metavar="PLAN_HASH")
    _add_json(p_prune)
    p_prune.set_defaults(_receipt_handler=_cmd_prune)

    parser.set_defaults(_receipt_parser=parser)
    return parser


# ── Execution and rendering ─────────────────────────────────────────────────


def _error_outcome(exc: Exception) -> _Outcome:
    """Map one failure to its contract exit code without leaking a
    traceback, secret, or raw locator."""
    from agent.receipt_ingest import EvidenceSourceError, ReceiptIngestError
    from agent.receipt_security import (
        ReceiptExportError,
        ReceiptSecurityError,
        RetentionError,
        SigningUnavailableError,
    )
    from agent.receipt_store import ReceiptStoreError

    message = _clip(str(exc), 500)
    if isinstance(exc, SigningUnavailableError):
        code, label = EXIT_UNAVAILABLE, "signing_unavailable"
    elif isinstance(
        exc,
        (
            ReceiptExportError,
            RetentionError,
            ReceiptIngestError,
            EvidenceSourceError,
            ValueError,
        ),
    ):
        code, label = EXIT_VALIDATION, "validation_error"
    elif isinstance(
        exc, (ReceiptStoreError, ReceiptSecurityError, sqlite3.Error, OSError)
    ):
        code, label = EXIT_STORAGE, "storage_failure"
    else:
        # Unexpected failure: report the type only, never the payload.
        code, label = EXIT_STORAGE, "internal_error"
        message = f"unexpected {type(exc).__name__}"
    return _Outcome(
        code,
        {"ok": False, "error": message, "code": label},
        (f"error: {message}",),
    )


def _execute(
    args: argparse.Namespace, *, output: str = "text"
) -> ReceiptCommandResult:
    handler = getattr(args, "_receipt_handler", None)
    if handler is None:
        parser = getattr(args, "_receipt_parser", None)
        help_text = parser.format_help() if parser is not None else _SLASH_HELP
        return ReceiptCommandResult(EXIT_OK, help_text.rstrip(), {"help": True})
    try:
        with _services() as services:
            outcome = handler(services, args)
    except _CliUsageError as exc:
        outcome = _Outcome(
            EXIT_VALIDATION,
            {"ok": False, "error": str(exc), "code": "usage_error"},
            (str(exc),),
        )
    except Exception as exc:  # noqa: BLE001 - mapped to contract exit codes
        outcome = _error_outcome(exc)
    as_json = output == "json" or bool(getattr(args, "json", False))
    if as_json:
        rendered = json.dumps(
            outcome.payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
    else:
        rendered = "\n".join(outcome.lines)
    return ReceiptCommandResult(outcome.exit_code, rendered, outcome.payload)


def receipt_command(args: argparse.Namespace) -> int:
    """Entry point for ``hades receipt ...`` argparse dispatch."""
    result = _execute(args)
    if result.output:
        print(result.output)
    return result.exit_code


def _validate_argv(argv: Sequence[str]) -> list[str]:
    items = [str(item) for item in argv]
    if len(items) > _MAX_ARGS:
        raise _usage(
            f"too many arguments ({len(items)}); receipt commands accept at "
            f"most {_MAX_ARGS}"
        )
    total = sum(len(item.encode("utf-8")) for item in items)
    if total > _MAX_TOTAL_ARG_BYTES:
        # Deliberately does not echo any argument content.
        raise _usage(
            "arguments exceed the 64 KiB total input bound for receipt "
            "commands"
        )
    return items


def run_argv(
    argv: Sequence[str], *, output: Literal["text", "json"] = "text"
) -> ReceiptCommandResult:
    """Parse and execute one receipt invocation from an argv list.

    The single shared surface behind the top-level command, the classic
    slash path, the native TUI RPC, and tests. Never raises for user
    input: bound violations and parse errors return exit code 2 and
    ``--help`` returns the help text at exit 0.
    """
    try:
        items = _validate_argv(argv)
    except _CliUsageError as exc:
        return ReceiptCommandResult(
            EXIT_VALIDATION,
            str(exc),
            {"ok": False, "error": str(exc), "code": "usage_error"},
        )
    wrap = _ReceiptArgumentParser(prog="hades", add_help=False)
    root_sub = wrap.add_subparsers(dest="_root")
    build_parser(root_sub)
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer), redirect_stderr(buffer):
            args = wrap.parse_args(["receipt", *items])
    except _CliUsageError as exc:
        return ReceiptCommandResult(
            EXIT_VALIDATION,
            (buffer.getvalue() + str(exc)).strip(),
            {"ok": False, "error": str(exc), "code": "usage_error"},
        )
    except SystemExit as exc:  # --help prints and exits 0
        code = exc.code if isinstance(exc.code, int) else EXIT_VALIDATION
        return ReceiptCommandResult(
            code, buffer.getvalue().rstrip(), {"help": True}
        )
    return _execute(args, output=output)


def run_slash(rest: str) -> str:
    """Execute a classic ``/receipt ...`` string and return its output.

    ``rest`` is everything after ``/receipt`` (or ``/receipts``); a bare
    or help invocation returns the short curated help block.
    """
    try:
        tokens = shlex.split(rest) if rest and rest.strip() else []
    except ValueError:
        return "error: unbalanced quotes in /receipt arguments"
    if not tokens or tokens[0] in {"help", "--help", "-h", "?"}:
        return _SLASH_HELP
    return run_argv(tokens).output
