"""The fallback parsers in both installers must read Hades' own extras.

The installers intentionally derive their tier-two fallback from
``pyproject.toml`` instead of mirroring the optional dependency list.  Since
``[all]`` contains self-references, a stale upstream distribution regex quietly
produces an empty list and turns the fallback into a no-op.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _all_parser_block(script: Path) -> str:
    source = script.read_text(encoding="utf-8")
    start = source.index('optional-dependencies"]["all"]') if script.suffix == ".sh" else source.index("optional-dependencies']['all']")
    return source[start : start + 500]


def test_posix_installer_parses_hades_self_references_for_all_extra_fallback():
    parser = _all_parser_block(ROOT / "scripts" / "install.sh")
    assert 'hades-agent\\[([\\w-]+)\\]' in parser
    assert 'hermes-agent\\[([\\w-]+)\\]' not in parser


def test_windows_installer_parses_hades_self_references_and_names_hades_on_failure():
    source = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    parser = _all_parser_block(ROOT / "scripts" / "install.ps1")
    assert "hades-agent\\[([\\w-]+)\\]" in parser
    assert "hermes-agent\\[([\\w-]+)\\]" not in parser
    assert "Failed to install hades-agent package even with no extras." in source
