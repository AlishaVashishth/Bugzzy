"""Groq-powered hosted AI analysis for Bug Localization Agent.

Leverages Groq Chat Completions API with structured JSON output to identify
potential defect candidates while strictly grounding suggestions in supplied context.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pr_context_builder.exceptions import ModelDependencyMissingError
from pr_context_builder.localization.analyzer import _is_line_in_diff
from pr_context_builder.localization.models import (
    AISuggestedBugLocation,
    EvidenceReference,
)
from pr_context_builder.models.context_package import ContextPackage
from pr_context_builder.models.repository import CodeSnippet

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an automated PR bug localization assistant.
Analyze the supplied changed code, diffs, and context snippets to identify
potential subtle defects, regressions, or integration issues.

Rules:
1. Ground every suggestion strictly in the supplied code and diffs.
2. Only reference file paths and line numbers that appear in the context.
3. Pinpoint the EXACT defective statement line(s), NOT the enclosing function or class declaration. For example, if a defective call is inside a function, report the line number of the call statement itself, not the 'def' line.
4. Distinguish added or modified lines in the diff from unchanged context lines.
5. Do NOT invent files, line numbers, commit SHAs, or test names.
6. Output ONLY valid JSON adhering to the following schema:
{
  "suggestions": [
    {
      "file_path": "path/to/file.py",
      "start_line": 10,
      "end_line": 20,
      "summary": "Concise summary of concern",
      "detailed_explanation": "Explanation linking evidence to potential defect",
      "potential_impact": "Anticipated runtime consequence",
      "confidence_explanation": "Uncertainties, missing context, or assumptions",
      "related_test_references": ["tests/test_foo.py"],
      "suggested_verification": ["Run test_foo to verify"]
    }
  ]
}
If no potential bugs or concerns are detected, return {"suggestions": []}.
"""


STOP_WORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are",
    "aren't", "as", "at", "be", "because", "been", "before", "being", "below", "between", "both",
    "but", "by", "can", "can't", "cannot", "could", "couldn't", "did", "didn't", "do", "does",
    "doesn't", "doing", "don't", "down", "during", "each", "few", "for", "from", "further", "had",
    "hadn't", "has", "hasn't", "have", "haven't", "having", "he", "he'd", "he'll", "he's", "her",
    "here", "here's", "hers", "herself", "him", "himself", "his", "how", "how's", "i", "i'd",
    "i'll", "i'm", "i've", "if", "in", "into", "is", "isn't", "it", "it's", "its", "itself",
    "let's", "me", "more", "most", "mustn't", "my", "myself", "no", "nor", "not", "of", "off",
    "on", "once", "only", "or", "other", "ought", "our", "ours", "ourselves", "out", "over", "own",
    "same", "shan't", "she", "she'd", "she'll", "she's", "should", "shouldn't", "so", "some", "such",
    "than", "that", "that's", "the", "their", "theirs", "them", "themselves", "then", "there",
    "there's", "these", "they", "they'd", "they'll", "they're", "they've", "this", "those",
    "through", "to", "too", "under", "until", "up", "very", "was", "wasn't", "we", "we'd", "we'll",
    "we're", "we've", "were", "weren't", "what", "what's", "when", "when's", "where", "where's",
    "which", "while", "who", "who's", "whom", "why", "why's", "with", "won't", "would", "wouldn't",
    "you", "you'd", "you'll", "you're", "you've", "your", "yours", "yourself", "yourselves",
    # domain terms
    "def", "class", "function", "method", "line", "lines", "call", "calls", "calling",
    "argument", "arguments", "parameter", "parameters", "arg", "args", "param", "params",
    "missing", "required", "return", "returns", "value", "values", "expected", "got",
    "error", "issue", "bug", "defect", "potential", "runtime", "failure", "fail", "pass", "file",
}


