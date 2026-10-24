"""Work out which indexed symbols a pull request changed."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CodeSymbol, RepoFile
from app.services.changed_symbols import find_changed_symbols
from app.services.patch_lines import parse_patch


@dataclass
class FileChange:
    path: str
    status: str | None
    changed_symbols: list[str]
    outside_symbols: bool
    note: str | None = None


async def load_symbols_by_path(
    session: AsyncSession, repo_id: int, paths: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Active symbols of the OLD version of each file, keyed by file path."""
    if not paths:
        return {}
    rows = await session.execute(
        select(
            RepoFile.path,
            CodeSymbol.qualified_name,
            CodeSymbol.kind,
            CodeSymbol.start_line,
            CodeSymbol.end_line,
        )
        .join(CodeSymbol, CodeSymbol.file_id == RepoFile.id)
        .where(
            RepoFile.repo_id == repo_id,
            RepoFile.path.in_(paths),
            RepoFile.deleted_at.is_(None),
            CodeSymbol.deleted_at.is_(None),
        )
    )
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        out[row.path].append(
            {
                "qualified_name": row.qualified_name,
                "kind": row.kind,
                "start_line": row.start_line,
                "end_line": row.end_line,
            }
        )
    return dict(out)


def analyze_pr_files(
    files: Iterable[Mapping[str, Any]],
    symbols_by_path: Mapping[str, list[dict[str, Any]]],
) -> list[FileChange]:
    """Pure function: turn PR files + indexed symbols into per-file changes."""
    results: list[FileChange] = []
    for f in files:
        path = f["path"]
        status = f.get("status")

        if not path.endswith(".py"):
            results.append(FileChange(path, status, [], False, "not a python file"))
            continue
        if status == "added":
            results.append(FileChange(path, status, [], False, "new file"))
            continue

        # The index describes the OLD file, so look it up by its old path.
        old_path = f.get("previous_path") or path
        symbols = symbols_by_path.get(old_path) or []
        if not symbols:
            results.append(
                FileChange(path, status, [], False, "no indexed symbols for this file")
            )
            continue

        if status == "removed":
            names = sorted(s["qualified_name"] for s in symbols)
            results.append(FileChange(path, status, names, False, "file removed"))
            continue

        patch = f.get("patch")
        if not patch:
            note = (
                "renamed without content changes"
                if status == "renamed"
                else "no patch from GitHub"
            )
            results.append(FileChange(path, status, [], False, note))
            continue

        changed = find_changed_symbols(symbols, parse_patch(patch))
        results.append(
            FileChange(path, status, changed.symbols, changed.outside_symbols, None)
        )
    return results