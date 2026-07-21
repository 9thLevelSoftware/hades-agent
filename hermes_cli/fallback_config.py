"""Backward-compatibility shim — delegates to hades_cli.fallback_config."""
import importlib, sys  # noqa: E401
sys.modules[__name__] = importlib.import_module("hades_cli.fallback_config")
