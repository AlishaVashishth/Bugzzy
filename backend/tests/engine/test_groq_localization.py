"""Offline unit tests for Groq-powered hybrid bug localization integration.

Tests verify prompt generation, structured JSON response parsing, grounding validation,
error handling, and strict separation between deterministic findings and AI suggestions
without calling real APIs or requiring an active API key.
"""

import json
from unittest.mock import MagicMock

from pr_context_builder.config import Settings
from pr_context_builder.localization.agent import localize_bugs
from pr_context_builder.localization.groq_client import GroqLocalizationClient
from pr_context_builder.localization.models import BugLocalizationResult
from pr_context_builder.models.context_package import ContextPackage
from pr_context_builder.models.pr_metadata import (
    ChangedFile,
    DiffHunk,
    FileChangeStatus,
    PRState,
    PullRequest,
)
from pr_context_builder.models.repository import CodeSnippet, EvidenceOrigin


def _make_sample_context() -> ContextPackage:
    pr = PullRequest(
        owner="example-org",
        repo="sample-repo",
        number=77,
        title="Refactor invoice calculations",
        body="Updates calculate_tax signature and call sites.",
        state=PRState.OPEN,
        base_sha="base7777",
        base_branch="main",
        head_sha="head8888",
        head_branch="feature/invoice",
    )
    cf = ChangedFile(
        filename="src/billing/invoice.py",
        status=FileChangeStatus.MODIFIED,
        hunks=[
            DiffHunk(
                old_start=10,
                old_lines=2,
                new_start=10,
                new_lines=2,
                lines=[
                    "-def calculate_tax(subtotal):",
                    "+def calculate_tax(subtotal, rate):",
                ],
            )
        ],
    )
    def_snippet = CodeSnippet(
        file_path="src/billing/invoice.py",
        start_line=10,
        end_line=15,
        content="def calculate_tax(subtotal, rate):\n    return subtotal * rate\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.ENCLOSING_SCOPE,
    )
    caller_snippet = CodeSnippet(
        file_path="src/billing/checkout.py",
        start_line=20,
        end_line=25,
        content=(
            "from src.billing.invoice import calculate_tax\n"
            "def finalize():\n"
            "    return calculate_tax(100)\n"
        ),
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.DIRECT_DEPENDENCY,
    )
    return ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[cf],
        evidence_snippets=[def_snippet, caller_snippet],
    )


def _make_mock_groq_completion(content: str) -> MagicMock:
    """Helper creating a mock response mimicking groq.chat.completions.create."""
    mock_choice = MagicMock()
    mock_choice.message.content = content
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    return mock_response


def test_groq_disabled_preserves_deterministic_findings() -> None:
    """When Groq is disabled, deterministic findings are returned with zero AI suggestions."""
    context = _make_sample_context()
    settings = Settings(github_token=None, groq_enabled=False)

    result = localize_bugs(context=context, settings=settings)

    assert result.has_findings is True
    assert len(result.findings) == 1
    assert result.findings[0].file_path == "src/billing/checkout.py"
    assert result.has_suggestions is False
    assert result.ai_suggestions == []
    assert result.dl_model_applied is None


def test_missing_groq_api_key_produces_limitation() -> None:
    """When Groq is enabled but API key is missing, records a limitation and does not crash."""
    context = _make_sample_context()
    settings = Settings(github_token=None, groq_enabled=True, groq_api_key=None)

    result = localize_bugs(context=context, settings=settings)

    assert result.has_findings is True
    assert len(result.findings) == 1
    assert result.has_suggestions is False
    assert any("[model_unavailable]" in lim for lim in result.analysis_limitations)
    assert any("GROQ_API_KEY is not configured" in lim for lim in result.analysis_limitations)


