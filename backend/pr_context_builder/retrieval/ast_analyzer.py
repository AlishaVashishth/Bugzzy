"""Deterministic Python AST symbol and import extraction.

Uses Python's standard library ast module to accurately extract functions,
classes, methods, imports, and line boundaries without hallucination.
"""

import ast

from pr_context_builder.exceptions import SyntaxParseError
from pr_context_builder.models.repository import SymbolReference, SymbolType


class PythonASTAnalyzer(ast.NodeVisitor):
    """AST Visitor that traverses Python source code to record verifiable symbols."""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.symbols: list[SymbolReference] = []
        self.imported_modules: set[str] = set()
        self._scope_stack: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        parent = ".".join(self._scope_stack) if self._scope_stack else None
        self.symbols.append(
            SymbolReference(
                name=node.name,
                symbol_type=SymbolType.CLASS,
                file_path=self.file_path,
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                parent_scope=parent,
            )
        )
        self._scope_stack.append(node.name)
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_function(node)

    def _record_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        is_method = bool(self._scope_stack)
        symbol_type = SymbolType.METHOD if is_method else SymbolType.FUNCTION
        if node.name.startswith("test_"):
            symbol_type = SymbolType.TEST_CASE

        parent = ".".join(self._scope_stack) if self._scope_stack else None
        self.symbols.append(
            SymbolReference(
                name=node.name,
                symbol_type=symbol_type,
                file_path=self.file_path,
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                parent_scope=parent,
            )
        )
        self._scope_stack.append(node.name)
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imported_modules.add(alias.name)
            self.symbols.append(
                SymbolReference(
                    name=alias.name,
                    symbol_type=SymbolType.IMPORT,
                    file_path=self.file_path,
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                )
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        self.imported_modules.add(module)
        for alias in node.names:
            full_name = f"{module}.{alias.name}" if module else alias.name
            self.symbols.append(
                SymbolReference(
                    name=full_name,
                    symbol_type=SymbolType.IMPORT,
                    file_path=self.file_path,
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                )
            )
        self.generic_visit(node)


def extract_python_symbols(source_code: str, file_path: str) -> list[SymbolReference]:
    """Deterministically extracts code symbols from Python source code.

    Args:
        source_code: Raw Python code.
        file_path: Relative file path for referencing.

    Returns:
        List of SymbolReference objects with accurate 1-indexed line numbers.

    Raises:
        SyntaxParseError: If the source code cannot be parsed by ast.parse.
    """
    try:
        tree = ast.parse(source_code, filename=file_path)
    except SyntaxError as exc:
        raise SyntaxParseError(f"Could not parse syntax of {file_path}: {exc}") from exc

    analyzer = PythonASTAnalyzer(file_path=file_path)
    analyzer.visit(tree)
    return analyzer.symbols
