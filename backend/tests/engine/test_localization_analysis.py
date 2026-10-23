"""Automated unit tests for evidence-grounded bug localization analysis.

Verifies detection of signature conflicts, deleted symbol references, clean changes,
missing context handling, and strict evidence grounding without external dependencies.
"""

from pr_context_builder.localization.agent import localize_bugs
from pr_context_builder.localization.analyzer import (
    _is_line_in_diff,
    validate_finding_grounding,
)
from pr_context_builder.localization.models import (
    BugSeverity,
    ConfidenceLevel,
    EvidenceReference,
    SuspectedBugLocation,
)
from pr_context_builder.models.context_package import (
    ContextPackage,
    LimitationType,
    RetrievalLimitation,
)
from pr_context_builder.models.pr_metadata import (
    ChangedFile,
    DiffHunk,
    FileChangeStatus,
    PRState,
    PullRequest,
)
from pr_context_builder.models.repository import CodeSnippet, EvidenceOrigin


def _make_dummy_pr(head_sha: str = "head1111", base_sha: str = "base0000") -> PullRequest:
    return PullRequest(
        owner="example-org",
        repo="sample-repo",
        number=42,
        title="Update payment processor",
        body="Refactors process_payment signature",
        state=PRState.OPEN,
        base_sha=base_sha,
        base_branch="main",
        head_sha=head_sha,
        head_branch="feature/payment",
    )


def test_seeded_defect_signature_missing_required_argument() -> None:
    """Ensure a changed function adding a required argument conflicts with a visible caller."""
    pr = _make_dummy_pr()

    # Changed file: payment.py adds required 'currency' argument without default
    changed_file = ChangedFile(
        filename="src/payment.py",
        status=FileChangeStatus.MODIFIED,
        additions=2,
        deletions=1,
        changes=3,
        patch=(
            "@@ -5,2 +5,3 @@\n"
            "-def process_payment(amount):\n"
            "+def process_payment(amount, currency):"
        ),
        hunks=[
            DiffHunk(
                old_start=5,
                old_lines=2,
                new_start=5,
                new_lines=3,
                lines=[
                    "-def process_payment(amount):",
                    "+def process_payment(amount, currency):",
                    "+    pass",
                ],
            )
        ],
    )

    # Snippet 1: Definition of process_payment in payment.py at head_sha
    def_snippet = CodeSnippet(
        file_path="src/payment.py",
        start_line=5,
        end_line=7,
        content="def process_payment(amount, currency):\n    return f'{currency}: {amount}'\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.ENCLOSING_SCOPE,
    )

    # Snippet 2: Visible caller in checkout.py calling with 1 argument (missing currency)
    caller_snippet = CodeSnippet(
        file_path="src/checkout.py",
        start_line=20,
        end_line=25,
        content="def checkout(cart):\n    total = 100\n    return process_payment(total)\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.DIRECT_DEPENDENCY,
    )

    pkg = ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[changed_file],
        evidence_snippets=[def_snippet, caller_snippet],
    )

    result = localize_bugs(pkg)

    assert result.has_findings is True
    assert len(result.findings) == 1

    finding = result.findings[0]
    assert finding.file_path == "src/checkout.py"
    assert finding.start_line == 22
    assert finding.commit_sha == pr.head_sha
    assert finding.severity == BugSeverity.HIGH
    assert finding.confidence == ConfidenceLevel.STRONG
    assert "missing required argument" in finding.summary
    assert "currency" in finding.detailed_explanation

    # Verify supporting evidence grounding
    assert len(finding.supporting_evidence) == 2
    assert finding.supporting_evidence[0].file_path == "src/checkout.py"
    assert finding.supporting_evidence[1].file_path == "src/payment.py"


def test_clean_change_with_backward_compatible_default_argument() -> None:
    """Ensure adding an optional parameter with a default does NOT produce a false positive."""
    pr = _make_dummy_pr()

    changed_file = ChangedFile(
        filename="src/payment.py",
        status=FileChangeStatus.MODIFIED,
        patch="diff patch",
        hunks=[],
    )

    # Function adds 'currency="USD"' with a default value
    def_snippet = CodeSnippet(
        file_path="src/payment.py",
        start_line=5,
        end_line=7,
        content="def process_payment(amount, currency='USD'):\n    return amount\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.ENCLOSING_SCOPE,
    )

    # Caller passes only amount (relying on default currency)
    caller_snippet = CodeSnippet(
        file_path="src/checkout.py",
        start_line=20,
        end_line=25,
        content="def checkout(cart):\n    return process_payment(100)\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.DIRECT_DEPENDENCY,
    )

    pkg = ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[changed_file],
        evidence_snippets=[def_snippet, caller_snippet],
    )

    result = localize_bugs(pkg)
    assert result.has_findings is False
    assert result.findings == []


