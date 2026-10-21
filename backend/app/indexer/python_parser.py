"""Pure Python AST parser for extracting symbols and dependency edges.

Zero database or network access; completely deterministic and unit-testable.
"""

from __future__ import annotations

import ast
import hashlib
import posixpath
from dataclasses import dataclass, field


@dataclass
class ParsedSymbol:
    name: str
    qualified_name: str
    kind: str  # 'module' | 'class' | 'function' | 'method'
    signature: str | None
    docstring: str | None
    start_line: int
    end_line: int
    content_hash: str | None


@dataclass
class ParsedEdge:
    source_qualified_name: str
    target_name: str
    relation: str  # 'calls' | 'imports' | 'inherits' | 'tests'
    call_line: int


@dataclass
class ParseResult:
    symbols: list[ParsedSymbol] = field(default_factory=list)
    edges: list[ParsedEdge] = field(default_factory=list)
    imported_names: dict[str, str] = field(default_factory=dict)  # local alias -> target


def module_path_from_path(file_path: str) -> str:
    """Derive dotted module path from repository-relative file path."""
    clean = file_path.replace("\\", "/").strip("/")
    if clean.endswith("/__init__.py"):
        clean = clean[:-len("/__init__.py")]
    elif clean == "__init__.py":
        return "__init__"
    elif clean.endswith(".py"):
        clean = clean[:-len(".py")]
    return clean.replace("/", ".")


def is_test_path(file_path: str) -> bool:
    """Check if the given path is a test file."""
    clean = file_path.replace("\\", "/").lower()
    base = posixpath.basename(clean)
    return (
        clean.startswith("tests/")
        or clean.startswith("test/")
        or "/tests/" in clean
        or "/test/" in clean
        or base.startswith("test_")
        or base.endswith("_test.py")
    )


