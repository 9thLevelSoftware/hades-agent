"""Opt-in auto-routing plugin registration shell."""

from pathlib import Path

from .auto_routing.cli import auto_routing_command, build_parser
from .auto_routing.runtime_resolver import AutoRoutingRuntimeResolver
from .auto_routing.service import AutoRoutingService


def register(ctx) -> None:
    resolver = AutoRoutingRuntimeResolver(ctx)
    ctx.register_agent_runtime_resolver(resolver)
    ctx.register_hook("pre_api_request", resolver.on_pre_api_request)
    ctx.register_hook("post_turn_outcome", resolver.on_post_turn_outcome)
    ctx.register_cli_command(
        name="auto-routing",
        help="Configure and inspect automatic model routing",
        setup_fn=build_parser,
        handler_fn=lambda args: auto_routing_command(
            args,
            service=resolver.service_for_current_profile(),
        ),
        description="Executable inventory, profile advice, validation, and routing history",
    )
    ctx.register_skill(
        "auto-routing",
        Path(__file__).parent / "skills" / "auto-routing" / "SKILL.md",
        description="Create or edit Auto Routing profiles through a validated CLI proposal",
    )


__all__ = [
    "AutoRoutingRuntimeResolver",
    "AutoRoutingService",
    "auto_routing_command",
    "build_parser",
    "register",
]
