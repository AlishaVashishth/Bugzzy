"""Queries for the Context Builder agent reading the code index.

Provides fast, indexed read operations over repo_files, code_symbols, and symbol_edges.
All queries ignore tombstoned (deleted_at IS NOT NULL) rows.
"""

from __future__ import annotations

from typing import Any
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def find_symbols(
    session: AsyncSession, repo_id: int, search_text: str
) -> list[dict[str, Any]]:
    """Search active symbols by text query across qualified_name, name, and docstring."""
    query = text("""
        SELECT 
            s.id,
            s.repo_id,
            s.file_id,
            f.path AS file_path,
            s.name,
            s.qualified_name,
            s.kind,
            s.signature,
            s.docstring,
            s.start_line,
            s.end_line,
            s.content_hash
        FROM code_symbols s
        JOIN repo_files f ON f.id = s.file_id
        WHERE s.repo_id = :repo_id
          AND s.deleted_at IS NULL
          AND f.deleted_at IS NULL
          AND (
              s.name ILIKE :pattern
              OR s.qualified_name ILIKE :pattern
              OR s.docstring ILIKE :pattern
              OR (s.search_vec @@ plainto_tsquery('simple', :raw_text))
          )
        ORDER BY 
            CASE 
                WHEN s.name = :raw_text THEN 1
                WHEN s.qualified_name = :raw_text THEN 2
                WHEN s.name ILIKE :prefix_pattern THEN 3
                ELSE 4
            END,
            s.name ASC
        LIMIT 50;
    """)

    result = await session.execute(
        query,
        {
            "repo_id": repo_id,
            "raw_text": search_text,
            "pattern": f"%{search_text}%",
            "prefix_pattern": f"{search_text}%",
        },
    )
    return [dict(row._mapping) for row in result]


async def get_symbol(
    session: AsyncSession, repo_id: int, qualified_name: str
) -> dict[str, Any] | None:
    """Look up an exact symbol by repo_id and qualified_name."""
    query = text("""
        SELECT 
            s.id,
            s.repo_id,
            s.file_id,
            f.path AS file_path,
            s.name,
            s.qualified_name,
            s.kind,
            s.signature,
            s.docstring,
            s.start_line,
            s.end_line,
            s.content_hash
        FROM code_symbols s
        JOIN repo_files f ON f.id = s.file_id
        WHERE s.repo_id = :repo_id
          AND s.qualified_name = :qualified_name
          AND s.deleted_at IS NULL
          AND f.deleted_at IS NULL
        LIMIT 1;
    """)

    result = await session.execute(
        query, {"repo_id": repo_id, "qualified_name": qualified_name}
    )
    row = result.first()
    return dict(row._mapping) if row else None


async def callers(
    session: AsyncSession, symbol_id: int, depth: int = 1
) -> list[dict[str, Any]]:
    """Return callers of a symbol up to depth (1..3) using a recursive CTE with cycle safety."""
    clamped_depth = max(1, min(depth, 3))

    query = text("""
        WITH RECURSIVE caller_graph AS (
            -- Base case: immediate callers
            SELECT 
                e.source_symbol_id AS caller_id,
                e.target_symbol_id AS callee_id,
                e.call_line,
                1 AS current_depth,
                ARRAY[e.target_symbol_id, e.source_symbol_id] AS path
            FROM symbol_edges e
            JOIN code_symbols s ON s.id = e.source_symbol_id
            JOIN repo_files f ON f.id = s.file_id
            WHERE e.target_symbol_id = :symbol_id
              AND e.relation = 'calls'
              AND s.deleted_at IS NULL
              AND f.deleted_at IS NULL

            UNION ALL

            -- Recursive step: callers of the caller
            SELECT 
                e.source_symbol_id,
                e.target_symbol_id,
                e.call_line,
                cg.current_depth + 1,
                cg.path || e.source_symbol_id
            FROM symbol_edges e
            JOIN caller_graph cg ON e.target_symbol_id = cg.caller_id
            JOIN code_symbols s ON s.id = e.source_symbol_id
            JOIN repo_files f ON f.id = s.file_id
            WHERE cg.current_depth < :max_depth
              AND e.relation = 'calls'
              AND s.deleted_at IS NULL
              AND f.deleted_at IS NULL
              AND NOT (e.source_symbol_id = ANY(cg.path))
        )
        SELECT DISTINCT ON (cg.caller_id, cg.call_line)
            s.id,
            s.repo_id,
            s.file_id,
            f.path AS file_path,
            s.name,
            s.qualified_name,
            s.kind,
            s.signature,
            s.docstring,
            s.start_line,
            s.end_line,
            cg.current_depth AS depth,
            cg.call_line
        FROM caller_graph cg
        JOIN code_symbols s ON s.id = cg.caller_id
        JOIN repo_files f ON f.id = s.file_id
        ORDER BY cg.caller_id, cg.call_line, cg.current_depth ASC;
    """)

    result = await session.execute(
        query, {"symbol_id": symbol_id, "max_depth": clamped_depth}
    )
    return [dict(row._mapping) for row in result]