def test_signature_change_with_no_visible_call_site() -> None:
    """Ensure a changed signature with NO visible caller does NOT fabricate a finding."""
    pr = _make_dummy_pr()

    changed_file = ChangedFile(
        filename="src/payment.py",
        status=FileChangeStatus.MODIFIED,
        patch="diff",
    )

    def_snippet = CodeSnippet(
        file_path="src/payment.py",
        start_line=5,
        end_line=7,
        content="def process_payment(amount, new_required_arg):\n    return amount\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.ENCLOSING_SCOPE,
    )

    # No caller snippet is present in context
    pkg = ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[changed_file],
        evidence_snippets=[def_snippet],
    )

    result = localize_bugs(pkg)
    assert result.has_findings is False
    assert result.findings == []


def test_deleted_symbol_still_referenced_in_active_code() -> None:
    """Ensure deleting a function that is still imported by active code raises a finding."""
    pr = _make_dummy_pr()

    # Changed file: legacy.py deletes 'old_helper'
    hunk = DiffHunk(
        old_start=10,
        old_lines=3,
        new_start=0,
        new_lines=0,
        lines=["-def old_helper(x):", "-    return x * 2", "-"],
    )
    changed_file = ChangedFile(
        filename="src/legacy.py",
        status=FileChangeStatus.MODIFIED,
        patch="diff",
        hunks=[hunk],
    )

    # Active snippet in caller.py imports deleted old_helper
    caller_snippet = CodeSnippet(
        file_path="src/caller.py",
        start_line=1,
        end_line=5,
        content="from src.legacy import old_helper\n\ndef run():\n    return old_helper(10)\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.DIRECT_DEPENDENCY,
    )

    pkg = ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[changed_file],
        evidence_snippets=[caller_snippet],
    )

    result = localize_bugs(pkg)
    assert result.has_findings is True
    assert len(result.findings) == 1

    finding = result.findings[0]
    assert finding.file_path == "src/caller.py"
    assert "Reference to deleted symbol 'old_helper'" in finding.summary
    assert finding.commit_sha == pr.head_sha

    # Verify provenance of supporting evidence: deletion at base_sha, import at head_sha
    shas = {ref.commit_sha for ref in finding.supporting_evidence}
    assert pr.head_sha in shas
    assert pr.base_sha in shas


def test_missing_or_empty_snippets() -> None:
    """Ensure empty snippet lists return cleanly without exceptions or fabricated findings."""
    pr = _make_dummy_pr()
    pkg = ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[],
        evidence_snippets=[],
        test_snippets=[],
    )

    result = localize_bugs(pkg)
    assert result.has_findings is False
    assert result.findings == []
    assert result.analyzed_snippets_count == 0


def test_unsupported_non_python_content() -> None:
    """Ensure non-Python files are safely skipped without generating spurious errors."""
    pr = _make_dummy_pr()
    cf = ChangedFile(filename="docs/README.md", status=FileChangeStatus.MODIFIED)
    snip = CodeSnippet(
        file_path="docs/README.md",
        start_line=1,
        end_line=10,
        content="# Documentation\nRun `process_payment(100)` in terminal.\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.REPOSITORY_REFERENCE,
        language="markdown",
    )

    pkg = ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[cf],
        evidence_snippets=[snip],
    )

    result = localize_bugs(pkg)
    assert result.has_findings is False
    assert result.findings == []


def test_line_ranges_and_commit_sha_provenance() -> None:
    """Ensure line ranges and commit SHAs are mathematically consistent and grounded."""
    pr = _make_dummy_pr()
    cf = ChangedFile(
        filename="src/api.py",
        status=FileChangeStatus.MODIFIED,
        hunks=[
            DiffHunk(
                old_start=1,
                old_lines=1,
                new_start=1,
                new_lines=2,
                lines=["+def f(a): pass"],
            )
        ],
    )
    def_snip = CodeSnippet(
        file_path="src/api.py",
        start_line=10,
        end_line=12,
        content="def calculate(a, b):\n    return a + b\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.ENCLOSING_SCOPE,
    )
    caller_snip = CodeSnippet(
        file_path="tests/test_api.py",
        start_line=100,
        end_line=105,
        content="def test_calc():\n    calculate(1)\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.TEST_FILE,
    )

    pkg = ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[cf],
        evidence_snippets=[def_snip],
        test_snippets=[caller_snip],
    )

    result = localize_bugs(pkg)
    assert result.has_findings is True
    finding = result.findings[0]

    assert finding.start_line == 101
    assert finding.end_line == 101
    assert finding.commit_sha == pr.head_sha
    assert finding.supporting_evidence[0].start_line >= 100
    assert finding.supporting_evidence[1].start_line >= 10


