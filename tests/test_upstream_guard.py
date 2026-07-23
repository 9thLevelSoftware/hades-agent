"""Tests for hades_cli.upstream_guard — upstream hermes-agent co-install
detection, one-shot warning, suppression knob, and a repo scan asserting no
user-facing ``pip install hermes-agent`` remediation strings remain."""

import importlib.metadata
import io
import re
from pathlib import Path

import pytest

from hades_cli import upstream_guard


_UPSTREAM_INSTALL_HINT = re.compile(
    r"\b(?:"
    r"(?:uv\s+)?pip\s+install(?:\s+--[^\s]+)*|"
    r"uv\s+tool\s+install(?:\s+--[^\s]+)*|"
    r"pipx\s+install(?:\s+--[^\s]+)*|"
    r"uvx\s+--from|"
    r"install(?:\s+the)?(?:\s+extra)?\s*:?"
    r")\s+[`'\"]?hermes-agent(?:\[[^\]]+\])?",
    re.IGNORECASE,
)
_NEGATIVE_INSTALL_CONTEXT = re.compile(
    r"\b(?:do\s+not|don't|never|avoid|unsupported|not\s+supported|installs\s+via)\b|请勿使用",
    re.IGNORECASE,
)


def _has_positive_upstream_install_hint(line: str) -> bool:
    return bool(_UPSTREAM_INSTALL_HINT.search(line)) and not _NEGATIVE_INSTALL_CONTEXT.search(line)


def _active_user_facing_files(repo_root: Path):
    source_roots = (
        "tools", "hades_cli", "agent", "gateway", "cron", "plugins",
        "providers", "acp_adapter", "acp_registry", "tui_gateway", "scripts",
    )
    for root_name in source_roots:
        root = repo_root / root_name
        if root.is_dir():
            yield from root.rglob("*.py")
    for root in (
        repo_root / "skills",
        repo_root / "optional-skills",
        repo_root / "website" / "docs",
        repo_root / "website" / "i18n",
    ):
        if root.is_dir():
            for suffix in ("*.py", "*.md", "*.mdx"):
                yield from root.rglob(suffix)


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


def test_no_positive_upstream_install_hints_remain_in_active_user_surfaces():
    """No user-facing remediation may direct users to upstream Hermes.

    The wording is intentionally broader than only ``pip install``: startup
    errors commonly say ``Install hermes-agent[...]`` without repeating the
    package manager.  An uninstall command and compatibility prose are not
    installation hints and therefore remain valid legacy references.
    """
    repo_root = Path(__file__).resolve().parent.parent
    offenders = []
    for source_file in sorted(set(_active_user_facing_files(repo_root))):
        if "tests" in source_file.parts or "test" in source_file.name:
            continue
        try:
            text = source_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _has_positive_upstream_install_hint(line):
                offenders.append(f"{source_file.relative_to(repo_root)}:{lineno}: {line.strip()}")
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
    assert _has_positive_upstream_install_hint(line) is is_hint


@pytest.mark.parametrize(
    ("line", "is_hint"),
    [
        ("uv tool install hermes-agent", True),
        ("uvx --from 'hermes-agent[acp]' hermes-acp", True),
        ("Install the extra: `hermes-agent[vertex]`.", True),
        ("Do not use `pip install hermes-agent`.", False),
        ("请勿使用 `pip install hermes-agent`。", False),
        ("installs via PyPI (e.g. `uv tool install hermes-agent`)", False),
    ],
)
def test_upstream_install_hint_detection_preserves_negative_guidance(line, is_hint):
    assert _has_positive_upstream_install_hint(line) is is_hint