def test_valid_structured_ai_output_parsed_and_grounded() -> None:
    """Valid AI suggestions are grounded against context and populated into ai_suggestions."""
    context = _make_sample_context()
    mock_json = json.dumps(
        {
            "suggestions": [
                {
                    "file_path": "src/billing/invoice.py",
                    "start_line": 10,
                    "end_line": 15,
                    "summary": "Check default tax rate handling",
                    "detailed_explanation": (
                        "Tax calculation signature changed without fallback default."
                    ),
                    "potential_impact": "TypeError if caller does not pass rate",
                    "confidence_explanation": "Based on diff hunk line 10.",
                    "related_test_references": ["tests/test_billing.py"],
                    "suggested_verification": ["Verify test_invoice runs"],
                }
            ]
        }
    )

    mock_client_backend = MagicMock()
    mock_client_backend.chat.completions.create.return_value = _make_mock_groq_completion(mock_json)

    groq_client = GroqLocalizationClient(
        api_key="gsk-mock-key",
        model="llama-3.3-70b-versatile",
        client=mock_client_backend,
    )

    result = localize_bugs(context=context, groq_client=groq_client)

    # 1. Deterministic findings remain intact
    assert result.has_findings is True
    assert len(result.findings) == 1

    # 2. AI suggestion is populated separately
    assert result.has_suggestions is True
    assert len(result.ai_suggestions) == 1
    sugg = result.ai_suggestions[0]
    assert sugg.suggestion_id == "AI-001"
    assert sugg.file_path == "src/billing/invoice.py"
    assert sugg.start_line == 10
    assert sugg.end_line == 15
    assert sugg.commit_sha == context.head_sha
    assert sugg.is_in_changed_code is True
    assert sugg.model_name == "llama-3.3-70b-versatile"
    assert len(sugg.supporting_evidence) >= 1
    assert result.dl_model_applied == "llama-3.3-70b-versatile"


def test_ai_suggestions_cannot_become_confirmed_findings() -> None:
    """AI suggestions are strictly placed in ai_suggestions and never in findings."""
    # Context with NO deterministic defects
    pr = PullRequest(
        owner="example-org",
        repo="sample-repo",
        number=1,
        title="Clean change",
        state=PRState.OPEN,
        base_sha="base1",
        base_branch="main",
        head_sha="head1",
        head_branch="feature",
    )
    snip = CodeSnippet(
        file_path="src/util.py",
        start_line=1,
        end_line=5,
        content="def helper(): return 1\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.ENCLOSING_SCOPE,
    )
    cf = ChangedFile(
        filename="src/util.py",
        status=FileChangeStatus.MODIFIED,
        hunks=[DiffHunk(old_start=1, old_lines=1, new_start=1, new_lines=1, lines=["+x=1"])],
    )
    context = ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[cf],
        evidence_snippets=[snip],
    )

    mock_json = json.dumps(
        {
            "suggestions": [
                {
                    "file_path": "src/util.py",
                    "start_line": 1,
                    "end_line": 2,
                    "summary": "Model believes this might fail",
                    "detailed_explanation": "Pure speculation by model",
                }
            ]
        }
    )

    mock_client_backend = MagicMock()
    mock_client_backend.chat.completions.create.return_value = _make_mock_groq_completion(mock_json)
    groq_client = GroqLocalizationClient(client=mock_client_backend)

    result = localize_bugs(context=context, groq_client=groq_client)

    # Deterministic findings MUST remain 0!
    assert result.has_findings is False
    assert result.findings == []

    # AI suggestion is recorded in ai_suggestions only
    assert result.has_suggestions is True
    assert len(result.ai_suggestions) == 1
    assert result.ai_suggestions[0].summary == "Model believes this might fail"


def test_invented_paths_and_out_of_range_lines_rejected() -> None:
    """Suggestions with hallucinated paths or line numbers are rejected."""
    context = _make_sample_context()
    mock_json = json.dumps(
        {
            "suggestions": [
                {
                    # Hallucinated path not in context
                    "file_path": "src/hallucinated/file.py",
                    "start_line": 10,
                    "end_line": 20,
                    "summary": "Hallucinated file",
                },
                {
                    # Valid path, but completely ungrounded line range outside any snippet or hunk
                    "file_path": "src/billing/invoice.py",
                    "start_line": 9999,
                    "end_line": 10005,
                    "summary": "Out of bounds line",
                },
                {
                    # Invalid inverted line range
                    "file_path": "src/billing/invoice.py",
                    "start_line": 15,
                    "end_line": 5,
                    "summary": "Inverted lines",
                },
            ]
        }
    )

    mock_client_backend = MagicMock()
    mock_client_backend.chat.completions.create.return_value = _make_mock_groq_completion(mock_json)
    groq_client = GroqLocalizationClient(client=mock_client_backend)

    result = localize_bugs(context=context, groq_client=groq_client)

    # All 3 hallucinated/invalid suggestions must be rejected
    assert result.has_suggestions is False
    assert result.ai_suggestions == []


def test_invalid_json_handled_safely() -> None:
    """Malformed non-JSON model response is handled safely as an analysis limitation."""
    context = _make_sample_context()

    mock_client_backend = MagicMock()
    mock_client_backend.chat.completions.create.return_value = _make_mock_groq_completion(
        "Here is what I think: {not valid json"
    )
    groq_client = GroqLocalizationClient(client=mock_client_backend)

    result = localize_bugs(context=context, groq_client=groq_client)

    # Deterministic findings preserved
    assert len(result.findings) == 1
    assert result.ai_suggestions == []
    assert any("[inference_error]" in lim for lim in result.analysis_limitations)


