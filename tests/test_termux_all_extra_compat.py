"""Regression coverage for the Termux broad install profile."""

import re
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def test_pyproject_defines_termux_all_without_known_blockers() -> None:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    requirements = data["project"]["optional-dependencies"]["termux-all"]
    self_extras = {
        match.group(1)
        for requirement in requirements
        if (match := re.fullmatch(r"hades-agent\[([^\]]+)\]", requirement))
    }

    assert "termux" in self_extras
    assert self_extras.isdisjoint({"matrix", "voice"})
    assert not any(
        requirement.startswith("hermes-agent[") for requirement in requirements
    )


def test_install_script_prefers_termux_all_then_fallbacks() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "pip install -e '.[termux-all]' -c constraints-termux.txt" in text
    assert "Termux broad profile (.[termux-all]) failed, trying baseline Termux profile..." in text
    assert "Termux baseline profile (.[termux]) failed, trying base install..." in text
