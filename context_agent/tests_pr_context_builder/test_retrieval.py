"""Unit tests for repository code locator and related code discovery.

Runs completely offline using synthetic source files and mocked GitHub responses.
"""

from unittest.mock import MagicMock

import pytest

from pr_context_builder.exceptions import ResourceNotFoundError
from pr_context_builder.github.client import GitHubClientProtocol
from pr_context_builder.models.context_package import LimitationType
from pr_context_builder.models.pr_metadata import (
    ChangedFile,
    DiffHunk,
    FileChangeStatus,
    PullRequest,
)
from pr_context_builder.models.repository import EvidenceOrigin, SymbolType
from pr_context_builder.retrieval.locator import RepositoryCodeLocator

PYTHON_SOURCE = """# Module docstring and comments
GLOBAL_CONFIG = {"debug": True}

class UserService:
    \"\"\"Manages user operations.\"\"\"

    def __init__(self, db_client):
        self.db = db_client

    def update_user_email(self, user_id: int, new_email: str) -> bool:
        if not new_email or "@" not in new_email:
            raise ValueError("Invalid email format")
        return self.db.update(user_id, email=new_email)

def format_user_display(name: str, role: str) -> str:
    return f"{name} ({role})"
"""


@pytest.fixture
def locator() -> RepositoryCodeLocator:
    return RepositoryCodeLocator(max_candidate_files=5, max_file_size_bytes=5000)


def test_locate_changes_inside_method(locator: RepositoryCodeLocator) -> None:
    """Ensure changes inside a method return the enclosing method snippet with commit SHA."""
    hunk = DiffHunk(
        old_start=11,
        old_lines=2,
        new_start=11,
        new_lines=4,
        header="def update_user_email():",
        lines=[
            "     def update_user_email(self, user_id: int, new_email: str) -> bool:",
            "+        if not new_email or '@' not in new_email:",
            "+            raise ValueError('Invalid email format')",
        ],
    )
    changed_file = ChangedFile(
        filename="src/services/user.py",
        status=FileChangeStatus.MODIFIED,
        additions=2,
        deletions=0,
        changes=2,
        patch="dummy_patch",
        hunks=[hunk],
    )

    snippets, limitations = locator.locate_enclosing_symbols(
        changed_file=changed_file,
        source_code=PYTHON_SOURCE,
        commit_sha="c0ffee123456",
    )

    assert len(limitations) == 0
    assert len(snippets) == 1
    snippet = snippets[0]
    assert snippet.file_path == "src/services/user.py"
    assert snippet.commit_sha == "c0ffee123456"
    assert snippet.origin == EvidenceOrigin.ENCLOSING_SCOPE
    assert snippet.start_line == 10
    assert snippet.end_line == 13
    assert "def update_user_email" in snippet.content
    assert any(s.name == "update_user_email" for s in snippet.symbols)


def test_locate_changes_inside_class_header(locator: RepositoryCodeLocator) -> None:
    """Ensure changes to a class definition map to the class construct."""
    hunk = DiffHunk(
        old_start=4,
        old_lines=2,
        new_start=4,
        new_lines=2,
        header="class UserService:",
        lines=["-class UserService:", "+class UserService(BaseService):"],
    )
    changed_file = ChangedFile(
        filename="src/services/user.py",
        status=FileChangeStatus.MODIFIED,
        additions=1,
        deletions=1,
        changes=2,
        patch="dummy_patch",
        hunks=[hunk],
    )

    snippets, limitations = locator.locate_enclosing_symbols(
        changed_file=changed_file,
        source_code=PYTHON_SOURCE,
        commit_sha="c0ffee123456",
    )

    assert len(snippets) == 1
    assert snippets[0].start_line == 4
    assert any(
        s.name == "UserService" and s.symbol_type == SymbolType.CLASS for s in snippets[0].symbols
    )


