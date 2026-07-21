"""Backward-compatibility shim — delegates to hades_cli.runtime_provider."""
import importlib, sys  # noqa: E401
sys.modules[__name__] = importlib.import_module("hades_cli.runtime_provider")