def test_rejection_of_ungrounded_fabricated_evidence() -> None:
    """Ensure validate_finding_grounding rejects ungrounded or fabricated references."""
    pr = _make_dummy_pr()
    pkg = ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[ChangedFile(filename="src/real.py", status=FileChangeStatus.MODIFIED)],
        evidence_snippets=[
            CodeSnippet(
                file_path="src/real.py",
                start_line=1,
                end_line=5,
                content="x = 1",
                commit_sha=pr.head_sha,
                origin=EvidenceOrigin.DIRECT_DIFF,
            )
        ],
    )

    # 1. Finding points to non-existent file path
    fake_finding = SuspectedBugLocation(
        finding_id="BUG-FAKE",
        file_path="src/hallucinated.py",
        start_line=10,
        end_line=12,
        commit_sha=pr.head_sha,
        is_in_changed_code=False,
        summary="Hallucinated finding",
        detailed_explanation="Not grounded",
        potential_impact="None",
        severity=BugSeverity.LOW,
        severity_justification="Test",
        confidence=ConfidenceLevel.SPECULATIVE,
        confidence_explanation="Test",
        supporting_evidence=[
            EvidenceReference(
                file_path="src/hallucinated.py",
                start_line=10,
                end_line=12,
                commit_sha=pr.head_sha,
                origin=EvidenceOrigin.DIRECT_DIFF,
                content_excerpt="fake code",
            )
        ],
    )
    assert validate_finding_grounding(fake_finding, pkg) is False

    # 2. Finding has invalid commit SHA
    fake_sha_finding = SuspectedBugLocation(
        finding_id="BUG-FAKE2",
        file_path="src/real.py",
        start_line=1,
        end_line=2,
        commit_sha="invalid_sha_not_in_context",
        is_in_changed_code=True,
        summary="Bad SHA",
        detailed_explanation="Bad SHA",
        potential_impact="None",
        severity=BugSeverity.LOW,
        severity_justification="Test",
        confidence=ConfidenceLevel.SPECULATIVE,
        confidence_explanation="Test",
        supporting_evidence=[],
    )
    assert validate_finding_grounding(fake_sha_finding, pkg) is False


def test_propagation_of_retrieval_limitations_alongside_findings() -> None:
    """Ensure analysis_limitations are fully preserved when findings are also generated."""
    pr = _make_dummy_pr()
    limitation = RetrievalLimitation(
        limitation_type=LimitationType.BUDGET_EXCEEDED,
        target_path="src/large_file.py",
        message="Skipped due to token budget.",
    )
    cf = ChangedFile(
        filename="src/service.py",
        status=FileChangeStatus.MODIFIED,
    )
    def_snip = CodeSnippet(
        file_path="src/service.py",
        start_line=1,
        end_line=5,
        content="def run(mode, verbose):\n    pass\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.ENCLOSING_SCOPE,
    )
    caller_snip = CodeSnippet(
        file_path="src/main.py",
        start_line=10,
        end_line=15,
        content="def start():\n    run('fast')\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.DIRECT_DEPENDENCY,
    )

    pkg = ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[cf],
        evidence_snippets=[def_snip, caller_snip],
        limitations=[limitation],
    )

    result = localize_bugs(pkg)
    assert result.has_findings is True
    assert len(result.findings) == 1
    assert len(result.analysis_limitations) == 1
    assert result.analysis_limitations[0] == (
        "[budget_exceeded] (src/large_file.py) Skipped due to token budget."
    )