def test_locate_changes_not_mapped_to_symbol(locator: RepositoryCodeLocator) -> None:
    """Ensure top-level changes (comments/globals) return direct diff lines without fake symbols."""
    hunk = DiffHunk(
        old_start=1,
        old_lines=2,
        new_start=1,
        new_lines=2,
        header="Top level config",
        lines=["-GLOBAL_CONFIG = {'debug': True}", "+GLOBAL_CONFIG = {'debug': False}"],
    )
    changed_file = ChangedFile(
        filename="src/services/user.py",
        status=FileChangeStatus.MODIFIED,
        additions=1,
        deletions=1,
        changes=2,
        patch="dummy_patch",
        hunks=[hunk],
    )

    snippets, limitations = locator.locate_enclosing_symbols(
        changed_file=changed_file,
        source_code=PYTHON_SOURCE,
        commit_sha="c0ffee123456",
    )

    assert len(snippets) == 1
    snippet = snippets[0]
    assert snippet.origin == EvidenceOrigin.DIRECT_DIFF
    assert snippet.symbols == []
    assert snippet.start_line == 1
    assert "GLOBAL_CONFIG" in snippet.content


def test_locate_deleted_and_binary_files(locator: RepositoryCodeLocator) -> None:
    """Ensure deleted and binary files produce explicit limitations without crashing."""
    # 1. Deleted file
    deleted_file = ChangedFile(
        filename="src/legacy.py",
        status=FileChangeStatus.REMOVED,
        patch="dummy",
    )
    snips_del, limits_del = locator.locate_enclosing_symbols(deleted_file, None, "sha1")
    assert len(snips_del) == 0
    assert len(limits_del) == 1
    assert limits_del[0].limitation_type == LimitationType.FILE_NOT_FOUND

    # 2. Binary file
    binary_file = ChangedFile(
        filename="assets/image.png",
        status=FileChangeStatus.MODIFIED,
        is_binary=True,
        patch=None,
    )
    snips_bin, limits_bin = locator.locate_enclosing_symbols(binary_file, None, "sha1")
    assert len(snips_bin) == 0
    assert len(limits_bin) == 1
    assert limits_bin[0].limitation_type == LimitationType.BINARY_FILE_SKIPPED


def test_locate_non_python_files(locator: RepositoryCodeLocator) -> None:
    """Ensure non-Python files emit UNSUPPORTED_LANGUAGE limitation and preserve raw line ranges."""
    ts_source = (
        "function calculateTotal(items: number[]): number {\n"
        "  return items.reduce((a, b) => a + b, 0);\n"
        "}\n"
    )
    hunk = DiffHunk(
        old_start=1,
        old_lines=3,
        new_start=1,
        new_lines=3,
        lines=["+function calculateTotal..."],
    )
    changed_file = ChangedFile(
        filename="src/calculator.ts",
        status=FileChangeStatus.MODIFIED,
        patch="dummy",
        hunks=[hunk],
    )

    snippets, limitations = locator.locate_enclosing_symbols(changed_file, ts_source, "sha1")

    assert len(limitations) == 1
    assert limitations[0].limitation_type == LimitationType.UNSUPPORTED_LANGUAGE
    assert len(snippets) == 1
    assert snippets[0].origin == EvidenceOrigin.DIRECT_DIFF
    assert snippets[0].language == "ts"
    assert snippets[0].symbols == []


def test_locate_malformed_python_source(locator: RepositoryCodeLocator) -> None:
    """Ensure syntax errors emit SYNTAX_PARSE_ERROR limitation and fallback to direct diff lines."""
    broken_code = "def broken(\n  # unclosed parenthesis"

    hunk = DiffHunk(old_start=1, old_lines=1, new_start=1, new_lines=2, lines=["+broken"])
    changed_file = ChangedFile(
        filename="src/broken.py",
        status=FileChangeStatus.MODIFIED,
        patch="dummy",
        hunks=[hunk],
    )

    snippets, limitations = locator.locate_enclosing_symbols(changed_file, broken_code, "sha1")

    assert len(limitations) == 1
    assert limitations[0].limitation_type == LimitationType.SYNTAX_PARSE_ERROR
    assert len(snippets) == 1
    assert snippets[0].origin == EvidenceOrigin.DIRECT_DIFF


