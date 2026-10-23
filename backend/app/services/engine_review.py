"""Run the context engine on a pull request and return plain-dict findings.

build_context makes blocking network calls, so callers must run this function
in a worker thread (asyncio.to_thread), never directly inside async code.
"""

from __future__ import annotations

from typing import Any


def _plain(value: Any) -> Any:
    """Enum -> its value; everything else unchanged."""
    return getattr(value, "value", value)


def run_engine_review(
    owner: str,
    name: str,
    pr_number: int,
    token: str,
    caller_index: Any = None,
) -> list[dict[str, Any]]:
    """Build the PR context, run the defect rules, return simple dicts."""
    # Imported here so importing the web app stays fast and tests can replace
    # these two functions without loading the engine's heavy dependencies.
    from pr_context_builder.builder.context_builder import build_context
    from pr_context_builder.localization.analyzer import analyze_code_defects

    context = build_context(
        owner, name, pr_number, github_token=token, caller_index=caller_index
    )
    findings = analyze_code_defects(context)
    return [
        {
            "finding_id": f.finding_id,
            "file_path": f.file_path,
            "start_line": f.start_line,
            "end_line": f.end_line,
            "summary": f.summary,
            "severity": _plain(f.severity),
            "confidence": _plain(f.confidence),
        }
        for f in findings
    ]