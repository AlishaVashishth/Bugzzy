"""Decide which indexed symbols a patch changed.

`symbols` are the functions/classes the index holds for the OLD version of one
file, each a dict with qualified_name, start_line and end_line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from app.services.patch_lines import PatchLines


@dataclass
class ChangedSymbols:
    symbols: list[str] = field(default_factory=list)  # sorted qualified names
    outside_symbols: bool = False  # True if some change is not inside any symbol


def _innermost(symbols: Iterable[Mapping[str, Any]], first: int, last: int):
    """Smallest symbol that fully covers lines first..last, or None."""
    best = None
    for sym in symbols:
        start, end = sym.get("start_line"), sym.get("end_line")
        if start is None or end is None:
            continue
        if start <= first and last <= end:
            if best is None or (end - start) < (best["end_line"] - best["start_line"]):
                best = sym
    return best


def find_changed_symbols(
    symbols: Iterable[Mapping[str, Any]], lines: PatchLines
) -> ChangedSymbols:
    symbols = list(symbols)
    found: set[str] = set()
    outside = False

    # A removed or replaced line must sit inside a symbol (old line numbers).
    for old_line in lines.removed_old_lines:
        sym = _innermost(symbols, old_line, old_line)
        if sym is None:
            outside = True
        else:
            found.add(sym["qualified_name"])

    # A pure insertion went in between old line `anchor` and `anchor + 1`.
    # It belongs to a symbol only if that symbol covers both lines.
    for anchor in lines.insertion_anchors:
        sym = _innermost(symbols, anchor, anchor + 1)
        if sym is None:
            outside = True
        else:
            found.add(sym["qualified_name"])

    return ChangedSymbols(symbols=sorted(found), outside_symbols=outside)