def test_discover_related_code_confirmed_vs_heuristic(locator: RepositoryCodeLocator) -> None:
    """Ensure confirmed AST imports and unconfirmed text matches are strictly differentiated."""
    from pr_context_builder.models.repository import SymbolReference

    modified_sym = SymbolReference(
        name="update_user_email",
        symbol_type=SymbolType.METHOD,
        file_path="src/services/user.py",
        start_line=10,
        end_line=13,
        parent_scope="UserService",
    )

    # Candidate 1: imports UserService from module (Confirmed AST)
    candidate_test = (
        "from src.services.user import UserService\n\n"
        "def test_user():\n"
        "    service = UserService(None)\n"
        "    service.update_user_email(1, 'test@example.com')\n"
    )

    # Candidate 2: text reference only, no AST import (Heuristic text match)
    candidate_docs = (
        "# API Documentation\n"
        "# To change user email, call update_user_email on UserService instance.\n"
    )

    candidates = {
        "tests/test_user.py": candidate_test,
        "docs/api.md": candidate_docs,
    }

    snippets, relationships, limitations = locator.discover_related_code(
        modified_symbols=[modified_sym],
        candidate_files=candidates,
        commit_sha="c0ffee",
    )

    assert len(relationships) == 2

    # Find AST confirmed relationship
    ast_rel = next(r for r in relationships if r.heuristic_used == "ast_confirmed_import")
    assert ast_rel.source_symbol == "tests/test_user.py"
    assert ast_rel.confidence == 1.0
    assert ast_rel.relationship_type == "tests"

    # Find heuristic text relationship
    text_rel = next(
        r for r in relationships if r.heuristic_used == "textual_name_match_unconfirmed"
    )
    assert text_rel.source_symbol == "docs/api.md"
    assert text_rel.confidence < 0.5
    assert text_rel.relationship_type == "possible_text_reference"


def test_discover_related_code_limits(locator: RepositoryCodeLocator) -> None:
    """Ensure candidate file count and size limits trigger BUDGET_EXCEEDED limitations."""
    from pr_context_builder.models.repository import SymbolReference

    sym = SymbolReference(
        name="my_func",
        symbol_type=SymbolType.FUNCTION,
        file_path="a.py",
        start_line=1,
        end_line=5,
    )

    # 1. Test size limit (locator limit is 5000 bytes)
    huge_content = "x = 1\n" * 2000  # > 5000 bytes
    snippets, rels, limits = locator.discover_related_code([sym], {"huge.py": huge_content}, "sha1")
    assert any(lim.limitation_type == LimitationType.BUDGET_EXCEEDED for lim in limits)
    assert len(snippets) == 0

    # 2. Test candidate count limit (locator limit is 5)
    many_candidates = {f"file_{i}.py": f"# my_func ref in {i}\n" for i in range(10)}
    snippets, rels, limits = locator.discover_related_code([sym], many_candidates, "sha1")
    assert any(
        lim.limitation_type == LimitationType.BUDGET_EXCEEDED
        and "Candidate file scan limited" in lim.message
        for lim in limits
    )


def test_locate_pr_code_with_mock_github_client() -> None:
    """Ensure locate_pr_code queries GitHubClient using exact commit SHA."""
    mock_client = MagicMock(spec=GitHubClientProtocol)

    hunk = DiffHunk(old_start=1, old_lines=1, new_start=1, new_lines=1, lines=["+pass"])
    changed_file = ChangedFile(
        filename="src/main.py",
        status=FileChangeStatus.MODIFIED,
        patch="dummy",
        hunks=[hunk],
    )
    pr = PullRequest(
        owner="octocat",
        repo="repo",
        number=1,
        title="Title",
        base_sha="base_sha_123",
        base_branch="main",
        head_sha="head_sha_456",
        head_branch="feature",
        changed_files=[changed_file],
    )

    mock_client.get_file_content.return_value = "def main():\n    pass\n"

    locator = RepositoryCodeLocator(github_client=mock_client)
    result = locator.locate_pr_code(pr)

    assert len(result.snippets) == 1
    assert result.snippets[0].commit_sha == "head_sha_456"
    assert result.snippets[0].start_line == 1
    assert result.snippets[0].end_line == 2

    # Verify commit SHA was explicitly passed to get_file_content
    mock_client.get_file_content.assert_called_once_with(
        "octocat", "repo", "src/main.py", "head_sha_456"
    )