def test_graceful_handling_of_malformed_syntax_snippets() -> None:
    """Ensure malformed or unparseable Python snippets do not crash the analyzer."""
    pr = _make_dummy_pr()
    cf = ChangedFile(filename="src/broken.py", status=FileChangeStatus.MODIFIED)
    broken_snip = CodeSnippet(
        file_path="src/broken.py",
        start_line=1,
        end_line=5,
        content="def broken(:\n  unclosed syntax",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.DIRECT_DIFF,
    )

    pkg = ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[cf],
        evidence_snippets=[broken_snip],
    )

    # Must complete cleanly without raising SyntaxError
    result = localize_bugs(pkg)
    assert result.has_findings is False
    assert result.findings == []


def test_edited_signature_is_not_deletion() -> None:
    """Changing calculate(a) to calculate(a, b=1) must not create a deleted-symbol finding."""
    pr = _make_dummy_pr()

    # Diff modifies definition line: -def calculate(a): +def calculate(a, b=1):
    hunk = DiffHunk(
        old_start=10,
        old_lines=2,
        new_start=10,
        new_lines=2,
        lines=[
            "-def calculate(a):",
            "+def calculate(a, b=1):",
            "     return a + b",
        ],
    )
    changed_file = ChangedFile(
        filename="src/calc.py",
        status=FileChangeStatus.MODIFIED,
        hunks=[hunk],
    )

    def_snippet = CodeSnippet(
        file_path="src/calc.py",
        start_line=10,
        end_line=13,
        content="def calculate(a, b=1):\n    return a + b\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.ENCLOSING_SCOPE,
    )

    caller_snippet = CodeSnippet(
        file_path="src/caller.py",
        start_line=1,
        end_line=6,
        content="from src.calc import calculate\n\ndef run():\n    return calculate(10)\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.DIRECT_DEPENDENCY,
    )

    pkg = ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[changed_file],
        evidence_snippets=[def_snippet, caller_snippet],
    )

    result = localize_bugs(pkg)
    # Must NOT report calculate as a deleted symbol or report any incompatible call
    assert result.has_findings is False
    assert result.findings == []


def test_unrelated_methods_same_name_different_class() -> None:
    """Modifying DatabaseService.connect must not flag BluetoothDevice.connect as incompatible."""
    pr = _make_dummy_pr()

    # PR modifies DatabaseService in src/db.py
    changed_file = ChangedFile(
        filename="src/db.py",
        status=FileChangeStatus.MODIFIED,
        hunks=[
            DiffHunk(
                old_start=5,
                old_lines=2,
                new_start=5,
                new_lines=2,
                lines=[
                    "-    def connect(self):",
                    "+    def connect(self, host, port):",
                ],
            )
        ],
    )

    db_snippet = CodeSnippet(
        file_path="src/db.py",
        start_line=1,
        end_line=10,
        content=(
            "class DatabaseService:\n    def connect(self, host, port):\n        self.host = host\n"
        ),
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.ENCLOSING_SCOPE,
    )

    # Unrelated caller in src/devices.py invoking BluetoothDevice.connect() with 0 extra args
    device_snippet = CodeSnippet(
        file_path="src/devices.py",
        start_line=1,
        end_line=15,
        content=(
            "class BluetoothDevice:\n"
            "    def connect(self):\n"
            "        pass\n\n"
            "def pair():\n"
            "    dev = BluetoothDevice()\n"
            "    dev.connect()\n"
        ),
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.REPOSITORY_REFERENCE,
    )

    pkg = ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[changed_file],
        evidence_snippets=[db_snippet, device_snippet],
    )

    result = localize_bugs(pkg)
    # dev.connect() is for BluetoothDevice, not DatabaseService; no finding
    assert result.has_findings is False
    assert result.findings == []


