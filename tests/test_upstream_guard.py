"""Tests for hades_cli.upstream_guard — upstream hermes-agent co-install
detection, one-shot warning, suppression knob, and a repo scan asserting no
user-facing ``pip install hermes-agent`` remediation strings remain."""

import importlib.metadata
import io
import re
from pathlib import Path

import pytest

from hades_cli import upstream_guard


@pytest.fixture(autouse=True)
def _reset_guard_state():
    """Each test starts with a cold detection cache and unwarned process."""
    upstream_guard.detect_upstream_hermes_dist.cache_clear()
    upstream_guard._warned = False
    yield
    upstream_guard.detect_upstream_hermes_dist.cache_clear()
    upstream_guard._warned = False


class _FakeDist:
    version = "0.19.0"


def _patch_upstream_installed(monkeypatch):
    def fake_distribution(name):
        if name == "hermes-agent":
            return _FakeDist()
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "distribution", fake_distribution)


def test_detect_returns_none_when_upstream_absent():
    # The test venv installs this fork (dist name "hades-agent"), never the
    # upstream "hermes-agent" distribution.
    assert upstream_guard.detect_upstream_hermes_dist() is None


def test_detect_not_fooled_by_own_dist():
    # Our own distribution must not trigger detection: only an exact
    # "hermes-agent" metadata hit counts, and ours is named "hades-agent".
    try:
        own = importlib.metadata.distribution("hades-agent")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("hades-agent not installed in this environment")
    assert own is not None
    assert upstream_guard.detect_upstream_hermes_dist() is None


def test_warn_prints_once_per_process(monkeypatch):
    _patch_upstream_installed(monkeypatch)

    stream = io.StringIO()
    assert upstream_guard.warn_if_upstream_present(stream=stream) is True
    output = stream.getvalue()
    assert output.count("\n") == 1, "warning must be a single line"
    assert "hermes-agent 0.19.0" in output
    assert "pip uninstall hermes-agent" in output
    assert "pip install --force-reinstall hades-agent" in output

    # Second call in the same process: no repeat.
    stream2 = io.StringIO()
    assert upstream_guard.warn_if_upstream_present(stream=stream2) is False
    assert stream2.getvalue() == ""


@pytest.mark.parametrize(
    "var", ["HADES_SUPPRESS_UPSTREAM_WARNING", "HERMES_SUPPRESS_UPSTREAM_WARNING"]
)
def test_suppression_env_var_silences_warning(monkeypatch, var):
    # HERMES_ spelling proves the dual-read env helper is honored.
    _patch_upstream_installed(monkeypatch)
    monkeypatch.setenv(var, "1")

    stream = io.StringIO()
    assert upstream_guard.warn_if_upstream_present(stream=stream) is False
    assert stream.getvalue() == ""


def test_no_upstream_install_hints_remain():
    """No user-facing remediation may direct users to upstream Hermes.

    The wording is intentionally broader than only ``pip install``: startup
    errors commonly say ``Install hermes-agent[...]`` without repeating the
    package manager.  An uninstall command and compatibility prose are not
    installation hints and therefore remain valid legacy references.
    """
    repo_root = Path(__file__).resolve().parent.parent
    pattern = re.compile(
        r"\b(?:pip\s+)?install(?:\s+--[^\s]+)*\s+['\"]?hermes-agent(?:\[|\b)",
        re.IGNORECASE,
    )
    offenders = []
    for top in ("tools", "hades_cli", "agent", "gateway", "cron",
                 "plugins", "providers", "acp_adapter", "acp_registry",
                 "tui_gateway", "scripts"):
        top_dir = repo_root / top
        if not top_dir.is_dir():
            continue
        for py_file in sorted(top_dir.rglob("*.py")):
            if "tests" in py_file.parts or "test" in py_file.name:
                continue
            try:
                text = py_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    offenders.append(f"{py_file.relative_to(repo_root)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Found stale upstream install remediation strings:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    ("line", "is_hint"),
    [
        ("Install hermes-agent[google_chat].", True),
        ("pip install 'hermes-agent[messaging]'", True),
        ("pip install --force-reinstall hermes-agent", True),
        ("pip uninstall hermes-agent", False),
        ("Legacy hermes-agent[all] compatibility remains supported.", False),
    ],
)
def test_upstream_install_hint_pattern_ignores_compatibility_and_uninstall(line, is_hint):
    pattern = re.compile(
        r"\b(?:pip\s+)?install(?:\s+--[^\s]+)*\s+['\"]?hermes-agent(?:\[|\b)",
        re.IGNORECASE,
    )
    assert bool(pattern.search(line)) is is_hint
