"""Backward-compatibility shim — delegates to hades_cli.managed_scope."""
import importlib, sys  # noqa: E401
sys.modules[__name__] = importlib.import_module("hades_cli.managed_scope")
