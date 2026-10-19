"""Unit tests for diff parsing and AST analysis setup."""

import pytest

from pr_context_builder.exceptions import SyntaxParseError
from pr_context_builder.github.diff_parser import parse_patch_hunks
from pr_context_builder.models.repository import SymbolType
from pr_context_builder.retrieval.ast_analyzer import extract_python_symbols

SAMPLE_DIFF_PATCH = """@@ -15,7 +15,9 @@ def authenticate(user):
-    token = generate_legacy_token(user)
+    if not user.is_active:
+        raise InactiveUserError()
+    token = generate_jwt_token(user)
     return token
"""

SAMPLE_PYTHON_CODE = """import os
from datetime import datetime

class PRService:
    def __init__(self, token: str):
        self.token = token

    def fetch_pr(self, number: int) -> dict:
        return {"number": number}

def test_fetch_pr():
    assert True
"""


def test_parse_patch_hunks() -> None:
    """Ensure diff parser accurately extracts line numbers and hunk details."""
    hunks = parse_patch_hunks(SAMPLE_DIFF_PATCH)
    assert len(hunks) == 1
    hunk = hunks[0]
    assert hunk.old_start == 15
    assert hunk.old_lines == 7
    assert hunk.new_start == 15
    assert hunk.new_lines == 9
    assert hunk.header == "def authenticate(user):"
    assert len(hunk.lines) == 5


def test_extract_python_symbols() -> None:
    """Ensure AST analyzer accurately extracts classes, methods, and functions with line numbers."""
    symbols = extract_python_symbols(SAMPLE_PYTHON_CODE, "src/pr_service.py")

    names = {s.name: s for s in symbols}

    assert "PRService" in names
    assert names["PRService"].symbol_type == SymbolType.CLASS
    assert names["PRService"].start_line == 4

    assert "__init__" in names
    assert names["__init__"].symbol_type == SymbolType.METHOD
    assert names["__init__"].parent_scope == "PRService"

    assert "fetch_pr" in names
    assert names["fetch_pr"].symbol_type == SymbolType.METHOD

    assert "test_fetch_pr" in names
    assert names["test_fetch_pr"].symbol_type == SymbolType.TEST_CASE


def test_ast_syntax_error_handling() -> None:
    """Ensure malformed Python raises SyntaxParseError explicitly rather than failing silently."""
    malformed_code = "def broken_syntax(:\n    pass"
    with pytest.raises(SyntaxParseError):
        extract_python_symbols(malformed_code, "broken.py")
