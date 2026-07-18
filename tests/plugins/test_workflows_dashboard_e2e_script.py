from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "test-workflows-dashboard-e2e.sh"


def test_workflow_browser_script_exists():
    assert SCRIPT.is_file(), f"Missing {SCRIPT}"


def test_workflow_browser_script_covers_release_viewports():
    text = SCRIPT.read_text(encoding="utf-8")
    for viewport in ("1440 900", "1280 576", "1024 768", "768 1024", "390 844"):
        assert viewport in text
    for marker in (
        "Generate draft",
        "Accept draft",
        "Publish",
        "Start Run",
        "Open Continuous Feed",
        "Pause",
        "Resume",
        "Close",
        "Start new feed",
        "Cancel Execution",
    ):
        assert marker in text


def test_workflow_browser_script_has_required_infrastructure():
    text = SCRIPT.read_text(encoding="utf-8")
    # trap cleanup
    assert "trap" in text
    assert "hermes dashboard" in text or "hermes" in text
    # viewport iteration
    assert "set viewport" in text or "viewport" in text
    # screenshot per viewport
    assert "screenshot" in text
    # geometry assertion for short viewport
    assert "240" in text
    assert "getBoundingClientRect" in text
    # no console errors assertion
    assert "console" in text.lower()
