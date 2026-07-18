from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cli import HermesCLI


class _InsightsEngineStub:
    calls = []

    def __init__(self, db):
        self.db = db

    def generate(self, *, days=30, source=None, learning=False):
        self.calls.append({"days": days, "source": source, "learning": learning})
        return {"days": days, "source": source, "learning": learning}

    def format_terminal(self, report):
        return f"days={report['days']} source={report['source']}"

    def format_terminal_learning(self, report):
        return f"LEARNING days={report['days']} source={report['source']}"


def _run_show_insights(command: str):
    cli_obj = HermesCLI.__new__(HermesCLI)
    db = MagicMock()
    _InsightsEngineStub.calls = []
    with patch("hermes_state.SessionDB", return_value=db), \
         patch("agent.insights.InsightsEngine", _InsightsEngineStub):
        cli_obj._show_insights(command)
    return _InsightsEngineStub.calls, db


def test_cli_insights_accepts_positional_days(capsys):
    calls, db = _run_show_insights("/insights 7")

    assert calls == [{"days": 7, "source": None, "learning": False}]
    db.close.assert_called_once()
    assert "days=7 source=None" in capsys.readouterr().out


def test_cli_insights_keeps_days_flag_and_source(capsys):
    calls, db = _run_show_insights("/insights --days 14 --source discord")

    assert calls == [{"days": 14, "source": "discord", "learning": False}]
    db.close.assert_called_once()
    assert "days=14 source=discord" in capsys.readouterr().out


def test_cli_insights_learning_flag(capsys):
    """--learning flag routes to format_terminal_learning instead of format_terminal."""
    calls, db = _run_show_insights("/insights --learning")

    assert calls == [{"days": 30, "source": None, "learning": True}]
    out = capsys.readouterr().out
    assert "LEARNING" in out, "format_terminal_learning should be called when --learning is passed"
    assert "days=30" in out


def test_cli_insights_learning_with_days_and_source(capsys):
    """--learning works alongside --days and --source."""
    calls, db = _run_show_insights("/insights --days 7 --source cli --learning")

    assert calls == [{"days": 7, "source": "cli", "learning": True}]
    out = capsys.readouterr().out
    assert "LEARNING" in out
    assert "days=7" in out


def test_main_cmd_insights_passes_learning_to_report_generation(capsys):
    calls = []

    class _MainEngineStub:
        def __init__(self, db):
            self.db = db

        def generate(self, *, days=30, source=None, learning=False):
            calls.append({"days": days, "source": source, "learning": learning})
            return {"days": days, "source": source}

        def format_terminal_learning(self, report):
            return "LEARNING"

    db = MagicMock()
    with (
        patch("hermes_state.SessionDB", return_value=db),
        patch("agent.insights.InsightsEngine", _MainEngineStub),
    ):
        from hermes_cli.main import cmd_insights

        cmd_insights(SimpleNamespace(days=9, source="main", learning=True))

    assert calls == [{"days": 9, "source": "main", "learning": True}]
    assert "LEARNING" in capsys.readouterr().out
