"""Backward-compatibility shim — delegates to hades_cli.cli_commands_mixin."""
import importlib, sys  # noqa: E401
sys.modules[__name__] = importlib.import_module("hades_cli.cli_commands_mixin")
