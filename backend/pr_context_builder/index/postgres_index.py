"""PostgreSQL implementation of caller file indexing using symbol graph."""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import psycopg

logger = logging.getLogger(__name__)

FIND_CALLER_FILES_SQL = """
SELECT DISTINCT f.path
FROM repositories r
JOIN symbol_edges e ON e.repo_id = r.id
JOIN code_symbols src_sym ON src_sym.id = e.source_symbol_id
JOIN repo_files f ON f.id = src_sym.file_id
LEFT JOIN code_symbols tgt_sym ON tgt_sym.id = e.target_symbol_id
WHERE LOWER(r.owner) = LOWER(%s)
  AND LOWER(r.name) = LOWER(%s)
  AND e.relation IN ('calls', 'imports', 'tests', 'inherits')
  AND src_sym.deleted_at IS NULL
  AND f.deleted_at IS NULL
  AND (
      (tgt_sym.id IS NOT NULL AND tgt_sym.deleted_at IS NULL AND tgt_sym.name = ANY(%s))
      OR
      (e.target_symbol_id IS NULL AND e.target_name = ANY(%s))
  )
ORDER BY f.path
LIMIT %s;
"""


@runtime_checkable
class CallerIndex(Protocol):
    """Protocol for finding files that reference or call specific symbols."""

    def find_caller_files(
        self,
        owner: str,
        repo: str,
        symbol_names: list[str],
        limit: int = 50,
    ) -> list[str]:
        """Find distinct file paths referencing given symbol names."""
        ...


class PostgresCallerIndex:
    """PostgreSQL-backed implementation of CallerIndex."""

    def __init__(
        self,
        database_url: str,
        connection_factory: Any = None,
        connect_timeout: int = 10,
    ) -> None:
        self._database_url = database_url
        self._connection_factory = connection_factory or psycopg.connect
        self._connect_timeout = connect_timeout

    def find_caller_files(
        self,
        owner: str,
        repo: str,
        symbol_names: list[str],
        limit: int = 50,
    ) -> list[str]:
        """Find files with edges calling, importing, or testing given symbols.

        Matches by repository owner and name. Ignores soft-deleted rows.
        Includes both resolved target symbols and unresolved edges matching target_name.
        """
        if not symbol_names:
            return []

        try:
            with self._connection_factory(
                self._database_url, connect_timeout=self._connect_timeout
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        FIND_CALLER_FILES_SQL,
                        (owner, repo, list(symbol_names), list(symbol_names), limit),
                    )
                    rows = cur.fetchall()
                    return [str(row[0]) for row in rows]
        except Exception as exc:
            exc_str = str(exc)
            if self._database_url:
                exc_str = exc_str.replace(self._database_url, "<redacted>")
            exc_str = exc_str[:200]
            logger.warning(
                "Database error while querying caller files for repository %s/%s: %s: %s",
                owner,
                repo,
                type(exc).__name__,
                exc_str,
            )
            return []