def test_provider_api_error_does_not_crash_or_erase_findings() -> None:
    """API exceptions (e.g. rate limit, connection timeout) record a limitation without crashing."""
    context = _make_sample_context()

    mock_client_backend = MagicMock()
    mock_client_backend.chat.completions.create.side_effect = RuntimeError(
        "Rate limit exceeded (HTTP 429)"
    )
    groq_client = GroqLocalizationClient(client=mock_client_backend)

    result = localize_bugs(context=context, groq_client=groq_client)

    assert result.has_findings is True
    assert len(result.findings) == 1
    assert result.ai_suggestions == []
    assert any("Rate limit exceeded" in lim for lim in result.analysis_limitations)


def test_serialization_round_trip_with_ai_suggestions() -> None:
    """BugLocalizationResult with AI suggestions round-trips through JSON serialization."""
    context = _make_sample_context()
    mock_json = json.dumps(
        {
            "suggestions": [
                {
                    "file_path": "src/billing/invoice.py",
                    "start_line": 10,
                    "end_line": 15,
                    "summary": "Check rate parameter default",
                }
            ]
        }
    )
    mock_client_backend = MagicMock()
    mock_client_backend.chat.completions.create.return_value = _make_mock_groq_completion(mock_json)
    groq_client = GroqLocalizationClient(client=mock_client_backend)

    result = localize_bugs(context=context, groq_client=groq_client)
    assert len(result.ai_suggestions) == 1

    # Serialize
    result_json = result.model_dump_json()

    # Deserialize
    restored = BugLocalizationResult.model_validate_json(result_json)
    assert restored.pr_number == result.pr_number
    assert len(restored.findings) == len(result.findings)
    assert len(restored.ai_suggestions) == 1
    assert restored.ai_suggestions[0].suggestion_id == "AI-001"
    assert restored.ai_suggestions[0].file_path == "src/billing/invoice.py"
    assert restored.ai_suggestions[0].is_in_changed_code is True


def _make_billing_pr1_context() -> ContextPackage:
    """Reconstructs the exact context for Amvi3007/Testing PR #1."""
    pr = PullRequest(
        owner="Amvi3007",
        repo="Testing",
        number=1,
        title="Update billing calculation",
        body="Updates final_price calculation.",
        state=PRState.OPEN,
        base_sha="base-sha-pr1",
        base_branch="main",
        head_sha="head-sha-pr1",
        head_branch="feature/billing",
    )
    # Head version of billing.py:
    # Line 2: def calculate_tax(subtotal, rate):
    # Line 3:     return subtotal * rate
    # Line 4:
    # Line 5:
    # Line 6: def final_price(subtotal, rate):
    # Line 7:     tax = calculate_tax(subtotal)
    # Line 8:     return subtotal + tax
    hunk = DiffHunk(
        old_start=4,
        old_lines=5,
        new_start=4,
        new_lines=5,
        lines=[
            " ",
            " ",
            " def final_price(subtotal, rate):",
            "-    tax = calculate_tax(subtotal, rate)",
            "+    tax = calculate_tax(subtotal)",
            "     return subtotal + tax",
        ],
    )
    cf = ChangedFile(
        filename="billing.py",
        status=FileChangeStatus.MODIFIED,
        additions=1,
        deletions=1,
        changes=2,
        hunks=[hunk],
    )
    callee_snip = CodeSnippet(
        file_path="billing.py",
        start_line=2,
        end_line=3,
        content="def calculate_tax(subtotal, rate):\n    return subtotal * rate\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.CALL_HIERARCHY,
    )
    caller_snip = CodeSnippet(
        file_path="billing.py",
        start_line=6,
        end_line=8,
        content=(
            "def final_price(subtotal, rate):\n"
            "    tax = calculate_tax(subtotal)\n"
            "    return subtotal + tax\n"
        ),
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.ENCLOSING_SCOPE,
    )
    return ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[cf],
        evidence_snippets=[callee_snip, caller_snip],
    )