def test_locate_pr_code_handles_api_error() -> None:
    """Ensure API errors when fetching content are captured as limitations without raising."""
    mock_client = MagicMock(spec=GitHubClientProtocol)
    mock_client.get_file_content.side_effect = ResourceNotFoundError("File not found on commit")

    changed_file = ChangedFile(
        filename="src/missing.py",
        status=FileChangeStatus.MODIFIED,
        patch="dummy",
        hunks=[DiffHunk(old_start=1, old_lines=1, new_start=1, new_lines=1, lines=["+x"])],
    )
    pr = PullRequest(
        owner="octocat",
        repo="repo",
        number=1,
        title="Title",
        base_sha="base",
        base_branch="main",
        head_sha="head",
        head_branch="feature",
        changed_files=[changed_file],
    )

    locator = RepositoryCodeLocator(github_client=mock_client)
    result = locator.locate_pr_code(pr)

    assert any(lim.limitation_type == LimitationType.FILE_NOT_FOUND for lim in result.limitations)


def test_locate_enclosing_symbols_retrieves_called_sibling_function(
    locator: RepositoryCodeLocator,
) -> None:
    """Ensure bare calls inside an enclosing function retrieve top-level sibling definitions."""
    source = (
        "def calculate_tax(subtotal, rate):\n"
        "    return subtotal * rate\n"
        "\n"
        "def final_price(subtotal, rate):\n"
        "    tax = calculate_tax(subtotal)\n"
        "    return subtotal + tax\n"
    )
    hunk = DiffHunk(
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
    changed_file = ChangedFile(
        filename="billing.py",
        status=FileChangeStatus.MODIFIED,
        patch="diff_patch",
        hunks=[hunk],
    )

    snippets, limitations = locator.locate_enclosing_symbols(
        changed_file=changed_file,
        source_code=source,
        commit_sha="commit_abc123",
    )

    assert len(snippets) == 2
    # Snippet 1: Enclosing scope
    assert snippets[0].origin == EvidenceOrigin.ENCLOSING_SCOPE
    assert snippets[0].start_line == 4
    assert snippets[0].end_line == 6
    assert snippets[0].commit_sha == "commit_abc123"

    # Snippet 2: Sibling callee
    assert snippets[1].origin == EvidenceOrigin.CALL_HIERARCHY
    assert snippets[1].start_line == 1
    assert snippets[1].end_line == 2
    assert "def calculate_tax(subtotal, rate):" in snippets[1].content
    assert snippets[1].commit_sha == "commit_abc123"
    assert snippets[1].file_path == "billing.py"


def test_locate_enclosing_symbols_no_false_match_bare_call_and_method(
    locator: RepositoryCodeLocator,
) -> None:
    """A bare call must never associate with a class method of the same name."""
    source = (
        "class Invoice:\n"
        "    def calculate_tax(self, subtotal, rate):\n"
        "        return subtotal * rate\n"
        "\n"
        "def final_price(subtotal):\n"
        "    tax = calculate_tax(subtotal)\n"
        "    return subtotal + tax\n"
    )
    hunk = DiffHunk(
        old_start=5,
        old_lines=3,
        new_start=5,
        new_lines=3,
        lines=[
            " def final_price(subtotal):",
            "-    tax = 0",
            "+    tax = calculate_tax(subtotal)",
            "     return subtotal + tax",
        ],
    )
    changed_file = ChangedFile(
        filename="billing.py",
        status=FileChangeStatus.MODIFIED,
        patch="diff_patch",
        hunks=[hunk],
    )

    snippets, limitations = locator.locate_enclosing_symbols(
        changed_file=changed_file,
        source_code=source,
        commit_sha="commit_abc123",
    )

    # Only final_price should be returned; Invoice.calculate_tax must NOT be falsely matched
    assert len(snippets) == 1
    assert snippets[0].origin == EvidenceOrigin.ENCLOSING_SCOPE
    assert snippets[0].start_line == 5
    assert snippets[0].end_line == 7


def test_locate_enclosing_symbols_self_method_call_resolution(
    locator: RepositoryCodeLocator,
) -> None:
    """self.method() calls must resolve to sibling methods in the same class."""
    source = (
        "class InvoiceCalculator:\n"
        "    def compute_discount(self, total):\n"
        "        return total * 0.1\n"
        "\n"
        "    def compute_total(self, total):\n"
        "        disc = self.compute_discount(total)\n"
        "        return total - disc\n"
    )
    hunk = DiffHunk(
        old_start=5,
        old_lines=3,
        new_start=5,
        new_lines=3,
        lines=[
            "     def compute_total(self, total):",
            "-        disc = 0",
            "+        disc = self.compute_discount(total)",
            "         return total - disc",
        ],
    )
    changed_file = ChangedFile(
        filename="calculator.py",
        status=FileChangeStatus.MODIFIED,
        patch="diff_patch",
        hunks=[hunk],
    )

    snippets, limitations = locator.locate_enclosing_symbols(
        changed_file=changed_file,
        source_code=source,
        commit_sha="commit_sha_111",
    )

    assert len(snippets) == 2
    assert snippets[0].origin == EvidenceOrigin.ENCLOSING_SCOPE
    assert snippets[0].start_line == 5
    assert snippets[0].end_line == 7

    assert snippets[1].origin == EvidenceOrigin.CALL_HIERARCHY
    assert snippets[1].start_line == 2
    assert snippets[1].end_line == 3
    assert "compute_discount" in snippets[1].content


def test_locate_enclosing_symbols_duplicate_prevention_and_max_limit(
    locator: RepositoryCodeLocator,
) -> None:
    """Duplicate calls are de-duplicated and total sibling snippets capped at 5."""
    source_lines = [
        "def h1(): pass",
        "def h2(): pass",
        "def h3(): pass",
        "def h4(): pass",
        "def h5(): pass",
        "def h6(): pass",
        "def h7(): pass",
        "def main_func():",
        "    h1()",
        "    h1()",  # duplicate
        "    h2()",
        "    h3()",
        "    h4()",
        "    h5()",
        "    h6()",  # should be excluded by limit (5 max)
        "    h7()",
    ]
    source = "\n".join(source_lines) + "\n"
    hunk = DiffHunk(
        old_start=8,
        old_lines=9,
        new_start=8,
        new_lines=9,
        lines=[" def main_func():"] + [f"+    {line.strip()}" for line in source_lines[8:]],
    )
    changed_file = ChangedFile(
        filename="utils.py",
        status=FileChangeStatus.MODIFIED,
        patch="diff_patch",
        hunks=[hunk],
    )

    snippets, limitations = locator.locate_enclosing_symbols(
        changed_file=changed_file,
        source_code=source,
        commit_sha="commit_limit_test",
    )

    # 1 enclosing scope (main_func) + max 5 sibling snippets = 6 total
    assert len(snippets) == 6
    assert snippets[0].origin == EvidenceOrigin.ENCLOSING_SCOPE
    sibling_snippets = snippets[1:]
    assert all(s.origin == EvidenceOrigin.CALL_HIERARCHY for s in sibling_snippets)

    # Check distinct line ranges (no duplicates)
    ranges = [(s.start_line, s.end_line) for s in sibling_snippets]
    assert len(ranges) == len(set(ranges))
    assert len(sibling_snippets) == 5