def test_unrelated_modules_same_function_name() -> None:
    """Modules with same function name must not be conflated when imports resolve elsewhere."""
    pr = _make_dummy_pr()

    # PR changes process in src/billing.py to require 2 arguments (tx_id, amount)
    cf_billing = ChangedFile(
        filename="src/billing.py",
        status=FileChangeStatus.MODIFIED,
        hunks=[
            DiffHunk(
                old_start=1,
                old_lines=2,
                new_start=1,
                new_lines=2,
                lines=[
                    "-def process(tx_id):",
                    "+def process(tx_id, amount):",
                ],
            )
        ],
    )

    billing_snippet = CodeSnippet(
        file_path="src/billing.py",
        start_line=1,
        end_line=5,
        content="def process(tx_id, amount):\n    return f'{tx_id}: {amount}'\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.ENCLOSING_SCOPE,
    )

    # src/queue.py is another module that defines process(task) with 1 argument
    queue_snippet = CodeSnippet(
        file_path="src/queue.py",
        start_line=1,
        end_line=5,
        content="def process(task):\n    return task\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.REPOSITORY_REFERENCE,
    )

    # Caller 1: worker.py imports process from src.queue and calls it with 1 argument
    worker_snippet = CodeSnippet(
        file_path="src/worker.py",
        start_line=1,
        end_line=8,
        content="from src.queue import process\n\ndef run_worker(job):\n    return process(job)\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.REPOSITORY_REFERENCE,
    )

    # Caller 2: payment_client.py imports from src.billing with 1 arg (missing amount)
    payment_client_snippet = CodeSnippet(
        file_path="src/payment_client.py",
        start_line=1,
        end_line=8,
        content=(
            "from src.billing import process\n\ndef send_payment(tx):\n    return process(tx)\n"
        ),
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.DIRECT_DEPENDENCY,
    )

    pkg = ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[cf_billing],
        evidence_snippets=[
            billing_snippet,
            queue_snippet,
            worker_snippet,
            payment_client_snippet,
        ],
    )

    result = localize_bugs(pkg)
    # Only payment_client.py must be flagged; worker.py must NOT be conflated
    assert result.has_findings is True
    assert len(result.findings) == 1

    finding = result.findings[0]
    assert finding.file_path == "src/payment_client.py"
    assert "amount" in finding.detailed_explanation
    assert finding.supporting_evidence[1].file_path == "src/billing.py"


def test_changed_line_accuracy_not_entire_file() -> None:
    """Caller location at line 300 must not be is_in_changed_code when hunk is lines 10-20."""
    pr = _make_dummy_pr()

    # Diff hunk strictly covers lines 10-20 of src/service.py
    hunk = DiffHunk(
        old_start=10,
        old_lines=10,
        new_start=10,
        new_lines=11,
        lines=["+def process_data(value, mode): pass"],
    )
    changed_file = ChangedFile(
        filename="src/service.py",
        status=FileChangeStatus.MODIFIED,
        hunks=[hunk],
    )

    # Changed function definition at line 10
    def_snippet = CodeSnippet(
        file_path="src/service.py",
        start_line=10,
        end_line=15,
        content="def process_data(value, mode):\n    return value\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.ENCLOSING_SCOPE,
    )

    # Caller A: Inside the changed file, but far away at line 300 (outside diff hunk!)
    caller_outside_hunk = CodeSnippet(
        file_path="src/service.py",
        start_line=298,
        end_line=305,
        content="def old_routine():\n    return process_data(42)\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.CALL_HIERARCHY,
    )

    # Caller B: Inside the changed file, directly inside the hunk at line 14
    caller_inside_hunk = CodeSnippet(
        file_path="src/service.py",
        start_line=12,
        end_line=16,
        content="def new_routine():\n    return process_data(99)\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.DIRECT_DIFF,
    )

    pkg = ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[changed_file],
        evidence_snippets=[def_snippet, caller_outside_hunk, caller_inside_hunk],
    )

    result = localize_bugs(pkg)
    assert len(result.findings) == 2

    finding_outside = next(f for f in result.findings if f.start_line >= 298)
    assert finding_outside.is_in_changed_code is False

    finding_inside = next(f for f in result.findings if f.start_line < 20)
    assert finding_inside.is_in_changed_code is True


