"""Backward-compatibility shim — delegates to hades_cli.model_normalize."""
import importlib, sys  # noqa: E401
sys.modules[__name__] = importlib.import_module("hades_cli.model_normalize")
