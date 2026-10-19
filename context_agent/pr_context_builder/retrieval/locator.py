"""Deterministic repository code and related symbol locator.

Maps PR diff hunks to enclosing functions, methods, and classes using AST analysis,
and discovers related repository code while strictly distinguishing confirmed AST
relationships from unconfirmed textual matches.
"""

import ast
from dataclasses import dataclass, field

from pr_context_builder.exceptions import (
    GitHubAPIError,
    SyntaxParseError,
)
from pr_context_builder.github.client import GitHubClientProtocol
from pr_context_builder.models.context_package import (
    InferredRelationship,
    LimitationType,
    RetrievalLimitation,
)
from pr_context_builder.models.pr_metadata import (
    ChangedFile,
    DiffHunk,
    FileChangeStatus,
    PullRequest,
)
from pr_context_builder.models.repository import (
    CodeSnippet,
    EvidenceOrigin,
    SymbolReference,
    SymbolType,
)
from pr_context_builder.retrieval.ast_analyzer import (
    PythonASTAnalyzer,
    extract_python_symbols,
)


@dataclass
class LocatedCodeResult:
    """Consolidated outcome of code and relationship location."""

    snippets: list[CodeSnippet] = field(default_factory=list)
    relationships: list[InferredRelationship] = field(default_factory=list)
    limitations: list[RetrievalLimitation] = field(default_factory=list)


MAX_SIBLING_SNIPPETS_PER_FILE: int = 5


def _hunk_line_range(hunk: DiffHunk, is_removed: bool) -> tuple[int, int]:
    """Calculates 1-indexed [start_line, end_line] affected by a diff hunk."""
    if is_removed:
        start = hunk.old_start
        lines = max(hunk.old_lines, 1)
    else:
        start = hunk.new_start
        lines = max(hunk.new_lines, 1)
    return start, start + lines - 1


def _is_overlapping(range_a: tuple[int, int], range_b: tuple[int, int]) -> bool:
    """Checks whether two closed integer intervals overlap."""
    return range_a[0] <= range_b[1] and range_a[1] >= range_b[0]