async def callees(
    session: AsyncSession, symbol_id: int
) -> list[dict[str, Any]]:
    """Return all symbols that the given symbol calls directly."""
    query = text("""
        SELECT 
            e.id AS edge_id,
            e.target_symbol_id,
            e.target_name,
            e.relation,
            e.call_line,
            s.name AS symbol_name,
            s.qualified_name,
            s.kind,
            s.signature,
            s.start_line,
            s.end_line,
            f.path AS file_path
        FROM symbol_edges e
        LEFT JOIN code_symbols s ON s.id = e.target_symbol_id AND s.deleted_at IS NULL
        LEFT JOIN repo_files f ON f.id = s.file_id AND f.deleted_at IS NULL
        WHERE e.source_symbol_id = :symbol_id
          AND e.relation = 'calls'
        ORDER BY e.call_line ASC, e.target_name ASC;
    """)

    result = await session.execute(query, {"symbol_id": symbol_id})
    return [dict(row._mapping) for row in result]


async def tests_for(
    session: AsyncSession, symbol_id: int
) -> list[dict[str, Any]]:
    """Return test functions that test or cover the given symbol."""
    query = text("""
        SELECT 
            s.id,
            s.repo_id,
            s.file_id,
            f.path AS file_path,
            s.name,
            s.qualified_name,
            s.kind,
            s.signature,
            s.docstring,
            s.start_line,
            s.end_line,
            e.call_line
        FROM symbol_edges e
        JOIN code_symbols s ON s.id = e.source_symbol_id
        JOIN repo_files f ON f.id = s.file_id
        WHERE e.target_symbol_id = :symbol_id
          AND e.relation = 'tests'
          AND s.deleted_at IS NULL
          AND f.deleted_at IS NULL
        ORDER BY f.path ASC, s.start_line ASC;
    """)

    result = await session.execute(query, {"symbol_id": symbol_id})
    return [dict(row._mapping) for row in result]


async def list_folder(
    session: AsyncSession, repo_id: int, parent_path: str
) -> list[dict[str, Any]]:
    """List directory contents (files and subfolders) for a given parent path."""
    query = text("""
        SELECT 
            id,
            repo_id,
            path,
            parent_path,
            name,
            node_type,
            language,
            size_bytes,
            content_hash,
            updated_at
        FROM repo_files
        WHERE repo_id = :repo_id
          AND parent_path = :parent_path
          AND deleted_at IS NULL
        ORDER BY 
            CASE WHEN node_type = 'dir' THEN 0 ELSE 1 END,
            name ASC;
    """)

    result = await session.execute(
        query, {"repo_id": repo_id, "parent_path": parent_path}
    )
    return [dict(row._mapping) for row in result]


async def files_by_path_prefix(
    session: AsyncSession, repo_id: int, prefix: str
) -> list[dict[str, Any]]:
    """List active files matching a path prefix."""
    prefix_clean = prefix.rstrip("/") + "/" if prefix and not prefix.endswith("/") else prefix
    pattern = f"{prefix_clean}%"

    query = text("""
        SELECT 
            id,
            repo_id,
            path,
            parent_path,
            name,
            node_type,
            language,
            size_bytes,
            content_hash,
            updated_at
        FROM repo_files
        WHERE repo_id = :repo_id
          AND path LIKE :pattern
          AND deleted_at IS NULL
        ORDER BY path ASC;
    """)

    result = await session.execute(query, {"repo_id": repo_id, "pattern": pattern})
    return [dict(row._mapping) for row in result]