def test_positional_only_parameters_counting_and_keyword_rejection() -> None:
    """Positional-only parameters must be counted properly and rejected when passed as keywords."""
    pr = _make_dummy_pr()

    # Function with positional-only dividend and divisor, optional precision
    changed_file = ChangedFile(
        filename="src/math_ops.py",
        status=FileChangeStatus.MODIFIED,
        hunks=[
            DiffHunk(
                old_start=1,
                old_lines=2,
                new_start=1,
                new_lines=2,
                lines=["+def divide(dividend, divisor, /, precision=2): pass"],
            )
        ],
    )

    def_snippet = CodeSnippet(
        file_path="src/math_ops.py",
        start_line=1,
        end_line=5,
        content=(
            "def divide(dividend, divisor, /, precision=2):\n"
            "    return round(dividend / divisor, precision)\n"
        ),
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.ENCLOSING_SCOPE,
    )

    # Call 1: Missing required positional-only argument 'divisor'
    caller_missing_posonly = CodeSnippet(
        file_path="src/caller_missing.py",
        start_line=1,
        end_line=5,
        content="from src.math_ops import divide\n\ndef run():\n    return divide(10)\n",
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.DIRECT_DEPENDENCY,
    )

    # Call 2: Positional-only parameter passed as keyword argument
    caller_posonly_as_kw = CodeSnippet(
        file_path="src/caller_kw.py",
        start_line=1,
        end_line=5,
        content=(
            "from src.math_ops import divide\n\n"
            "def run():\n"
            "    return divide(dividend=10, divisor=2)\n"
        ),
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.DIRECT_DEPENDENCY,
    )

    # Call 3: Valid positional arguments
    caller_valid = CodeSnippet(
        file_path="src/caller_valid.py",
        start_line=1,
        end_line=5,
        content=(
            "from src.math_ops import divide\n\ndef run():\n    return divide(10, 2, precision=4)\n"
        ),
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.DIRECT_DEPENDENCY,
    )

    pkg = ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[changed_file],
        evidence_snippets=[
            def_snippet,
            caller_missing_posonly,
            caller_posonly_as_kw,
            caller_valid,
        ],
    )

    result = localize_bugs(pkg)
    assert len(result.findings) == 2

    # Verify caller_missing finding
    f_missing = next(f for f in result.findings if f.file_path == "src/caller_missing.py")
    assert "missing required positional argument" in f_missing.summary
    assert "divisor" in f_missing.detailed_explanation
    assert f_missing.severity == BugSeverity.HIGH

    # Verify caller_kw finding
    f_kw = next(f for f in result.findings if f.file_path == "src/caller_kw.py")
    assert "positional-only argument(s) passed as keyword argument(s)" in f_kw.summary
    assert "dividend" in f_kw.detailed_explanation or "divisor" in f_kw.detailed_explanation
    assert f_kw.severity == BugSeverity.HIGH


def test_localize_bugs_same_file_sibling_callee_missing_argument() -> None:
    """Ensure missing argument defect is localized when callee is a same-file sibling."""
    pr = _make_dummy_pr()
    changed_file = ChangedFile(
        filename="billing.py",
        status=FileChangeStatus.MODIFIED,
        hunks=[
            DiffHunk(
                old_start=4,
                old_lines=3,
                new_start=4,
                new_lines=3,
                lines=[
                    " def final_price(subtotal, rate):",
                    "-    tax = calculate_tax(subtotal, rate)",
                    "+    tax = calculate_tax(subtotal)",
                    "     return subtotal + tax",
                ],
            )
        ],
    )
    enclosing_snippet = CodeSnippet(
        file_path="billing.py",
        start_line=4,
        end_line=6,
        content=(
            "def final_price(subtotal, rate):\n"
            "    tax = calculate_tax(subtotal)\n"
            "    return subtotal + tax\n"
        ),
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.ENCLOSING_SCOPE,
    )
    sibling_callee_snippet = CodeSnippet(
        file_path="billing.py",
        start_line=1,
        end_line=2,
        content=("def calculate_tax(subtotal, rate):\n    return subtotal * rate\n"),
        commit_sha=pr.head_sha,
        origin=EvidenceOrigin.CALL_HIERARCHY,
    )

    pkg = ContextPackage(
        pr=pr,
        head_sha=pr.head_sha,
        base_sha=pr.base_sha,
        changed_files=[changed_file],
        evidence_snippets=[enclosing_snippet, sibling_callee_snippet],
    )

    result = localize_bugs(pkg)
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.finding_id == "BUG-001"
    assert finding.file_path == "billing.py"
    assert finding.start_line == 5
    assert finding.end_line == 5
    assert "missing required argument(s): 'rate'" in finding.summary
    assert finding.severity == BugSeverity.HIGH
    assert finding.confidence == ConfidenceLevel.STRONG
    assert len(finding.supporting_evidence) == 2


def test_is_line_in_diff_distinguishes_added_lines_from_context_lines() -> None:
    """_is_line_in_diff must return True only for added/modified lines (+), not unchanged context lines."""
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
        hunks=[hunk],
    )

    # Added/modified statement on line 7
    assert _is_line_in_diff("billing.py", 7, 7, [cf]) is True

    # Unchanged context lines on lines 4, 5, 6, 8
    assert _is_line_in_diff("billing.py", 6, 6, [cf]) is False
    assert _is_line_in_diff("billing.py", 8, 8, [cf]) is False
    assert _is_line_in_diff("billing.py", 4, 5, [cf]) is False

    # Overlapping span covering line 7 returns True
    assert _is_line_in_diff("billing.py", 6, 8, [cf]) is True

