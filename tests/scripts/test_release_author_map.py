"""Contributor-attribution mappings for generated local commit identities."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def test_generated_local_hermes_identity_is_mapped():
    path = Path(__file__).resolve().parents[2] / "scripts" / "release.py"
    spec = importlib.util.spec_from_file_location("release_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.AUTHOR_MAP["hermes@example.invalid"] == "9thLevelSoftware"
    assert module.AUTHOR_MAP["dasblueyeddevil@gmail.com"] == "9thLevelSoftware"