def test_regression_billing_pr1_identifies_line7_and_rejects_line6() -> None:
    """Regression test for Amvi3007/Testing PR #1:

    When AI model suggests line 6 (enclosing function definition), the grounding validator
    refines the location to the defective statement on line 7 and rejects line 6.
    """
    context = _make_billing_pr1_context()

    # Model reports line 6 (function declaration) instead of line 7
    mock_json = json.dumps(
        {
            "suggestions": [
                {
                    "file_path": "billing.py",
                    "start_line": 6,
                    "end_line": 6,
                    "summary": "In billing.py, calculate_tax(subtotal) call is missing required parameter 'rate'",
                    "detailed_explanation": (
                        "calculate_tax requires 2 arguments (subtotal, rate), but final_price "
                        "calls calculate_tax(subtotal) with only 1 argument."
                    ),
                    "potential_impact": "TypeError at runtime when final_price is invoked",
                    "confidence_explanation": "Defective call in final_price.",
                }
            ]
        }
    )

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_groq_completion(mock_json)
    groq_client = GroqLocalizationClient(client=mock_client)

    result = localize_bugs(context=context, groq_client=groq_client)

    # 1. Deterministic finding identifies line 7
    assert result.has_findings is True
    assert len(result.findings) == 1
    assert result.findings[0].start_line == 7
    assert result.findings[0].end_line == 7
    assert result.findings[0].is_in_changed_code is True

    # 2. AI suggestion refines to line 7 and does NOT accept line 6
    assert result.has_suggestions is True
    assert len(result.ai_suggestions) == 1
    sugg = result.ai_suggestions[0]
    assert sugg.file_path == "billing.py"
    assert sugg.start_line == 7  # Line 7 is identified!
    assert sugg.start_line != 6  # Line 6 is NOT accepted as the defective line!
    assert sugg.end_line == 7
    assert sugg.is_in_changed_code is True

    # 3. Evidence excerpt actually supports the defective statement
    assert len(sugg.supporting_evidence) >= 1
    assert sugg.supporting_evidence[0].content_excerpt == "tax = calculate_tax(subtotal)"
    assert sugg.supporting_evidence[0].content_excerpt != "def final_price(subtotal, rate):"


def test_regression_billing_pr1_broad_range_refined_to_line7() -> None:
    """When AI model reports broad function range 6-8, validator refines location to line 7."""
    context = _make_billing_pr1_context()

    mock_json = json.dumps(
        {
            "suggestions": [
                {
                    "file_path": "billing.py",
                    "start_line": 6,
                    "end_line": 8,
                    "summary": "Missing required argument 'rate' in calculate_tax call",
                    "detailed_explanation": "calculate_tax(subtotal) is missing rate argument.",
                }
            ]
        }
    )

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_groq_completion(mock_json)
    groq_client = GroqLocalizationClient(client=mock_client)

    result = localize_bugs(context=context, groq_client=groq_client)

    assert len(result.ai_suggestions) == 1
    sugg = result.ai_suggestions[0]
    assert sugg.start_line == 7
    assert sugg.end_line == 7
    assert sugg.supporting_evidence[0].content_excerpt == "tax = calculate_tax(subtotal)"


def test_regression_unverified_function_declaration_rejected() -> None:
    """If AI reports a function declaration that cannot be verified to contain a defect, reject it."""
    context = _make_billing_pr1_context()

    # Unchanged declaration on line 6 with generic non-matching description
    mock_json = json.dumps(
        {
            "suggestions": [
                {
                    "file_path": "billing.py",
                    "start_line": 6,
                    "end_line": 6,
                    "summary": "Function declaration style issue",
                    "detailed_explanation": "Naming convention could be better.",
                }
            ]
        }
    )

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_groq_completion(mock_json)
    groq_client = GroqLocalizationClient(client=mock_client)

    result = localize_bugs(context=context, groq_client=groq_client)

    # Line 6 is an unchanged declaration with no verifiable defect; must be rejected!
    assert result.has_suggestions is False
    assert result.ai_suggestions == []


def test_regression_billing_pr1_exact_line7_accepted() -> None:
    """When AI model directly reports line 7, it is verified and accepted."""
    context = _make_billing_pr1_context()

    mock_json = json.dumps(
        {
            "suggestions": [
                {
                    "file_path": "billing.py",
                    "start_line": 7,
                    "end_line": 7,
                    "summary": "Missing argument 'rate' in calculate_tax call",
                    "detailed_explanation": "calculate_tax requires 2 arguments.",
                }
            ]
        }
    )

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_groq_completion(mock_json)
    groq_client = GroqLocalizationClient(client=mock_client)

    result = localize_bugs(context=context, groq_client=groq_client)

    assert len(result.ai_suggestions) == 1
    sugg = result.ai_suggestions[0]
    assert sugg.start_line == 7
    assert sugg.end_line == 7
    assert sugg.is_in_changed_code is True
    assert sugg.supporting_evidence[0].content_excerpt == "tax = calculate_tax(subtotal)"

