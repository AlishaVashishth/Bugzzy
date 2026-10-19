"""Deterministic unified diff / patch parser.

Extracts diff hunks, starting lines, line counts, and affected line coordinates
without hallucination or heuristic guessing.
"""

import re

from pr_context_builder.models.pr_metadata import DiffHunk

HUNK_HEADER_REGEX = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(?:[ ]*(.*))?$")


def parse_patch_hunks(patch_text: str | None) -> list[DiffHunk]:
    """Parses raw git unified diff patch text into structured DiffHunk objects.

    Args:
        patch_text: Raw diff text string (e.g. from GitHub API ChangedFiles response).

    Returns:
        List of parsed DiffHunk objects with exact old/new line numbers.
    """
    if not patch_text or not patch_text.strip():
        return []

    hunks: list[DiffHunk] = []
    current_hunk: DiffHunk | None = None

    for line in patch_text.splitlines():
        match = HUNK_HEADER_REGEX.match(line)
        if match:
            if current_hunk is not None:
                hunks.append(current_hunk)

            old_start = int(match.group(1))
            old_lines = int(match.group(2)) if match.group(2) is not None else 1
            new_start = int(match.group(3))
            new_lines = int(match.group(4)) if match.group(4) is not None else 1
            header = match.group(5) or ""

            current_hunk = DiffHunk(
                old_start=old_start,
                old_lines=old_lines,
                new_start=new_start,
                new_lines=new_lines,
                header=header.strip(),
                lines=[],
            )
        elif current_hunk is not None:
            current_hunk.lines.append(line)

    if current_hunk is not None:
        hunks.append(current_hunk)

    return hunks
