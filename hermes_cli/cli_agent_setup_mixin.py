"""Backward-compatibility shim — delegates to hades_cli.cli_agent_setup_mixin."""
import importlib, sys  # noqa: E401
sys.modules[__name__] = importlib.import_module("hades_cli.cli_agent_setup_mixin")
