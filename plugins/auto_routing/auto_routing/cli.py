"""Exact CLI surface for explicit auto-routing and activation workflows."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, is_dataclass, replace
from enum import Enum
from typing import Any, Callable

from .models import REASONING_EFFORT_ORDER
from .service import AutoRoutingService


class CommandWriteClass(str, Enum):
    READ_ONLY = "read_only"
    APPEND_ONLY_OBSERVATION = "append_only_observation"
    GUARDED_CONTROL_PLANE = "guarded_control_plane"


@dataclass(frozen=True)
class CommandMetadata:
    name: str
    help: str
    write_class: CommandWriteClass


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class _CommandSpec:
    metadata: CommandMetadata
    setup: Callable[[argparse.ArgumentParser], None]


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")


def _setup_apply(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--proposal", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-config-sha")
    _json_flag(parser)


def _inventory(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--include-ineligible", action="store_true")
    _json_flag(parser)


def _verify_runtime(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("runtime_stable_id")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-hash")
    parser.add_argument("--ack-billable", action="store_true")
    _json_flag(parser)


def _refresh_catalog(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--models-dev", action="store_true")
    parser.add_argument("--hermes", action="store_true")
    parser.add_argument("--file", action="append", default=[])
    _json_flag(parser)


def _plan(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--request", required=True)
    parser.add_argument("--prompt-file", action="append", default=[])
    _json_flag(parser)


def _validate(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--proposal")
    _json_flag(parser)


def _activate(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode",
        choices=("shadow", "active"),
        default="active",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-config-sha")
    _json_flag(parser)


def _explain(parser: argparse.ArgumentParser) -> None:
    lookup = parser.add_mutually_exclusive_group(required=True)
    lookup.add_argument("--decision-id")
    lookup.add_argument("--session-id")
    lookup.add_argument("--operation-id")
    parser.add_argument("--task-index", type=int)
    parser.add_argument("--detailed", action="store_true")
    _json_flag(parser)


_FEEDBACK_VALUES = (
    "rating-1",
    "rating-2",
    "rating-3",
    "rating-4",
    "rating-5",
    "rejected",
    "corrected",
    "manual-reroute",
)


def _feedback(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--evidence-id", required=True)
    parser.add_argument("--value", required=True, choices=_FEEDBACK_VALUES)
    _json_flag(parser)


def _report(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--decision-id")
    parser.add_argument("--profile-id")
    parser.add_argument("--runtime-id")
    parser.add_argument("--reasoning-effort", choices=REASONING_EFFORT_ORDER)
    _json_flag(parser)


def _read_only(parser: argparse.ArgumentParser) -> None:
    _json_flag(parser)


def _adapt_read_only(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile-id", required=True)
    _json_flag(parser)


def _adapt_mutation(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-hash")
    _json_flag(parser)


def _adapt_rollback(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-hash")
    _json_flag(parser)


def _manage_read_only(parser: argparse.ArgumentParser) -> None:
    _json_flag(parser)


def _manage_history(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile-id")
    _json_flag(parser)


def _manage_reconcile(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-hash")
    _json_flag(parser)


def _manage_mutation(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-hash")
    _json_flag(parser)


def _manage_recover(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--receipt-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-hash")
    _json_flag(parser)


def _manage_schedule(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--schedule", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-hash")
    _json_flag(parser)


def _manage_ranking_trust(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ranking-pack-path", required=True)
    parser.add_argument(
        "--trusted-ed25519-public-key",
        dest="trusted_public_keys",
        action="append",
        required=True,
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-hash")
    _json_flag(parser)


def _manage_daily_cap(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", dest="daily_limit", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-hash")
    _json_flag(parser)


_ADAPT_SPECS: tuple[_CommandSpec, ...] = (
    _CommandSpec(
        CommandMetadata(
            "adapt status",
            "Show one profile's adaptive control state",
            CommandWriteClass.READ_ONLY,
        ),
        _adapt_read_only,
    ),
    _CommandSpec(
        CommandMetadata(
            "adapt history",
            "Show one profile's immutable adaptive history",
            CommandWriteClass.READ_ONLY,
        ),
        _adapt_read_only,
    ),
    _CommandSpec(
        CommandMetadata(
            "adapt freeze",
            "Preview or apply a profile-local adaptation freeze",
            CommandWriteClass.GUARDED_CONTROL_PLANE,
        ),
        _adapt_mutation,
    ),
    _CommandSpec(
        CommandMetadata(
            "adapt unfreeze",
            "Preview or apply a profile-local adaptation unfreeze",
            CommandWriteClass.GUARDED_CONTROL_PLANE,
        ),
        _adapt_mutation,
    ),
    _CommandSpec(
        CommandMetadata(
            "adapt rollback",
            "Preview or apply an exact frozen profile revision rollback",
            CommandWriteClass.GUARDED_CONTROL_PLANE,
        ),
        _adapt_rollback,
    ),
)


def _adapt(parser: argparse.ArgumentParser) -> None:
    leaves = parser.add_subparsers(dest="auto_routing_adapt_action", required=True)
    for spec in _ADAPT_SPECS:
        metadata = spec.metadata
        leaf_name = metadata.name.removeprefix("adapt ")
        child = leaves.add_parser(
            leaf_name,
            help=f"{metadata.help} [{metadata.write_class.value}]",
            description=f"{metadata.help}\nwrite_class={metadata.write_class.value}",
        )
        child.error = lambda message, metadata=metadata: _raise_usage_error(
            metadata.name,
            metadata.write_class,
            message,
        )
        spec.setup(child)


_MANAGE_SPECS: tuple[_CommandSpec, ...] = (
    _CommandSpec(
        CommandMetadata(
            "manage inventory",
            "Show persisted eligible management inventory",
            CommandWriteClass.READ_ONLY,
        ),
        _manage_read_only,
    ),
    _CommandSpec(
        CommandMetadata(
            "manage ranking",
            "Show verified local ranking-pack status",
            CommandWriteClass.READ_ONLY,
        ),
        _manage_read_only,
    ),
    _CommandSpec(
        CommandMetadata(
            "manage status",
            "Show global autonomous-management status",
            CommandWriteClass.READ_ONLY,
        ),
        _manage_read_only,
    ),
    _CommandSpec(
        CommandMetadata(
            "manage history",
            "Show immutable management revision history",
            CommandWriteClass.READ_ONLY,
        ),
        _manage_history,
    ),
    _CommandSpec(
        CommandMetadata(
            "manage reconcile",
            "Run one local automatic reconciliation",
            CommandWriteClass.GUARDED_CONTROL_PLANE,
        ),
        _manage_reconcile,
    ),
    _CommandSpec(
        CommandMetadata(
            "manage enable",
            "Preview or enable global autonomous profile management",
            CommandWriteClass.GUARDED_CONTROL_PLANE,
        ),
        _manage_mutation,
    ),
    _CommandSpec(
        CommandMetadata(
            "manage disable",
            "Preview or disable global autonomous profile management",
            CommandWriteClass.GUARDED_CONTROL_PLANE,
        ),
        _manage_mutation,
    ),
    _CommandSpec(
        CommandMetadata(
            "manage freeze",
            "Preview or freeze management changes globally",
            CommandWriteClass.GUARDED_CONTROL_PLANE,
        ),
        _manage_mutation,
    ),
    _CommandSpec(
        CommandMetadata(
            "manage unfreeze",
            "Preview or unfreeze management changes globally",
            CommandWriteClass.GUARDED_CONTROL_PLANE,
        ),
        _manage_mutation,
    ),
    _CommandSpec(
        CommandMetadata(
            "manage recover",
            "Preview or apply exact receipt-bound config recovery",
            CommandWriteClass.GUARDED_CONTROL_PLANE,
        ),
        _manage_recover,
    ),
    _CommandSpec(
        CommandMetadata(
            "manage schedule",
            "Preview or update the local management schedule",
            CommandWriteClass.GUARDED_CONTROL_PLANE,
        ),
        _manage_schedule,
    ),
    _CommandSpec(
        CommandMetadata(
            "manage ranking-trust",
            "Preview or replace the complete local ranking trust set",
            CommandWriteClass.GUARDED_CONTROL_PLANE,
        ),
        _manage_ranking_trust,
    ),
    _CommandSpec(
        CommandMetadata(
            "manage daily-cap",
            "Preview or update the per-profile UTC daily change cap",
            CommandWriteClass.GUARDED_CONTROL_PLANE,
        ),
        _manage_daily_cap,
    ),
)


def _manage(parser: argparse.ArgumentParser) -> None:
    leaves = parser.add_subparsers(dest="auto_routing_manage_action", required=True)
    for spec in _MANAGE_SPECS:
        metadata = spec.metadata
        leaf_name = metadata.name.removeprefix("manage ")
        child = leaves.add_parser(
            leaf_name,
            help=f"{metadata.help} [{metadata.write_class.value}]",
            description=f"{metadata.help}\nwrite_class={metadata.write_class.value}",
        )
        child.error = lambda message, metadata=metadata: _raise_usage_error(
            metadata.name,
            metadata.write_class,
            message,
        )
        spec.setup(child)


_COMMAND_SPECS: tuple[_CommandSpec, ...] = (
    _CommandSpec(
        CommandMetadata(
            "setup",
            "Preview or apply initial shadow authority",
            CommandWriteClass.GUARDED_CONTROL_PLANE,
        ),
        _setup_apply,
    ),
    _CommandSpec(
        CommandMetadata(
            "edit",
            "Preview or apply edited shadow authority",
            CommandWriteClass.GUARDED_CONTROL_PLANE,
        ),
        _setup_apply,
    ),
    _CommandSpec(
        CommandMetadata(
            "inventory",
            "Inspect or explicitly refresh executable runtimes",
            CommandWriteClass.READ_ONLY,
        ),
        _inventory,
    ),
    _CommandSpec(
        CommandMetadata(
            "verify-runtime",
            "Preview or explicitly approve one bounded billable probe",
            CommandWriteClass.GUARDED_CONTROL_PLANE,
        ),
        _verify_runtime,
    ),
    _CommandSpec(
        CommandMetadata(
            "refresh-catalog",
            "Append an immutable catalog observation",
            CommandWriteClass.APPEND_ONLY_OBSERVATION,
        ),
        _refresh_catalog,
    ),
    _CommandSpec(
        CommandMetadata(
            "plan",
            "Compare profile rankings and build a read-only rules proposal",
            CommandWriteClass.READ_ONLY,
        ),
        _plan,
    ),
    _CommandSpec(
        CommandMetadata(
            "validate",
            "Validate current or proposed authority",
            CommandWriteClass.READ_ONLY,
        ),
        _validate,
    ),
    _CommandSpec(
        CommandMetadata(
            "activate",
            "Preview or apply a guarded shadow/active transition",
            CommandWriteClass.GUARDED_CONTROL_PLANE,
        ),
        _activate,
    ),
    _CommandSpec(
        CommandMetadata(
            "explain",
            "Explain one persisted routing decision without raw task content",
            CommandWriteClass.READ_ONLY,
        ),
        _explain,
    ),
    _CommandSpec(
        CommandMetadata(
            "feedback",
            "Append finite feedback to one routed turn evidence event",
            CommandWriteClass.APPEND_ONLY_OBSERVATION,
        ),
        _feedback,
    ),
    _CommandSpec(
        CommandMetadata(
            "report",
            "Summarize immutable route evidence without ranking targets",
            CommandWriteClass.READ_ONLY,
        ),
        _report,
    ),
    _CommandSpec(
        CommandMetadata(
            "status",
            "Show effective auto-routing state",
            CommandWriteClass.READ_ONLY,
        ),
        _read_only,
    ),
    _CommandSpec(
        CommandMetadata(
            "doctor",
            "Check auto-routing activation health",
            CommandWriteClass.READ_ONLY,
        ),
        _read_only,
    ),
    _CommandSpec(
        CommandMetadata(
            "adapt",
            "Inspect or control conservative profile adaptation",
            CommandWriteClass.READ_ONLY,
        ),
        _adapt,
    ),
    _CommandSpec(
        CommandMetadata(
            "manage",
            "Inspect or control autonomous profile management",
            CommandWriteClass.READ_ONLY,
        ),
        _manage,
    ),
)

_ALL_COMMAND_SPECS = (*_COMMAND_SPECS, *_ADAPT_SPECS, *_MANAGE_SPECS)
_SPEC_BY_NAME = {spec.metadata.name: spec for spec in _ALL_COMMAND_SPECS}
if len(_SPEC_BY_NAME) != len(_ALL_COMMAND_SPECS) or any(
    not isinstance(spec.metadata.write_class, CommandWriteClass)
    for spec in _ALL_COMMAND_SPECS
):
    raise RuntimeError("every unique auto-routing command needs a closed write class")


def command_metadata(name: str, *, refresh: bool = False) -> CommandMetadata:
    try:
        metadata = _SPEC_BY_NAME[name].metadata
    except KeyError as error:
        raise ValueError(f"unknown auto-routing command: {name}") from error
    if name == "inventory" and refresh:
        return replace(
            metadata,
            write_class=CommandWriteClass.APPEND_ONLY_OBSERVATION,
        )
    return metadata


def _raise_usage_error(
    command: str,
    write_class: CommandWriteClass,
    message: str,
) -> None:
    print(
        json.dumps(
            {
                "ok": False,
                "error": message,
                "command": command,
                "write_class": write_class.value,
            },
            sort_keys=True,
        )
    )
    raise SystemExit(2)


def build_parser(parser: argparse.ArgumentParser) -> None:
    """Register the complete, closed auto-routing command surface."""
    parser.set_defaults(auto_routing_action="status", auto_routing_json=False)
    parser.error = lambda message: _raise_usage_error(
        "auto-routing",
        CommandWriteClass.READ_ONLY,
        message,
    )
    subcommands = parser.add_subparsers(dest="auto_routing_action")
    for spec in _COMMAND_SPECS:
        metadata = spec.metadata
        child = subcommands.add_parser(
            metadata.name,
            help=f"{metadata.help} [{metadata.write_class.value}]",
            description=f"{metadata.help}\nwrite_class={metadata.write_class.value}",
        )
        child.error = lambda message, metadata=metadata: _raise_usage_error(
            metadata.name,
            metadata.write_class,
            message,
        )
        spec.setup(child)


def _error(
    command: str,
    message: str,
    *,
    refresh: bool = False,
    error_code: str | None = None,
) -> CommandResult:
    metadata = command_metadata(command, refresh=refresh)
    return CommandResult(
        exit_code=2,
        payload={
            "ok": False,
            "error": message,
            "command": command,
            "write_class": metadata.write_class.value,
            **({"error_code": error_code} if error_code else {}),
        },
    )


def _success(command: str, payload: dict[str, Any], *, refresh: bool = False) -> CommandResult:
    metadata = command_metadata(command, refresh=refresh)
    return CommandResult(
        exit_code=0,
        payload={
            **payload,
            "command": command,
            "write_class": metadata.write_class.value,
        },
    )


def _content_free_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, warnings=False)
    raise TypeError("auto-routing command returned an unsupported payload")


def execute(args: argparse.Namespace, *, service: AutoRoutingService) -> CommandResult:
    """Execute parsed arguments without printing, for testable CLI behavior."""
    command = str(getattr(args, "auto_routing_action", None) or "status")
    if command == "adapt":
        command = f"adapt {getattr(args, 'auto_routing_adapt_action', '')}".rstrip()
    if command == "manage":
        command = f"manage {getattr(args, 'auto_routing_manage_action', '')}".rstrip()
    inventory_refresh = command == "inventory" and bool(
        getattr(args, "refresh", False)
    )

    def error_result(
        message: str,
        *,
        error_code: str | None = None,
    ) -> CommandResult:
        return _error(
            command,
            message,
            refresh=inventory_refresh,
            error_code=error_code,
        )

    try:
        if command in {"setup", "edit"}:
            apply = bool(args.apply)
            expected = args.expected_config_sha
            if apply != bool(expected):
                return error_result(
                    "--apply and --expected-config-sha must be supplied together",
                )
            if apply:
                return _success(
                    command,
                    service.apply_config(
                        args.proposal,
                        expected_config_sha256=expected,
                    ),
                )
            return _success(command, service.preview_config(args.proposal))
        if command == "validate":
            return _success(command, service.validate(args.proposal))
        if command == "activate":
            apply = bool(args.apply)
            expected = args.expected_config_sha
            if apply != bool(expected):
                return error_result(
                    "--apply and --expected-config-sha must be supplied together",
                )
            if apply:
                return _success(
                    command,
                    service.apply_activation(
                        args.mode,
                        expected_config_sha256=expected,
                    ),
                )
            return _success(command, service.preview_activation(args.mode))
        if command == "status":
            return _success(command, service.status())
        if command == "manage inventory":
            return _success(command, service.management_inventory())
        if command == "manage ranking":
            return _success(command, service.management_ranking_status())
        if command == "manage status":
            return _success(command, service.management_status())
        if command == "manage history":
            return _success(
                command,
                service.management_history(
                    profile_id=getattr(args, "profile_id", None),
                ),
            )
        if command == "manage reconcile" and bool(args.scheduled):
            if args.apply or args.expect_hash:
                return error_result(
                    "--scheduled does not accept manual approval flags",
                    error_code="scheduled_approval_forbidden",
                )
            invocation = service.assert_scheduled_management_invocation()
            report = service.reconcile_management(
                scheduled=True,
                scheduled_invocation=invocation,
            )
            service.complete_scheduled_management_invocation(invocation)
            return _success(
                command,
                _content_free_payload(report),
            )
        if command == "manage recover":
            apply = bool(args.apply)
            expected = args.expect_hash
            if apply and not expected:
                return error_result(
                    "--apply requires --expect-hash",
                    error_code="expected_hash_required",
                )
            if expected and not apply:
                return error_result(
                    "--expect-hash requires --apply",
                    error_code="apply_required",
                )
            if apply:
                return _success(
                    command,
                    service.apply_management_recovery(
                        args.receipt_id,
                        expected_hash=expected,
                    ),
                )
            return _success(
                command,
                service.preview_management_recovery(args.receipt_id),
            )
        if command in {
            "manage reconcile",
            "manage enable",
            "manage disable",
            "manage freeze",
            "manage unfreeze",
            "manage schedule",
            "manage ranking-trust",
            "manage daily-cap",
        }:
            apply = bool(args.apply)
            expected = args.expect_hash
            if apply and not expected:
                return error_result(
                    "--apply requires --expect-hash",
                    error_code="expected_hash_required",
                )
            if expected and not apply:
                return error_result(
                    "--expect-hash requires --apply",
                    error_code="apply_required",
                )
            action = command.removeprefix("manage ")
            schedule = getattr(args, "schedule", None)
            ranking_pack_path = getattr(args, "ranking_pack_path", None)
            trusted_public_keys = getattr(args, "trusted_public_keys", None)
            if trusted_public_keys is not None:
                trusted_public_keys = tuple(trusted_public_keys)
            daily_limit = getattr(args, "daily_limit", None)
            if apply:
                return _success(
                    command,
                    _content_free_payload(
                        service.apply_management_control(
                            action=action,
                            expected_hash=expected,
                            schedule=schedule,
                            ranking_pack_path=ranking_pack_path,
                            trusted_public_keys=trusted_public_keys,
                            daily_limit=daily_limit,
                        )
                    ),
                )
            return _success(
                command,
                service.preview_management_control(
                    action=action,
                    schedule=schedule,
                    ranking_pack_path=ranking_pack_path,
                    trusted_public_keys=trusted_public_keys,
                    daily_limit=daily_limit,
                ),
            )
        if command == "adapt status":
            return _success(command, service.adaptation_status(args.profile_id))
        if command == "adapt history":
            return _success(command, service.adaptation_history(args.profile_id))
        if command in {"adapt freeze", "adapt unfreeze", "adapt rollback"}:
            action = command.removeprefix("adapt ")
            apply = bool(args.apply)
            expected = args.expect_hash
            if apply != bool(expected):
                return error_result(
                    "--apply and --expect-hash must be supplied together",
                )
            revision_id = getattr(args, "revision", None)
            if apply:
                return _success(
                    command,
                    service.apply_adaptation_control(
                        action=action,
                        profile_id=args.profile_id,
                        revision_id=revision_id,
                        expected_hash=expected,
                    ),
                )
            return _success(
                command,
                service.preview_adaptation_control(
                    action=action,
                    profile_id=args.profile_id,
                    revision_id=revision_id,
                ),
            )
        if command == "explain":
            return _success(
                command,
                service.explain(
                    decision_id=args.decision_id,
                    session_id=args.session_id,
                    operation_id=args.operation_id,
                    task_index=args.task_index,
                    detailed=args.detailed,
                ),
            )
        if command == "feedback":
            return _success(
                command,
                service.record_feedback(
                    evidence_id=args.evidence_id,
                    value=args.value,
                ),
            )
        if command == "report":
            return _success(
                command,
                service.report(
                    days=args.days,
                    decision_id=args.decision_id,
                    profile_id=args.profile_id,
                    runtime_id=args.runtime_id,
                    reasoning_effort=args.reasoning_effort,
                ),
            )
        if command == "inventory":
            return _success(
                command,
                service.inventory(
                    refresh=args.refresh,
                    include_ineligible=args.include_ineligible,
                ),
                refresh=args.refresh,
            )
        if command == "refresh-catalog":
            return _success(
                command,
                service.refresh_catalog(
                    models_dev=args.models_dev,
                    hermes=args.hermes,
                    files=args.file,
                ),
            )
        if command == "verify-runtime":
            if args.apply and (not args.expect_hash or not args.ack_billable):
                return error_result(
                    "apply requires --expect-hash and --ack-billable",
                )
            if not args.apply and (args.expect_hash or args.ack_billable):
                return error_result(
                    "verification approval flags require --apply",
                )
            return _success(
                command,
                service.verify_runtime(
                    args.runtime_stable_id,
                    apply=args.apply,
                    precondition_hash=args.expect_hash,
                    acknowledge_billable=args.ack_billable,
                ),
            )
        if command == "plan":
            payload = service.plan(args.request, prompt_files=args.prompt_file)
            result = _success(
                command,
                payload,
            )
            return result if payload.get("ready") else replace(result, exit_code=2)
        if command == "doctor":
            payload = service.doctor()
            result = _success(command, payload)
            return result if payload.get("healthy") else replace(result, exit_code=2)
        return error_result("unsupported auto-routing command")
    except Exception as error:
        return _error(
            command,
            str(error) or type(error).__name__,
            refresh=inventory_refresh,
        )


def auto_routing_command(
    args: argparse.Namespace,
    *,
    service: AutoRoutingService,
) -> int:
    """Registered plugin handler with real shell exit-code propagation."""
    result = execute(args, service=service)
    print(json.dumps(result.payload, indent=2, sort_keys=True))
    if result.exit_code:
        raise SystemExit(result.exit_code)
    return 0


__all__ = [
    "CommandMetadata",
    "CommandResult",
    "CommandWriteClass",
    "auto_routing_command",
    "build_parser",
    "command_metadata",
    "execute",
]
