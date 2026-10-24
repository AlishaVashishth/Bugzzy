"""Tests for the engine wrapper. The real engine is replaced by fakes."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.engine_review import run_engine_review


class _Level:
    def __init__(self, value):
        self.value = value


def _finding(**overrides):
    base = dict(
        finding_id="BUG-001",
        file_path="billing.py",
        start_line=7,
        end_line=7,
        summary="Incompatible call",
        severity=_Level("high"),
        confidence=_Level("strong"),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_findings_are_converted_to_plain_dicts(monkeypatch):
    monkeypatch.setattr(
        "pr_context_builder.builder.context_builder.build_context",
        MagicMock(return_value="ctx"),
    )
    monkeypatch.setattr(
        "pr_context_builder.localization.analyzer.analyze_code_defects",
        MagicMock(return_value=[_finding()]),
    )
    assert run_engine_review("o", "n", 1, "tok") == [
        {
            "finding_id": "BUG-001",
            "file_path": "billing.py",
            "start_line": 7,
            "end_line": 7,
            "summary": "Incompatible call",
            "severity": "high",
            "confidence": "strong",
        }
    ]


def test_arguments_reach_the_engine(monkeypatch):
    fake_build = MagicMock(return_value="ctx")
    fake_analyze = MagicMock(return_value=[])
    monkeypatch.setattr("pr_context_builder.builder.context_builder.build_context", fake_build)
    monkeypatch.setattr("pr_context_builder.localization.analyzer.analyze_code_defects", fake_analyze)
    run_engine_review("owner", "repo", 5, "tok")
    fake_build.assert_called_once_with("owner", "repo", 5, github_token="tok", caller_index=None)
    fake_analyze.assert_called_once_with("ctx")


def test_no_findings_gives_empty_list(monkeypatch):
    monkeypatch.setattr(
        "pr_context_builder.builder.context_builder.build_context", MagicMock(return_value="ctx")
    )
    monkeypatch.setattr(
        "pr_context_builder.localization.analyzer.analyze_code_defects", MagicMock(return_value=[])
    )
    assert run_engine_review("o", "n", 1, "tok") == []