class SymbolExtractor(ast.NodeVisitor):
    def __init__(self, source_code: str, file_path: str):
        self.source_code = source_code
        self.file_path = file_path
        self.module_path = module_path_from_path(file_path)
        self.is_test_file = is_test_path(file_path)

        self.symbols: list[ParsedSymbol] = []
        self.edges: list[ParsedEdge] = []
        self.imported_names: dict[str, str] = {}

        # Stack tracking current scopes: list of (scope_name, kind, qualified_name)
        self._scope_stack: list[tuple[str, str, str]] = []

    def _hash_node(self, node: ast.AST | None) -> str:
        if node is None:
            return hashlib.sha256(self.source_code.encode("utf-8")).hexdigest()
        segment = ast.get_source_segment(self.source_code, node)
        if segment is None:
            try:
                segment = ast.unparse(node)
            except Exception:
                segment = ""
        return hashlib.sha256(segment.encode("utf-8")).hexdigest()

    def _start_line(self, node: ast.AST) -> int:
        if hasattr(node, "decorator_list") and node.decorator_list:
            return min(d.lineno for d in node.decorator_list)
        return getattr(node, "lineno", 1)

    def _end_line(self, node: ast.AST) -> int:
        return getattr(node, "end_lineno", getattr(node, "lineno", 1))

    def parse(self) -> ParseResult:
        tree = ast.parse(self.source_code, filename=self.file_path)

        # 1. Module-level symbol
        module_name = posixpath.splitext(posixpath.basename(self.file_path))[0]
        if module_name == "__init__":
            parent_dir = posixpath.basename(posixpath.dirname(self.file_path))
            if parent_dir:
                module_name = parent_dir

        total_lines = len(self.source_code.splitlines()) or 1
        module_symbol = ParsedSymbol(
            name=module_name,
            qualified_name=self.module_path,
            kind="module",
            signature=None,
            docstring=ast.get_docstring(tree),
            start_line=1,
            end_line=total_lines,
            content_hash=self._hash_node(None),
        )
        self.symbols.append(module_symbol)

        # 2. Traverse AST
        self.generic_visit(tree)

        return ParseResult(
            symbols=self.symbols,
            edges=self.edges,
            imported_names=self.imported_names,
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            target = alias.name
            asname = alias.asname or alias.name
            self.imported_names[asname] = target
            self.edges.append(
                ParsedEdge(
                    source_qualified_name=self.module_path,
                    target_name=target,
                    relation="imports",
                    call_line=node.lineno,
                )
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level > 0:
            # Relative import
            parts = self.module_path.split(".")
            base_parts = parts[:-node.level] if len(parts) >= node.level else []
            if module:
                resolved_mod = ".".join(base_parts + [module])
            else:
                resolved_mod = ".".join(base_parts)
        else:
            resolved_mod = module

        for alias in node.names:
            if resolved_mod:
                target = f"{resolved_mod}.{alias.name}"
            else:
                target = alias.name
            asname = alias.asname or alias.name
            self.imported_names[asname] = target
            self.edges.append(
                ParsedEdge(
                    source_qualified_name=self.module_path,
                    target_name=target,
                    relation="imports",
                    call_line=node.lineno,
                )
            )
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        parent_qname = (
            self._scope_stack[-1][2] if self._scope_stack else self.module_path
        )
        class_qname = f"{parent_qname}.{node.name}"

        # Signature
        if node.bases:
            bases_str = ", ".join(ast.unparse(b) for b in node.bases)
            sig = f"{node.name}({bases_str})"
        else:
            sig = node.name

        sym = ParsedSymbol(
            name=node.name,
            qualified_name=class_qname,
            kind="class",
            signature=sig,
            docstring=ast.get_docstring(node),
            start_line=self._start_line(node),
            end_line=self._end_line(node),
            content_hash=self._hash_node(node),
        )
        self.symbols.append(sym)

        # Inherits edges
        for base in node.bases:
            target_name = ast.unparse(base)
            self.edges.append(
                ParsedEdge(
                    source_qualified_name=class_qname,
                    target_name=target_name,
                    relation="inherits",
                    call_line=node.lineno,
                )
            )

        self._scope_stack.append((node.name, "class", class_qname))
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._handle_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._handle_function(node)

    def _handle_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        is_method = bool(self._scope_stack and self._scope_stack[-1][1] == "class")
        kind = "method" if is_method else "function"

        parent_qname = (
            self._scope_stack[-1][2] if self._scope_stack else self.module_path
        )
        func_qname = f"{parent_qname}.{node.name}"

        try:
            args_str = ast.unparse(node.args)
        except Exception:
            args_str = ""
        sig = f"{node.name}({args_str})"

        sym = ParsedSymbol(
            name=node.name,
            qualified_name=func_qname,
            kind=kind,
            signature=sig,
            docstring=ast.get_docstring(node),
            start_line=self._start_line(node),
            end_line=self._end_line(node),
            content_hash=self._hash_node(node),
        )
        self.symbols.append(sym)

        self._scope_stack.append((node.name, kind, func_qname))
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if self._scope_stack:
            source_qname = self._scope_stack[-1][2]
            source_name = self._scope_stack[-1][0]

            try:
                if isinstance(node.func, ast.Name):
                    target_name = node.func.id
                else:
                    target_name = ast.unparse(node.func)
            except Exception:
                target_name = "unknown"

            # Determine relation: if inside a test function or test file
            is_test = (
                self.is_test_file
                or source_name.startswith("test_")
                or source_name.startswith("Test")
            )
            relation = "tests" if is_test else "calls"

            self.edges.append(
                ParsedEdge(
                    source_qualified_name=source_qname,
                    target_name=target_name,
                    relation=relation,
                    call_line=getattr(node, "lineno", 1),
                )
            )

        self.generic_visit(node)


def parse_python_source(source_code: str, file_path: str) -> ParseResult:
    """Parse Python source code and return symbols, edges, and imports."""
    extractor = SymbolExtractor(source_code, file_path)
    return extractor.parse()
