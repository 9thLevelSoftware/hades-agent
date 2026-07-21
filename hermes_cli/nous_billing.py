"""Backward-compatibility shim — delegates to hades_cli.nous_billing."""
import importlib, sys  # noqa: E401
sys.modules[__name__] = importlib.import_module("hades_cli.nous_billing")
