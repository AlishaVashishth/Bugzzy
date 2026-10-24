"""Turn a GitHub unified-diff patch into line numbers.

The index describes the OLD version of a file (the base branch), so we report
removed lines by their OLD line numbers. Added lines are reported by their NEW
line numbers. Pure insertions have no old line to point at, so we also report
an "anchor": the old line just before the insertion (0 = top of the file).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class PatchLines:
    removed_old_lines: list[int] = field(default_factory=list)
    added_new_lines: list[int] = field(default_factory=list)
    insertion_anchors: list[int] = field(default_factory=list)


def parse_patch(patch: str | None) -> PatchLines:
    """Parse one file's patch text. Returns empty results for None/empty."""
    result = PatchLines()
    if not patch:
        return result

    old_no = 0
    new_no = 0
    in_hunk = False
    run_added = False
    run_removed = False
    run_anchor = 0
    in_run = False

    def close_run() -> None:
        nonlocal in_run, run_added, run_removed
        if in_run and run_added and not run_removed:
            result.insertion_anchors.append(run_anchor)
        in_run = False
        run_added = False
        run_removed = False

    def open_run() -> None:
        nonlocal in_run, run_anchor
        if not in_run:
            in_run = True
            run_anchor = old_no - 1

    for line in patch.splitlines():
        match = _HUNK.match(line)
        if match:
            close_run()
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            new_start = int(match.group(3))
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            # When a side has 0 lines, GitHub's start number is the line BEFORE.
            old_no = old_start + 1 if old_count == 0 else old_start
            new_no = new_start + 1 if new_count == 0 else new_start
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("\\"):  # "\ No newline at end of file"
            continue
        if line.startswith("-"):
            open_run()
            run_removed = True
            result.removed_old_lines.append(old_no)
            old_no += 1
        elif line.startswith("+"):
            open_run()
            run_added = True
            result.added_new_lines.append(new_no)
            new_no += 1
        else:  # unchanged context line
            close_run()
            old_no += 1
            new_no += 1

    close_run()
    return result