class RepositoryCodeLocator:
    """Locates affected code constructs and related repository context at an exact commit SHA."""

    def __init__(
        self,
        github_client: GitHubClientProtocol | None = None,
        max_candidate_files: int = 20,
        max_file_size_bytes: int = 100_000,
    ):
        self._github_client = github_client
        self._max_candidate_files = max_candidate_files
        self._max_file_size_bytes = max_file_size_bytes

    def locate_enclosing_symbols(
        self,
        changed_file: ChangedFile,
        source_code: str | None,
        commit_sha: str,
    ) -> tuple[list[CodeSnippet], list[RetrievalLimitation]]:
        """Identifies functions, methods, or classes overlapping modified diff hunks.

        Args:
            changed_file: The modified file metadata with parsed diff hunks.
            source_code: Full content of the file at the target commit SHA (or None if unavailable).
            commit_sha: Immutable commit SHA at which source code was fetched.

        Returns:
            Tuple of (extracted CodeSnippets with exact line ranges, list of RetrievalLimitations).
        """
        snippets: list[CodeSnippet] = []
        limitations: list[RetrievalLimitation] = []

        # 1. Handle deleted files when source is unavailable
        if changed_file.status == FileChangeStatus.REMOVED and source_code is None:
            # Retain deletion diff hunks as direct evidence of deletion at base commit SHA
            for hunk in changed_file.hunks:
                start_l, end_l = _hunk_line_range(hunk, is_removed=True)
                snippets.append(
                    CodeSnippet(
                        file_path=changed_file.filename,
                        start_line=start_l,
                        end_line=end_l,
                        content="\n".join(hunk.lines),
                        commit_sha=commit_sha,
                        origin=EvidenceOrigin.DIRECT_DIFF,
                        language="python" if changed_file.filename.endswith(".py") else "text",
                        symbols=[],
                    )
                )
            limitations.append(
                RetrievalLimitation(
                    limitation_type=LimitationType.FILE_NOT_FOUND,
                    target_path=changed_file.filename,
                    message=(
                        f"File {changed_file.filename} was deleted in PR; "
                        "source content unavailable."
                    ),
                    suggested_mitigation=(
                        "Inspect base commit SHA if deleted code context is needed."
                    ),
                )
            )
            return snippets, limitations

        # 2. Handle binary files or files without patch data
        if changed_file.is_binary or not changed_file.patch:
            limitations.append(
                RetrievalLimitation(
                    limitation_type=LimitationType.BINARY_FILE_SKIPPED,
                    target_path=changed_file.filename,
                    message=(
                        f"File {changed_file.filename} is binary or lacks patch data; "
                        "AST analysis skipped."
                    ),
                )
            )
            return snippets, limitations

        # 3. Handle missing source code
        if source_code is None:
            limitations.append(
                RetrievalLimitation(
                    limitation_type=LimitationType.FILE_NOT_FOUND,
                    target_path=changed_file.filename,
                    message=(
                        f"Source content unavailable for {changed_file.filename} "
                        f"at commit {commit_sha}."
                    ),
                )
            )
            return snippets, limitations

        lines = source_code.splitlines(keepends=True)

        # 4. Handle non-Python files
        if not changed_file.filename.endswith(".py"):
            limitations.append(
                RetrievalLimitation(
                    limitation_type=LimitationType.UNSUPPORTED_LANGUAGE,
                    target_path=changed_file.filename,
                    message=(
                        f"Language-specific AST symbol extraction unavailable for non-Python file "
                        f"{changed_file.filename}. Preserving raw diff line ranges."
                    ),
                )
            )
            # Extract raw diff hunk line ranges without symbol mapping
            for hunk in changed_file.hunks:
                start_l, end_l = _hunk_line_range(
                    hunk, is_removed=(changed_file.status == FileChangeStatus.REMOVED)
                )
                start_idx = max(0, start_l - 1)
                end_idx = min(len(lines), end_l)
                content = "".join(lines[start_idx:end_idx]) if lines else "\n".join(hunk.lines)
                snippets.append(
                    CodeSnippet(
                        file_path=changed_file.filename,
                        start_line=start_l,
                        end_line=max(start_l, end_idx),
                        content=content,
                        commit_sha=commit_sha,
                        origin=EvidenceOrigin.DIRECT_DIFF,
                        language=changed_file.filename.rsplit(".", 1)[-1],
                        symbols=[],
                    )
                )
            return snippets, limitations

        # 5. Parse Python AST symbols
        try:
            file_symbols = extract_python_symbols(source_code, changed_file.filename)
        except SyntaxParseError as exc:
            limitations.append(
                RetrievalLimitation(
                    limitation_type=LimitationType.SYNTAX_PARSE_ERROR,
                    target_path=changed_file.filename,
                    message=(
                        f"Python syntax error in {changed_file.filename}: {exc}. "
                        "Enclosing symbol mapping unavailable."
                    ),
                )
            )
            # Fallback to direct hunk line ranges
            for hunk in changed_file.hunks:
                start_l, end_l = _hunk_line_range(hunk, is_removed=False)
                start_idx = max(0, start_l - 1)
                end_idx = min(len(lines), end_l)
                snippets.append(
                    CodeSnippet(
                        file_path=changed_file.filename,
                        start_line=start_l,
                        end_line=max(start_l, end_idx),
                        content="".join(lines[start_idx:end_idx]),
                        commit_sha=commit_sha,
                        origin=EvidenceOrigin.DIRECT_DIFF,
                        language="python",
                        symbols=[],
                    )
                )
            return snippets, limitations

        # Filter symbols to code constructs (functions, methods, classes)
        code_constructs = [
            s
            for s in file_symbols
            if s.symbol_type
            in (SymbolType.FUNCTION, SymbolType.METHOD, SymbolType.CLASS, SymbolType.TEST_CASE)
        ]

        seen_ranges: set[tuple[int, int]] = set()

        for hunk in changed_file.hunks:
            hunk_range = _hunk_line_range(
                hunk, is_removed=(changed_file.status == FileChangeStatus.REMOVED)
            )

            # Find all constructs overlapping this hunk
            overlapping = [
                s
                for s in code_constructs
                if _is_overlapping(hunk_range, (s.start_line, s.end_line))
            ]

            if not overlapping:
                # Changes at module level (e.g. comments, top-level assignments, imports)
                start_l, end_l = hunk_range
                start_idx = max(0, start_l - 1)
                end_idx = min(len(lines), end_l)
                chunk_range = (start_l, max(start_l, end_idx))

                if chunk_range not in seen_ranges:
                    seen_ranges.add(chunk_range)
                    snippets.append(
                        CodeSnippet(
                            file_path=changed_file.filename,
                            start_line=start_l,
                            end_line=max(start_l, end_idx),
                            content="".join(lines[start_idx:end_idx]),
                            commit_sha=commit_sha,
                            origin=EvidenceOrigin.DIRECT_DIFF,
                            language="python",
                            symbols=[],
                        )
                    )
            else:
                # Prefer most specific constructs (methods/functions over enclosing class)
                # Sort by smallest line span first
                overlapping.sort(key=lambda s: s.end_line - s.start_line)
                primary_symbol = overlapping[0]

                sym_range = (primary_symbol.start_line, primary_symbol.end_line)
                if sym_range not in seen_ranges:
                    seen_ranges.add(sym_range)
                    start_idx = max(0, primary_symbol.start_line - 1)
                    end_idx = min(len(lines), primary_symbol.end_line)
                    snippets.append(
                        CodeSnippet(
                            file_path=changed_file.filename,
                            start_line=primary_symbol.start_line,
                            end_line=primary_symbol.end_line,
                            content="".join(lines[start_idx:end_idx]),
                            commit_sha=commit_sha,
                            origin=EvidenceOrigin.ENCLOSING_SCOPE,
                            language="python",
                            symbols=overlapping,
                        )
                    )

        # 6. Discover same-file sibling definitions directly invoked within enclosing scopes
        sibling_snippets: list[CodeSnippet] = []
        try:
            parsed_tree = ast.parse(source_code, filename=changed_file.filename)
        except Exception:
            parsed_tree = None

        if parsed_tree is not None:
            constructs_by_name: dict[str, list[SymbolReference]] = {}
            for c in code_constructs:
                constructs_by_name.setdefault(c.name, []).append(c)

            for snip in list(snippets):
                if snip.origin != EvidenceOrigin.ENCLOSING_SCOPE:
                    continue

                enclosing_sym = snip.symbols[0] if snip.symbols else None
                enclosing_parent = enclosing_sym.parent_scope if enclosing_sym else None

                calls_in_scope: list[ast.Call] = []
                for node in ast.walk(parsed_tree):
                    if isinstance(node, ast.Call):
                        node_line = getattr(node, "lineno", None)
                        if node_line is not None and snip.start_line <= node_line <= snip.end_line:
                            calls_in_scope.append(node)

                calls_in_scope.sort(
                    key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0))
                )

                for call in calls_in_scope:
                    if len(sibling_snippets) >= MAX_SIBLING_SNIPPETS_PER_FILE:
                        break

                    target_sym: SymbolReference | None = None

                    # Bare function call: func(...)
                    # Must ONLY resolve to a top-level function or class (parent_scope is None).
                    # Never associate bare calls with methods inside a class.
                    if isinstance(call.func, ast.Name):
                        bare_name = call.func.id
                        candidates = [
                            s
                            for s in constructs_by_name.get(bare_name, [])
                            if s.parent_scope is None
                            and s.symbol_type in (SymbolType.FUNCTION, SymbolType.CLASS)
                        ]
                        if len(candidates) == 1:
                            target_sym = candidates[0]

                    # Self/cls method call: self.method(...) or cls.method(...)
                    # Must ONLY resolve to a method within the SAME class.
                    elif isinstance(call.func, ast.Attribute):
                        recv = call.func.value
                        if (
                            isinstance(recv, ast.Name)
                            and recv.id in ("self", "cls")
                            and enclosing_parent is not None
                        ):
                            attr_name = call.func.attr
                            candidates = [
                                s
                                for s in constructs_by_name.get(attr_name, [])
                                if s.parent_scope == enclosing_parent
                                and s.symbol_type == SymbolType.METHOD
                            ]
                            if len(candidates) == 1:
                                target_sym = candidates[0]

                    if target_sym is not None:
                        target_range = (target_sym.start_line, target_sym.end_line)
                        if target_range not in seen_ranges:
                            seen_ranges.add(target_range)
                            s_idx = max(0, target_sym.start_line - 1)
                            e_idx = min(len(lines), target_sym.end_line)
                            sibling_snippets.append(
                                CodeSnippet(
                                    file_path=changed_file.filename,
                                    start_line=target_sym.start_line,
                                    end_line=max(target_sym.start_line, e_idx),
                                    content="".join(lines[s_idx:e_idx]),
                                    commit_sha=commit_sha,
                                    origin=EvidenceOrigin.CALL_HIERARCHY,
                                    language="python",
                                    symbols=[target_sym],
                                )
                            )

            snippets.extend(sibling_snippets)

        return snippets, limitations

    def discover_related_code(
        self,
        modified_symbols: list[SymbolReference],
        candidate_files: dict[str, str],
        commit_sha: str,
    ) -> tuple[list[CodeSnippet], list[InferredRelationship], list[RetrievalLimitation]]:
        """Conservative related-code discovery scanning candidate files at immutable commit SHA.

        Strictly distinguishes confirmed AST imports from unconfirmed textual symbol matches.

        Args:
            modified_symbols: Code symbols modified in the PR diff hunks.
            candidate_files: Mapping of {file_path: raw_content} within the repository.
            commit_sha: The exact commit SHA evaluated.

        Returns:
            Tuple of (
                related CodeSnippets,
                InferredRelationships with explicit provenance,
                limitations,
            ).
        """
        snippets: list[CodeSnippet] = []
        relationships: list[InferredRelationship] = []
        limitations: list[RetrievalLimitation] = []

        if not modified_symbols or not candidate_files:
            return snippets, relationships, limitations

        symbol_names = {s.name for s in modified_symbols if s.name}

        # Apply candidate file count limit
        candidate_items = list(candidate_files.items())
        if len(candidate_items) > self._max_candidate_files:
            limitations.append(
                RetrievalLimitation(
                    limitation_type=LimitationType.BUDGET_EXCEEDED,
                    message=(
                        f"Candidate file scan limited to {self._max_candidate_files} files "
                        f"(total candidates: {len(candidate_items)})."
                    ),
                    suggested_mitigation=(
                        "Increase max_candidate_files limit if broader scan is needed."
                    ),
                )
            )
            candidate_items = candidate_items[: self._max_candidate_files]

        for file_path, content in candidate_items:
            # Check file size limit
            content_bytes = len(content.encode("utf-8"))
            if content_bytes > self._max_file_size_bytes:
                limitations.append(
                    RetrievalLimitation(
                        limitation_type=LimitationType.BUDGET_EXCEEDED,
                        target_path=file_path,
                        message=(
                            f"File {file_path} ({content_bytes} bytes) exceeds size limit "
                            f"({self._max_file_size_bytes} bytes); skipped from related scan."
                        ),
                    )
                )
                continue

            lines = content.splitlines(keepends=True)

            if file_path.endswith(".py"):
                try:
                    analyzer = PythonASTAnalyzer(file_path=file_path)
                    analyzer.visit(__import__("ast").parse(content, filename=file_path))
                except SyntaxParseError:
                    continue
                except Exception:
                    continue

                # 1. Confirmed AST Import Detection
                ast_matched_names: set[str] = set()
                imported_names = set(analyzer.imported_modules)
                for s in analyzer.symbols:
                    if s.symbol_type == SymbolType.IMPORT:
                        imported_names.add(s.name)

                for sym in modified_symbols:
                    mod_path = sym.file_path.replace("/", ".").replace("\\", ".")
                    if mod_path.endswith(".py"):
                        mod_path = mod_path[:-3]

                    is_imported = any(
                        imp == mod_path
                        or imp.startswith(mod_path + ".")
                        or imp.endswith("." + sym.name)
                        or (sym.parent_scope and imp.endswith("." + sym.parent_scope))
                        or imp == sym.name
                        or (sym.parent_scope and imp == sym.parent_scope)
                        for imp in imported_names
                    )

                    if is_imported:
                        ast_matched_names.add(sym.name)
                        rel_type = "tests" if "test" in file_path.lower() else "imports"
                        relationships.append(
                            InferredRelationship(
                                source_symbol=file_path,
                                target_symbol=sym.name,
                                relationship_type=rel_type,
                                confidence=1.0,  # 100% confirmed by AST grammar
                                heuristic_used="ast_confirmed_import",
                            )
                        )

                if ast_matched_names:
                    snippets.append(
                        CodeSnippet(
                            file_path=file_path,
                            start_line=1,
                            end_line=len(lines),
                            content=content,
                            commit_sha=commit_sha,
                            origin=EvidenceOrigin.TEST_FILE
                            if "test" in file_path.lower()
                            else EvidenceOrigin.DIRECT_DEPENDENCY,
                            language="python",
                            symbols=analyzer.symbols,
                        )
                    )
                    continue

            # 2. Heuristic Unconfirmed Textual Matches (for remaining files or non-Python)
            # Scan for occurrences of symbol names in content
            for sym_name in symbol_names:
                if sym_name in content:
                    relationships.append(
                        InferredRelationship(
                            source_symbol=file_path,
                            target_symbol=sym_name,
                            relationship_type="possible_text_reference",
                            confidence=0.35,  # Strictly unconfirmed heuristic estimate
                            heuristic_used="textual_name_match_unconfirmed",
                        )
                    )
                    snippets.append(
                        CodeSnippet(
                            file_path=file_path,
                            start_line=1,
                            end_line=min(len(lines), 50),  # Bounded preview snippet
                            content="".join(lines[:50]),
                            commit_sha=commit_sha,
                            origin=EvidenceOrigin.REPOSITORY_REFERENCE,
                            language=file_path.rsplit(".", 1)[-1] if "." in file_path else "text",
                            symbols=[],
                        )
                    )
                    break  # One snippet per file

        return snippets, relationships, limitations

    def locate_pr_code(
        self,
        pr: PullRequest,
        candidate_file_contents: dict[str, str] | None = None,
    ) -> LocatedCodeResult:
        """Fetches PR file contents via GitHubClient and locates all affected symbols.

        Args:
            pr: PullRequest metadata containing head_sha and changed_files list.
            candidate_file_contents: Optional pre-fetched candidate repository files.

        Returns:
            LocatedCodeResult with snippets, relationships, and explicit limitations.
        """
        all_snippets: list[CodeSnippet] = []
        all_relationships: list[InferredRelationship] = []
        all_limitations: list[RetrievalLimitation] = []
        all_modified_symbols: list[SymbolReference] = []

        files_to_process = pr.changed_files
        if not files_to_process and self._github_client:
            try:
                files_to_process = self._github_client.get_pull_request_files(
                    pr.owner, pr.repo, pr.number
                )
            except GitHubAPIError as exc:
                all_limitations.append(
                    RetrievalLimitation(
                        limitation_type=LimitationType.API_ERROR,
                        message=f"Failed to fetch changed files for PR #{pr.number}: {exc}",
                    )
                )
                return LocatedCodeResult(limitations=all_limitations)

        for changed_file in files_to_process:
            content: str | None = None
            is_deleted = changed_file.status == FileChangeStatus.REMOVED
            target_sha = pr.base_sha if is_deleted else pr.head_sha

            # Fetch file content at target_sha (base_sha for deleted files, head_sha for others)
            if self._github_client and not changed_file.is_binary:
                try:
                    content = self._github_client.get_file_content(
                        pr.owner, pr.repo, changed_file.filename, target_sha
                    )
                except GitHubAPIError as exc:
                    all_limitations.append(
                        RetrievalLimitation(
                            limitation_type=LimitationType.FILE_NOT_FOUND,
                            target_path=changed_file.filename,
                            message=(
                                f"Could not retrieve content for {changed_file.filename} "
                                f"at {target_sha}: {exc}"
                            ),
                            suggested_mitigation=(
                                "Inspect base commit SHA if deleted code context is needed."
                                if is_deleted
                                else None
                            ),
                        )
                    )

            snippets, limitations = self.locate_enclosing_symbols(
                changed_file=changed_file,
                source_code=content,
                commit_sha=target_sha,
            )
            all_snippets.extend(snippets)
            all_limitations.extend(limitations)

            for snip in snippets:
                all_modified_symbols.extend(snip.symbols)

        # Related-code discovery
        if candidate_file_contents:
            rel_snippets, rel_relationships, rel_limitations = self.discover_related_code(
                modified_symbols=all_modified_symbols,
                candidate_files=candidate_file_contents,
                commit_sha=pr.head_sha,
            )
            all_snippets.extend(rel_snippets)
            all_relationships.extend(rel_relationships)
            all_limitations.extend(rel_limitations)

        return LocatedCodeResult(
            snippets=all_snippets,
            relationships=all_relationships,
            limitations=all_limitations,
        )