class GroqLocalizationClient:
    """Client for Groq Chat Completions API providing AI-assisted defect suggestions."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "llama-3.3-70b-versatile",
        timeout: float = 30.0,
        max_tokens: int = 2048,
        client: Any = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_tokens = max_tokens

        if client is not None:
            self._client = client
        elif self._api_key:
            self._client = self._init_client()
        else:
            self._client = None

    @property
    def model_name(self) -> str:
        return self._model

    def _init_client(self) -> Any:
        """Lazily imports and instantiates the official Groq client."""
        try:
            from groq import Groq
        except ImportError as exc:
            raise ModelDependencyMissingError(
                f"Groq SDK is not installed: {exc}. "
                "Install it using 'pip install -r requirements-groq.txt' or 'pip install groq'."
            ) from exc

        return Groq(api_key=self._api_key, timeout=self._timeout)

    def build_prompt(self, context: ContextPackage) -> str:
        """Constructs a compact context payload tailored for AI bug localization."""
        lines: list[str] = [
            f"Pull Request #{context.pr.number}: {context.pr.title}",
            f"Base commit: {context.base_sha[:8]}, Head commit: {context.head_sha[:8]}",
        ]
        if context.pr.body:
            body_preview = context.pr.body[:500]
            lines.append(f"PR Description: {body_preview}")

        lines.append("\n--- CHANGED FILES & DIFFS ---")
        for cf in context.changed_files:
            lines.append(f"File: {cf.filename} (Status: {cf.status.value})")
            if cf.patch:
                lines.append(cf.patch[:2000])

        lines.append("\n--- CONTEXT SNIPPETS ---")
        all_snippets = context.evidence_snippets + context.test_snippets
        for idx, snip in enumerate(all_snippets[:10], start=1):
            lines.append(
                f"Snippet {idx} [{snip.file_path}:{snip.start_line}-{snip.end_line}] "
                f"(origin={snip.origin.value}):"
            )
            content_preview = snip.content[:1000]
            lines.append(content_preview)

        return "\n".join(lines)

    def analyze(
        self,
        context: ContextPackage,
    ) -> tuple[list[AISuggestedBugLocation], list[str]]:
        """Queries Groq Chat Completions API and validates returned suggestions.

        Returns:
            Tuple of (validated AISuggestedBugLocation list, list of limitation messages).
        """
        limitations: list[str] = []

        if not self._api_key and self._client is None:
            limitations.append(
                "[model_unavailable] GROQ_API_KEY is not set. Groq AI localization skipped."
            )
            return [], limitations

        try:
            if self._client is None:
                self._client = self._init_client()
        except ModelDependencyMissingError as exc:
            limitations.append(f"[model_unavailable] {exc}")
            return [], limitations
        except Exception as exc:
            limitations.append(f"[model_unavailable] Failed to initialize Groq client: {exc}")
            return [], limitations

        user_content = self.build_prompt(context)

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
                max_tokens=self._max_tokens,
                temperature=0.1,
            )
        except Exception as exc:
            logger.warning("Groq API request failed: %s", exc)
            limitations.append(f"[inference_error] Groq API call failed: {exc}")
            return [], limitations

        try:
            raw_text = response.choices[0].message.content or "{}"
            data = json.loads(raw_text)
        except (AttributeError, IndexError, json.JSONDecodeError) as exc:
            limitations.append(f"[inference_error] Invalid JSON returned by Groq model: {exc}")
            return [], limitations

        raw_suggestions = data.get("suggestions", [])
        if not isinstance(raw_suggestions, list):
            limitations.append("[inference_error] Groq response 'suggestions' field is not a list.")
            return [], limitations

        validated_suggestions: list[AISuggestedBugLocation] = []
        suggestion_idx = 1

        for raw_sugg in raw_suggestions:
            if not isinstance(raw_sugg, dict):
                continue
            sugg = self._validate_and_ground_suggestion(
                raw=raw_sugg,
                context=context,
                suggestion_id=f"AI-{suggestion_idx:03d}",
            )
            if sugg is not None:
                validated_suggestions.append(sugg)
                suggestion_idx += 1

        return validated_suggestions, limitations



    def _find_defective_statement_in_snippet(
        self,
        snippet: CodeSnippet,
        summary: str,
        explanation: str,
        modified_lines: set[int],
    ) -> int | None:
        """Finds the line number of the defective statement inside a snippet body."""
        snip_lines = snippet.content.splitlines()
        words = set(re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]+\b", f"{summary} {explanation}"))
        defect_tokens = {w for w in words if w.lower() not in STOP_WORDS and len(w) > 1}

        candidates: list[tuple[int, int]] = []  # (score, line_no)

        for idx, line in enumerate(snip_lines):
            line_no = snippet.start_line + idx
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(("def ", "async def ", "class ")):
                continue

            score = 0
            is_mod = line_no in modified_lines
            if is_mod:
                score += 100

            # Token matches
            matched_tokens = 0
            has_call_match = False
            for token in defect_tokens:
                if token in stripped:
                    matched_tokens += 1
                    # Extra weight if it's a call like token(...)
                    if f"{token}(" in stripped:
                        has_call_match = True

            # If there are no matching tokens at all from the defect description,
            # this statement does not support the reported defect
            if matched_tokens == 0:
                continue

            score = matched_tokens * 10
            if has_call_match:
                score += 50

            is_mod = line_no in modified_lines
            if is_mod:
                score += 100

            candidates.append((score, line_no))

        if not candidates:
            return None

        # Sort by score descending
        candidates.sort(key=lambda x: x[0], reverse=True)
        best_score, best_line = candidates[0]

        if best_score >= 10:
            return best_line

        return None

    def _refine_and_verify_statement_location(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
        summary: str,
        explanation: str,
        snippets: list[CodeSnippet],
        modified_lines: set[int],
    ) -> tuple[int, int] | None:
        """Verifies and refines reported line numbers to target the exact defective statement."""
        # Find snippet covering or overlapping the start_line
        covering_snip = next(
            (s for s in snippets if s.start_line <= start_line <= s.end_line),
            None,
        )
        if covering_snip is None:
            covering_snip = next(
                (s for s in snippets if max(start_line, s.start_line) <= min(end_line, s.end_line)),
                None,
            )

        if covering_snip is None:
            # No snippet available: verify against modified lines in diff
            if modified_lines and start_line in modified_lines:
                return start_line, end_line
            return None

        snip_lines = covering_snip.content.splitlines()
        offset = start_line - covering_snip.start_line
        line_at_start = snip_lines[offset].strip() if 0 <= offset < len(snip_lines) else ""

        is_declaration = line_at_start.startswith(("def ", "async def ", "class "))
        is_broad_scope = (
            (is_declaration and end_line >= covering_snip.end_line)
            or (start_line == covering_snip.start_line and end_line >= covering_snip.end_line)
        )

        # Check if declaration itself is a modified line in the diff
        if is_declaration and (start_line in modified_lines):
            combined_desc = f"{summary} {explanation}".lower()
            sig_keywords = {
                "signature",
                "parameter",
                "parameters",
                "argument",
                "arguments",
                "default",
                "param",
                "params",
                "def ",
            }
            if any(kw in combined_desc for kw in sig_keywords):
                return start_line, end_line

        # If it is a declaration line or broad function scope or an unchanged context line in a modified file:
        needs_refinement = (
            (is_declaration and start_line not in modified_lines)
            or is_broad_scope
            or (bool(modified_lines) and start_line not in modified_lines)
        )

        if needs_refinement:
            candidate = self._find_defective_statement_in_snippet(
                snippet=covering_snip,
                summary=summary,
                explanation=explanation,
                modified_lines=modified_lines,
            )
            if candidate is not None:
                return candidate, candidate

            # If start_line was actually added/modified in the diff, it is valid
            if start_line in modified_lines:
                return start_line, end_line

            # If it's an unchanged context line or ungrounded declaration:
            # Reject suggestion instead of silently accepting an incorrect line
            return None

        return start_line, end_line

    def _validate_and_ground_suggestion(
        self,
        raw: dict[str, Any],
        context: ContextPackage,
        suggestion_id: str,
    ) -> AISuggestedBugLocation | None:
        """Validates that a suggested code location is grounded in actual context."""
        file_path = raw.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            return None

        # 1. Path grounding: Must exist in changed files or context snippets
        valid_paths = {f.filename for f in context.changed_files} | {
            s.file_path for s in context.evidence_snippets + context.test_snippets
        }
        if file_path not in valid_paths:
            return None

        # 2. Line range grounding
        try:
            start_line = int(raw.get("start_line", 1))
            end_line = int(raw.get("end_line", start_line))
        except (TypeError, ValueError):
            return None

        if start_line < 1 or end_line < start_line:
            return None

        summary = str(raw.get("summary", "AI-suggested defect candidate")).strip()
        explanation = str(raw.get("detailed_explanation", "")).strip()

        target_cf = next((cf for cf in context.changed_files if cf.filename == file_path), None)
        modified_lines = target_cf.get_modified_head_lines() if target_cf else set()

        all_file_snippets = [
            s
            for s in context.evidence_snippets + context.test_snippets
            if s.file_path == file_path
        ]

        # 3. Refine & verify statement-level location
        refined = self._refine_and_verify_statement_location(
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            summary=summary,
            explanation=explanation,
            snippets=all_file_snippets,
            modified_lines=modified_lines,
        )
        if refined is None:
            # Ungrounded or unverified location; rejected to avoid false line reports
            return None

        orig_start = start_line
        start_line, end_line = refined

        # 4. Check grounding against snippets or diffs
        matching_snippets = [
            s
            for s in all_file_snippets
            if max(start_line, s.start_line) <= min(end_line, s.end_line)
        ]
        is_in_changed = _is_line_in_diff(file_path, start_line, end_line, context.changed_files)
        if not matching_snippets and not is_in_changed:
            return None

        # 5. Extract supporting evidence directly supporting the statement
        supporting_evidence: list[EvidenceReference] = []
        for snip in matching_snippets[:2]:
            lines = snip.content.splitlines()
            rel_idx = max(0, start_line - snip.start_line)
            excerpt = lines[rel_idx].strip() if rel_idx < len(lines) else snip.file_path
            supporting_evidence.append(
                EvidenceReference(
                    file_path=snip.file_path,
                    start_line=snip.start_line,
                    end_line=snip.end_line,
                    commit_sha=snip.commit_sha,
                    origin=snip.origin,
                    content_excerpt=excerpt,
                )
            )

        # Include related definition evidence if available and not yet included
        if len(supporting_evidence) < 2:
            statement_tokens = set(re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]+\b", f"{summary} {explanation}"))
            for other_snip in context.evidence_snippets:
                if any(
                    ev.file_path == other_snip.file_path and ev.start_line == other_snip.start_line
                    for ev in supporting_evidence
                ):
                    continue
                if any(
                    tok in other_snip.content
                    for tok in statement_tokens
                    if len(tok) > 3 and tok.lower() not in STOP_WORDS
                ):
                    other_lines = other_snip.content.splitlines()
                    other_excerpt = other_lines[0].strip() if other_lines else other_snip.file_path
                    supporting_evidence.append(
                        EvidenceReference(
                            file_path=other_snip.file_path,
                            start_line=other_snip.start_line,
                            end_line=other_snip.end_line,
                            commit_sha=other_snip.commit_sha,
                            origin=other_snip.origin,
                            content_excerpt=other_excerpt,
                        )
                    )
                    break

        impact = str(raw.get("potential_impact", "Potential runtime issue")).strip()
        confidence_exp = str(
            raw.get("confidence_explanation", "AI model estimate; requires dynamic verification.")
        ).strip()
        if start_line != orig_start:
            refine_note = (
                f"Location refined from enclosing declaration line {orig_start} "
                f"to defective statement at line {start_line}."
            )
            confidence_exp = f"{confidence_exp} ({refine_note})" if confidence_exp else refine_note

        related_tests = [
            str(t).strip() for t in raw.get("related_test_references", []) if isinstance(t, str)
        ]
        verifications = [
            str(v).strip() for v in raw.get("suggested_verification", []) if isinstance(v, str)
        ]

        return AISuggestedBugLocation(
            suggestion_id=suggestion_id,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            commit_sha=context.head_sha,
            is_in_changed_code=is_in_changed,
            summary=summary or "AI-suggested defect candidate",
            detailed_explanation=explanation or summary,
            potential_impact=impact,
            confidence_explanation=confidence_exp,
            supporting_evidence=supporting_evidence,
            related_test_references=related_tests,
            suggested_verification=verifications,
            model_name=self._model,
        )
