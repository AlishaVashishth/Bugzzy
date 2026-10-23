"""Evidence-grounded AST and diff defect analyzer for Bug Localization Agent.

Performs conservative, deterministic static checks strictly grounded in the
supplied ContextPackage evidence without executing repository code or making
external network calls.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from pr_context_builder.localization.models import (
    BugSeverity,
    ConfidenceLevel,
    EvidenceReference,
    SuspectedBugLocation,
)
from pr_context_builder.models.context_package import ContextPackage
from pr_context_builder.models.pr_metadata import ChangedFile, FileChangeStatus
from pr_context_builder.models.repository import CodeSnippet, EvidenceOrigin


@dataclass(frozen=True)
class FunctionSignature:
    """Extracted signature metadata for a function or method."""

    name: str
    file_path: str
    start_line: int
    end_line: int
    min_args: int
    max_args: int | None  # None indicates *args is present
    required_params: tuple[str, ...]
    all_allowed_kwargs: frozenset[str] | None  # None indicates **kwargs is present
    kwonly_required: frozenset[str]
    commit_sha: str
    origin: EvidenceOrigin
    content_excerpt: str
    parent_scope: str | None = None  # Enclosing class name if method
    posonly_param_names: frozenset[str] = frozenset()
    posonly_required_count: int = 0


def _module_matches_path(module_name: str, file_path: str) -> bool:
    """Checks whether a Python dotted module path corresponds to a file path."""
    if not module_name or not file_path:
        return False
    clean_path = file_path.replace("\\", "/")
    if clean_path.endswith(".py"):
        clean_path = clean_path[:-3]
    path_dots = clean_path.replace("/", ".")

    if module_name == path_dots:
        return True
    if path_dots.endswith("." + module_name):
        return True
    mod_as_path = module_name.replace(".", "/")
    if clean_path.endswith(mod_as_path):
        return True
    file_stem = clean_path.rsplit("/", 1)[-1]
    return module_name == file_stem or module_name.endswith("." + file_stem)


def _is_line_in_diff(
    file_path: str,
    start_line: int,
    end_line: int,
    changed_files: list[ChangedFile],
) -> bool:
    """Returns True if [start_line, end_line] overlaps with added or modified lines in diff hunks."""
    target_cf = next((cf for cf in changed_files if cf.filename == file_path), None)
    if not target_cf or not target_cf.hunks:
        return False

    modified_lines = target_cf.get_modified_head_lines()
    if not modified_lines:
        return False

    return any(line in modified_lines for line in range(start_line, end_line + 1))


def _extract_function_signatures(
    code: str,
    file_path: str,
    snippet_start_line: int,
    commit_sha: str,
    origin: EvidenceOrigin,
) -> list[FunctionSignature]:
    """Extracts function and method signatures from source code using AST parsing."""
    try:
        tree = ast.parse(code, filename=file_path)
    except SyntaxError:
        return []

    signatures: list[FunctionSignature] = []
    lines = code.splitlines()

    class SignatureVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._class_stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._class_stack.append(node.name)
            self.generic_visit(node)
            self._class_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._handle_function(node)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._handle_function(node)
            self.generic_visit(node)

        def _handle_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            is_method = bool(self._class_stack)
            parent_scope = self._class_stack[-1] if is_method else None

            posonly = list(getattr(node.args, "posonlyargs", []))
            args = list(node.args.args)

            # Drop self / cls for method signatures
            if is_method:
                if posonly and posonly[0].arg in ("self", "cls"):
                    posonly = posonly[1:]
                elif not posonly and args and args[0].arg in ("self", "cls"):
                    args = args[1:]

            all_pos = posonly + args
            total_pos = len(all_pos)
            defaults = node.args.defaults
            num_defaults = len(defaults)
            min_args = max(0, total_pos - num_defaults)

            has_vararg = node.args.vararg is not None
            has_kwarg = node.args.kwarg is not None

            required_params = tuple(a.arg for a in all_pos[:min_args])
            posonly_param_names = frozenset(a.arg for a in posonly)
            posonly_req_count = min(len(posonly), min_args)

            kwonly = node.args.kwonlyargs
            kw_defaults = node.args.kw_defaults
            kwonly_req = frozenset(
                arg.arg
                for arg, default in zip(kwonly, kw_defaults, strict=False)
                if default is None
            )

            # Positional-only parameters cannot be passed as keyword arguments
            all_kw = (
                None
                if has_kwarg
                else frozenset(a.arg for a in args) | frozenset(a.arg for a in kwonly)
            )

            abs_start = snippet_start_line + node.lineno - 1
            node_end = node.end_lineno or node.lineno
            abs_end = snippet_start_line + node_end - 1

            excerpt_line_idx = node.lineno - 1
            if excerpt_line_idx < len(lines):
                excerpt = lines[excerpt_line_idx].strip()
            else:
                excerpt = node.name

            signatures.append(
                FunctionSignature(
                    name=node.name,
                    file_path=file_path,
                    start_line=abs_start,
                    end_line=abs_end,
                    min_args=min_args,
                    max_args=None if has_vararg else total_pos,
                    required_params=required_params,
                    all_allowed_kwargs=all_kw,
                    kwonly_required=kwonly_req,
                    commit_sha=commit_sha,
                    origin=origin,
                    content_excerpt=excerpt,
                    parent_scope=parent_scope,
                    posonly_param_names=posonly_param_names,
                    posonly_required_count=posonly_req_count,
                )
            )

    visitor = SignatureVisitor()
    visitor.visit(tree)
    return signatures


class _CallerScopeContext:
    """Lightweight caller-file AST context for resolving call targets deterministically."""

    def __init__(self, tree: ast.AST, file_path: str) -> None:
        self.file_path = file_path
        # Map: local_name -> (module_name, original_name)
        self.imported_symbols: dict[str, tuple[str, str]] = {}
        # Map: local_alias -> module_path
        self.imported_modules: dict[str, str] = {}
        # Map: var_name -> class_name
        self.var_classes: dict[str, str] = {}
        # Function/Class definitions in this file
        self.local_definitions: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for alias in node.names:
                    local = alias.asname or alias.name
                    self.imported_symbols[local] = (mod, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name
                    self.imported_modules[local] = alias.name
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.local_definitions.add(node.name)
            elif isinstance(node, ast.Assign):
                if isinstance(node.value, ast.Call):
                    cls_name: str | None = None
                    if isinstance(node.value.func, ast.Name):
                        cls_name = node.value.func.id
                    elif isinstance(node.value.func, ast.Attribute):
                        cls_name = node.value.func.attr
                    if cls_name:
                        for tgt in node.targets:
                            if isinstance(tgt, ast.Name):
                                self.var_classes[tgt.id] = cls_name


def _can_resolve_call_target(
    node: ast.Call,
    sig: FunctionSignature,
    caller_ctx: _CallerScopeContext,
    caller_file_path: str,
    caller_origin: EvidenceOrigin,
    all_changed_sigs: list[FunctionSignature],
) -> bool:
    """Verifies whether the Call node deterministically targets the given signature."""
    # 1. Bare function call: func_name(...)
    if isinstance(node.func, ast.Name):
        func_name = node.func.id
        if func_name != sig.name:
            return False

        # Bare call cannot invoke a class instance method without self/cls
        if sig.parent_scope is not None:
            return False

        # Caller is in the same file as definition
        if caller_file_path == sig.file_path:
            return True

        # Caller explicitly imported symbol
        if func_name in caller_ctx.imported_symbols:
            imp_mod, orig_name = caller_ctx.imported_symbols[func_name]
            return orig_name == sig.name and _module_matches_path(imp_mod, sig.file_path)

        # If caller defines this symbol locally in another file, it shadows external func
        if func_name in caller_ctx.local_definitions:
            return False

        # If caller imported any other symbol with this name from a different module, reject
        matching_sigs = [
            s for s in all_changed_sigs if s.name == func_name and s.parent_scope is None
        ]
        if len(matching_sigs) > 1:
            # Ambiguous: multiple changed functions share the same name across modules
            return False

        # Visible snippet retrieved as direct dependency/test file without local name collisions
        return caller_origin in (
            EvidenceOrigin.DIRECT_DEPENDENCY,
            EvidenceOrigin.CALL_HIERARCHY,
            EvidenceOrigin.TEST_FILE,
        )

    # 2. Attribute / method call: receiver.attr_name(...)
    if isinstance(node.func, ast.Attribute):
        attr_name = node.func.attr
        if attr_name != sig.name:
            return False

        receiver = node.func.value

        # Qualified module call: module.func_name(...)
        if isinstance(receiver, ast.Name) and sig.parent_scope is None:
            recv_name = receiver.id
            if recv_name in caller_ctx.imported_modules:
                mod_target = caller_ctx.imported_modules[recv_name]
                return _module_matches_path(mod_target, sig.file_path)
            # Direct module name used without alias (e.g. math.calculate)
            return _module_matches_path(recv_name, sig.file_path)

        # Class method call: obj.method_name(...)
        if sig.parent_scope is not None:
            if isinstance(receiver, ast.Name):
                recv_name = receiver.id

                # Direct class call: ClassName.method(instance, ...)
                if recv_name == sig.parent_scope:
                    return True

                # Known variable instantiated with class
                if recv_name in caller_ctx.var_classes:
                    return caller_ctx.var_classes[recv_name] == sig.parent_scope

                # self / cls call within the same class
                if recv_name in ("self", "cls") and caller_file_path == sig.file_path:
                    return True

            # If receiver cannot be resolved to the target class, do not speculate
            return False

    return False


def _find_conflicting_calls(
    caller_snippet: CodeSnippet,
    signatures: list[FunctionSignature],
) -> list[tuple[FunctionSignature, ast.Call, str, str]]:
    """Identifies call sites in caller_snippet that demonstrably conflict with signatures.

    Returns:
        List of tuples: (signature, call_node, failure_reason, detailed_explanation)
    """
    try:
        tree = ast.parse(caller_snippet.content, filename=caller_snippet.file_path)
    except SyntaxError:
        return []

    caller_ctx = _CallerScopeContext(tree, caller_snippet.file_path)
    conflicts: list[tuple[FunctionSignature, ast.Call, str, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        for sig in signatures:
            if not _can_resolve_call_target(
                node=node,
                sig=sig,
                caller_ctx=caller_ctx,
                caller_file_path=caller_snippet.file_path,
                caller_origin=caller_snippet.origin,
                all_changed_sigs=signatures,
            ):
                continue

            # If call passes *args or **kwargs, dynamic arguments prevent static certainty
            if any(isinstance(a, ast.Starred) for a in node.args):
                continue
            if any(kw.arg is None for kw in node.keywords):
                continue

            pos_count = len(node.args)
            kw_names = {kw.arg for kw in node.keywords if kw.arg is not None}

            # 1. Positional-only parameter passed as keyword argument
            posonly_as_kw = sorted(kw_names & sig.posonly_param_names)
            if posonly_as_kw:
                reason = (
                    f"got positional-only argument(s) passed as keyword argument(s): "
                    f"{', '.join(repr(p) for p in posonly_as_kw)}"
                )
                detail = (
                    f"Call to '{sig.name}' provides positional-only parameter(s) "
                    f"{posonly_as_kw} as keyword argument(s)."
                )
                conflicts.append((sig, node, reason, detail))
                continue

            # 2. Missing required positional-only parameters
            if pos_count < sig.posonly_required_count:
                missing_posonly = [
                    sig.required_params[i] for i in range(pos_count, sig.posonly_required_count)
                ]
                reason = (
                    f"missing required positional argument(s): "
                    f"{', '.join(repr(p) for p in missing_posonly)}"
                )
                detail = (
                    f"Call to '{sig.name}' provides {pos_count} positional argument(s), "
                    f"but definition requires at least {sig.posonly_required_count} "
                    f"positional-only argument(s) {missing_posonly}."
                )
                conflicts.append((sig, node, reason, detail))
                continue

            # 3. Missing required positional-or-keyword parameters
            posonly_len = len(sig.posonly_param_names)
            missing_pos_or_kw: list[str] = []
            for idx in range(posonly_len, sig.min_args):
                param = sig.required_params[idx]
                if pos_count <= idx and param not in kw_names:
                    missing_pos_or_kw.append(param)

            if missing_pos_or_kw:
                reason = (
                    f"missing required argument(s): {', '.join(repr(p) for p in missing_pos_or_kw)}"
                )
                detail = (
                    f"Call to '{sig.name}' provides {pos_count} positional argument(s), "
                    f"but definition requires parameter(s) {missing_pos_or_kw}."
                )
                conflicts.append((sig, node, reason, detail))
                continue

            # 4. Too many positional arguments
            if sig.max_args is not None and pos_count > sig.max_args:
                reason = f"takes {sig.max_args} positional argument(s) but {pos_count} were given"
                detail = (
                    f"Call to '{sig.name}' provides {pos_count} positional argument(s), "
                    f"exceeding the maximum allowed ({sig.max_args})."
                )
                conflicts.append((sig, node, reason, detail))
                continue

            # 5. Unexpected keyword argument
            if sig.all_allowed_kwargs is not None:
                unexpected = sorted(kw_names - sig.all_allowed_kwargs)
                if unexpected:
                    reason = f"got unexpected keyword argument {repr(unexpected[0])}"
                    detail = (
                        f"Call to '{sig.name}' specifies keyword argument '{unexpected[0]}', "
                        f"which is not accepted by definition."
                    )
                    conflicts.append((sig, node, reason, detail))
                    continue

            # 6. Missing required keyword-only argument
            missing_kwonly = sorted(sig.kwonly_required - kw_names)
            if missing_kwonly:
                reason = (
                    f"missing required keyword-only argument(s): "
                    f"{', '.join(repr(p) for p in missing_kwonly)}"
                )
                detail = (
                    f"Call to '{sig.name}' does not provide required keyword-only "
                    f"argument(s) {missing_kwonly}."
                )
                conflicts.append((sig, node, reason, detail))
                continue

    return conflicts


def _check_removed_symbol_references(
    context: ContextPackage,
) -> list[SuspectedBugLocation]:
    """Identifies active code references to symbols demonstrably deleted in the PR diff."""
    findings: list[SuspectedBugLocation] = []

    # 1. Collect symbols that are added or still present in head snippets
    added_symbols_by_file: dict[str, set[str]] = {}
    for cf in context.changed_files:
        for hunk in cf.hunks:
            for line in hunk.lines:
                if line.startswith("+") and not line.startswith("+++"):
                    stripped = line[1:].strip()
                    if stripped.startswith("def ") and "(" in stripped:
                        fn_name = stripped[4 : stripped.find("(")].strip()
                        if fn_name and not fn_name.startswith("__"):
                            added_symbols_by_file.setdefault(cf.filename, set()).add(fn_name)
                    elif stripped.startswith("class ") and (":" in stripped or "(" in stripped):
                        end_pos = min(
                            [p for p in (stripped.find("("), stripped.find(":")) if p != -1]
                        )
                        cls_name = stripped[6:end_pos].strip()
                        if cls_name:
                            added_symbols_by_file.setdefault(cf.filename, set()).add(cls_name)

    head_symbols_by_file: dict[str, set[str]] = {}
    for snip in context.evidence_snippets + context.test_snippets:
        if snip.commit_sha == context.head_sha and snip.language == "python":
            try:
                tree = ast.parse(snip.content, filename=snip.file_path)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        head_symbols_by_file.setdefault(snip.file_path, set()).add(node.name)
            except SyntaxError:
                pass

    # 2. Identify candidate deletions: (symbol_name, file_path, start_line, end_line, excerpt)
    deleted_symbols: list[tuple[str, str, int, int, str]] = []

    for cf in context.changed_files:
        if cf.status == FileChangeStatus.REMOVED:
            mod_name = cf.filename.rsplit("/", 1)[-1].replace(".py", "")
            deleted_symbols.append((mod_name, cf.filename, 1, 1, f"Deleted file {cf.filename}"))

        for hunk in cf.hunks:
            for line in hunk.lines:
                if line.startswith("-") and not line.startswith("---"):
                    stripped = line[1:].strip()
                    if stripped.startswith("def ") and "(" in stripped:
                        fn_name = stripped[4 : stripped.find("(")].strip()
                        if fn_name and not fn_name.startswith("__"):
                            # If re-added or still present in head, it is edited, not deleted
                            if fn_name in added_symbols_by_file.get(cf.filename, set()):
                                continue
                            if fn_name in head_symbols_by_file.get(cf.filename, set()):
                                continue
                            deleted_symbols.append(
                                (
                                    fn_name,
                                    cf.filename,
                                    hunk.old_start,
                                    hunk.old_start + max(hunk.old_lines - 1, 0),
                                    line,
                                )
                            )
                    elif stripped.startswith("class ") and (":" in stripped or "(" in stripped):
                        end_pos = min(
                            [p for p in (stripped.find("("), stripped.find(":")) if p != -1]
                        )
                        cls_name = stripped[6:end_pos].strip()
                        if cls_name:
                            if cls_name in added_symbols_by_file.get(cf.filename, set()):
                                continue
                            if cls_name in head_symbols_by_file.get(cf.filename, set()):
                                continue
                            deleted_symbols.append(
                                (
                                    cls_name,
                                    cf.filename,
                                    hunk.old_start,
                                    hunk.old_start + max(hunk.old_lines - 1, 0),
                                    line,
                                )
                            )

    if not deleted_symbols:
        return findings

    all_active_snippets = context.evidence_snippets + context.test_snippets
    finding_idx = 1

    for snippet in all_active_snippets:
        if not snippet.file_path.endswith(".py"):
            continue
        try:
            tree = ast.parse(snippet.content, filename=snippet.file_path)
        except SyntaxError:
            continue

        lines = snippet.content.splitlines()

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    for del_name, del_file, del_start, del_end, del_excerpt in deleted_symbols:
                        if alias.name != del_name:
                            continue
                        # Verify that the import's module matches the deleted file's path
                        if node.module and not _module_matches_path(node.module, del_file):
                            continue
                        if snippet.file_path == del_file:
                            continue

                        call_line = snippet.start_line + node.lineno - 1
                        call_end = snippet.start_line + (node.end_lineno or node.lineno) - 1
                        excerpt_idx = node.lineno - 1
                        caller_excerpt = (
                            lines[excerpt_idx].strip() if excerpt_idx < len(lines) else alias.name
                        )

                        evidence_caller = EvidenceReference(
                            file_path=snippet.file_path,
                            start_line=call_line,
                            end_line=call_end,
                            commit_sha=snippet.commit_sha,
                            origin=snippet.origin,
                            content_excerpt=caller_excerpt,
                        )
                        evidence_deletion = EvidenceReference(
                            file_path=del_file,
                            start_line=max(1, del_start),
                            end_line=max(del_start, del_end),
                            commit_sha=context.base_sha,
                            origin=EvidenceOrigin.DIRECT_DIFF,
                            content_excerpt=del_excerpt.strip(),
                        )

                        is_in_changed = _is_line_in_diff(
                            snippet.file_path, call_line, call_end, context.changed_files
                        )

                        findings.append(
                            SuspectedBugLocation(
                                finding_id=f"BUG-{finding_idx:03d}",
                                file_path=snippet.file_path,
                                start_line=call_line,
                                end_line=call_end,
                                commit_sha=snippet.commit_sha,
                                is_in_changed_code=is_in_changed,
                                summary=f"Reference to deleted symbol '{alias.name}'",
                                detailed_explanation=(
                                    f"Symbol '{alias.name}' was removed in {del_file} at "
                                    f"base commit {context.base_sha[:8]}, but is still imported in "
                                    f"{snippet.file_path}:{call_line}."
                                ),
                                potential_impact=(
                                    f"ImportError or AttributeError when {snippet.file_path} "
                                    "is imported."
                                ),
                                severity=BugSeverity.HIGH,
                                severity_justification=(
                                    "Importing or accessing a deleted symbol causes an unhandled "
                                    "ImportError or AttributeError."
                                ),
                                confidence=ConfidenceLevel.STRONG,
                                confidence_explanation=(
                                    "Directly corroborated by deletion diff in base commit and "
                                    "referencing import statement in head commit."
                                ),
                                supporting_evidence=[evidence_caller, evidence_deletion],
                                related_test_references=[],
                                suggested_verification=[
                                    f"Remove reference to '{alias.name}' in {snippet.file_path} "
                                    f"or restore required symbol in {del_file}."
                                ],
                            )
                        )
                        finding_idx += 1

    return findings


def validate_finding_grounding(
    finding: SuspectedBugLocation,
    context: ContextPackage,
) -> bool:
    """Validates that all file paths, line ranges, and SHAs in a finding are grounded in context."""
    valid_paths = {f.filename for f in context.changed_files} | {
        s.file_path for s in context.evidence_snippets + context.test_snippets
    }
    if finding.file_path not in valid_paths:
        return False

    valid_shas = {context.head_sha, context.base_sha}
    if finding.commit_sha not in valid_shas:
        return False

    if finding.start_line < 1 or finding.end_line < finding.start_line:
        return False

    if not finding.supporting_evidence:
        return False

    for ref in finding.supporting_evidence:
        if ref.file_path not in valid_paths:
            return False
        if ref.commit_sha not in valid_shas:
            return False
        if ref.start_line < 1 or ref.end_line < ref.start_line:
            return False
        if not ref.content_excerpt.strip():
            return False

    return True


def analyze_code_defects(context: ContextPackage) -> list[SuspectedBugLocation]:
    """Performs conservative, evidence-grounded defect analysis across ContextPackage."""
    findings: list[SuspectedBugLocation] = []

    # 1. Collect function signatures modified in the PR (preserving all scopes and modules)
    changed_filenames = {f.filename for f in context.changed_files if f.filename.endswith(".py")}
    changed_signatures: list[FunctionSignature] = []

    for snip in context.evidence_snippets:
        if snip.file_path in changed_filenames and snip.language == "python":
            sigs = _extract_function_signatures(
                code=snip.content,
                file_path=snip.file_path,
                snippet_start_line=snip.start_line,
                commit_sha=snip.commit_sha,
                origin=snip.origin,
            )
            changed_signatures.extend(sigs)

    # 2. Check for signature conflicts against visible calling snippets
    all_calling_snippets = context.evidence_snippets + context.test_snippets
    finding_idx = 1

    for caller_snip in all_calling_snippets:
        if caller_snip.language != "python" or not caller_snip.file_path.endswith(".py"):
            continue

        conflicts = _find_conflicting_calls(caller_snip, changed_signatures)
        caller_lines = caller_snip.content.splitlines()

        for sig, call_node, reason, detail in conflicts:
            call_start = caller_snip.start_line + call_node.lineno - 1
            call_end = caller_snip.start_line + (call_node.end_lineno or call_node.lineno) - 1

            excerpt_idx = call_node.lineno - 1
            call_excerpt = (
                caller_lines[excerpt_idx].strip() if excerpt_idx < len(caller_lines) else sig.name
            )

            evidence_call = EvidenceReference(
                file_path=caller_snip.file_path,
                start_line=call_start,
                end_line=call_end,
                commit_sha=caller_snip.commit_sha,
                origin=caller_snip.origin,
                content_excerpt=call_excerpt,
            )

            evidence_def = EvidenceReference(
                file_path=sig.file_path,
                start_line=sig.start_line,
                end_line=sig.end_line,
                commit_sha=sig.commit_sha,
                origin=sig.origin,
                content_excerpt=sig.content_excerpt,
            )

            is_in_changed = _is_line_in_diff(
                caller_snip.file_path, call_start, call_end, context.changed_files
            )
            test_refs = [s.file_path for s in context.test_snippets if sig.name in s.content]

            finding = SuspectedBugLocation(
                finding_id=f"BUG-{finding_idx:03d}",
                file_path=caller_snip.file_path,
                start_line=call_start,
                end_line=call_end,
                commit_sha=caller_snip.commit_sha,
                is_in_changed_code=is_in_changed,
                summary=f"Incompatible call to '{sig.name}': {reason}",
                detailed_explanation=detail,
                potential_impact=f"TypeError at runtime when '{sig.name}' is invoked.",
                severity=BugSeverity.HIGH,
                severity_justification=(
                    f"Passing incompatible arguments to '{sig.name}' raises a "
                    "deterministic TypeError."
                ),
                confidence=ConfidenceLevel.STRONG,
                confidence_explanation=(
                    "Both the updated signature and the conflicting call site are corroborated "
                    "by static AST evidence in visible context snippets."
                ),
                supporting_evidence=[evidence_call, evidence_def],
                related_test_references=test_refs,
                suggested_verification=[
                    (
                        f"Update call to '{sig.name}' in {caller_snip.file_path} "
                        "to supply expected arguments."
                    ),
                    f"Execute test suite exercising '{sig.name}' to verify runtime compatibility.",
                ],
            )

            if validate_finding_grounding(finding, context):
                findings.append(finding)
                finding_idx += 1

    # 3. Check for references to removed/deleted symbols
    removed_findings = _check_removed_symbol_references(context)
    for rf in removed_findings:
        if validate_finding_grounding(rf, context):
            rf.finding_id = f"BUG-{finding_idx:03d}"
            findings.append(rf)
            finding_idx += 1

    return findings
