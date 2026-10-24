"""Unit tests for PostgresCallerIndex using fake connection objects."""

from __future__ import annotations

import logging
from typing import Any

import psycopg
import pytest

from pr_context_builder.index.postgres_index import (
    CallerIndex,
    PostgresCallerIndex,
)


class FakeCursor:
    """Mock database cursor for testing SQL execution and results."""

    def __init__(
        self,
        rows: list[tuple[Any, ...]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.rows = rows if rows is not None else []
        self.error = error
        self.executed_queries: list[str] = []
        self.executed_params: list[Any] = []

    def execute(self, query: str, params: Any = None) -> None:
        if self.error:
            raise self.error
        self.executed_queries.append(query)
        self.executed_params.append(params)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        return False


class FakeConnection:
    """Mock database connection providing FakeCursor."""

    def __init__(self, cursor: FakeCursor) -> None:
        self.cursor_instance = cursor
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        return False


def test_caller_index_protocol() -> None:
    """Verify PostgresCallerIndex conforms to the CallerIndex protocol."""
    index = PostgresCallerIndex("postgresql://user:pass@localhost:5432/db")
    assert isinstance(index, CallerIndex)


def test_normal_result() -> None:
    """Cover normal result: query executes, returns distinct file paths."""
    cursor = FakeCursor(rows=[("src/auth.py",), ("src/main.py",)])
    conn = FakeConnection(cursor)

    connect_calls: list[dict[str, Any]] = []

    def fake_connect(url: str, **kwargs: Any) -> FakeConnection:
        connect_calls.append({"url": url, "kwargs": kwargs})
        return conn

    index = PostgresCallerIndex("postgresql://user:pass@localhost:5432/db", connection_factory=fake_connect)
    files = index.find_caller_files(
        owner="octocat",
        repo="Hello-World",
        symbol_names=["authenticate", "verify_token"],
        limit=25,
    )

    assert files == ["src/auth.py", "src/main.py"]
    assert len(connect_calls) == 1
    assert connect_calls[0]["kwargs"].get("connect_timeout") == 10
    assert len(cursor.executed_queries) == 1
    assert cursor.executed_params[0] == (
        "octocat",
        "Hello-World",
        ["authenticate", "verify_token"],
        ["authenticate", "verify_token"],
        25,
    )


def test_empty_symbol_list() -> None:
    """Empty symbol list should return [] immediately without connecting."""
    connect_called = False

    def fake_connect(url: str, **kwargs: Any) -> FakeConnection:
        nonlocal connect_called
        connect_called = True
        return FakeConnection(FakeCursor())

    index = PostgresCallerIndex("postgresql://user:pass@localhost:5432/db", connection_factory=fake_connect)
    files = index.find_caller_files("octocat", "Hello-World", [])

    assert files == []
    assert not connect_called


def test_db_error_returns_empty_list(caplog: pytest.LogCaptureFixture) -> None:
    """Database error during connect or query execution returns [] and logs warning."""
    db_url = "postgresql://user:secret_password@db.neon.tech:5432/testdb"

    def fake_connect_error(url: str, **kwargs: Any) -> FakeConnection:
        raise psycopg.OperationalError("connection refused")

    index = PostgresCallerIndex(db_url, connection_factory=fake_connect_error)

    with caplog.at_level(logging.WARNING):
        files = index.find_caller_files("octocat", "Hello-World", ["foo"])

    assert files == []
    # Verify warning logged
    assert any("Database error" in record.message for record in caplog.records)
    # Ensure database URL (with potential secrets) was never logged
    assert db_url not in caplog.text
    assert "secret_password" not in caplog.text


def test_cursor_error_returns_empty_list(caplog: pytest.LogCaptureFixture) -> None:
    """Error during query execution inside cursor returns [] and logs warning."""
    cursor = FakeCursor(error=psycopg.DatabaseError("query timeout"))
    conn = FakeConnection(cursor)

    index = PostgresCallerIndex("postgresql://localhost/db", connection_factory=lambda url, **kw: conn)

    with caplog.at_level(logging.WARNING):
        files = index.find_caller_files("octocat", "Hello-World", ["bar"])

    assert files == []
    assert any("Database error" in record.message for record in caplog.records)


def test_empty_tables_or_missing_repo_returns_empty_list() -> None:
    """When query yields zero rows, returns empty list."""
    cursor = FakeCursor(rows=[])
    conn = FakeConnection(cursor)

    index = PostgresCallerIndex("postgresql://localhost/db", connection_factory=lambda url, **kw: conn)
    files = index.find_caller_files("octocat", "nonexistent-repo", ["symbol"])

    assert files == []


def test_sql_uses_placeholders() -> None:
    """Ensure query uses %s placeholders and no literal strings or concatenation."""
    cursor = FakeCursor(rows=[])
    conn = FakeConnection(cursor)

    index = PostgresCallerIndex("postgresql://localhost/db", connection_factory=lambda url, **kw: conn)
    owner = "special'owner"
    repo = "special\"repo"
    symbols = ["func_a", "func_b"]
    limit = 10

    index.find_caller_files(owner=owner, repo=repo, symbol_names=symbols, limit=limit)

    assert len(cursor.executed_queries) == 1
    query = cursor.executed_queries[0]
    params = cursor.executed_params[0]

    # Verify %s placeholders are used in SQL
    assert "%s" in query
    # Verify no raw user parameters were interpolated into query text
    assert owner not in query
    assert repo not in query
    assert "func_a" not in query
    assert "func_b" not in query

    # Verify placeholders are parameterized in the tuple
    assert params[0] == owner
    assert params[1] == repo
    assert params[2] == symbols
    assert params[3] == symbols
    assert params[4] == limit

    # Count placeholders
    assert query.count("%s") == 5


def test_url_never_appears_in_log_output_when_in_exception(caplog: pytest.LogCaptureFixture) -> None:
    """Ensure that if an exception message contains the database URL, it is redacted from log output."""
    db_url = "postgresql://user:super_secret@localhost:5432/my_database"

    def fake_connect_error(url: str, **kwargs: Any) -> FakeConnection:
        raise psycopg.OperationalError(f"could not connect to {db_url}: timeout")

    index = PostgresCallerIndex(db_url, connection_factory=fake_connect_error)

    with caplog.at_level(logging.WARNING):
        files = index.find_caller_files("owner", "repo", ["symbol"])

    assert files == []
    # Assert database URL never appears in log output
    assert db_url not in caplog.text
    assert "super_secret" not in caplog.text
    assert "<redacted>" in caplog.text
    assert "OperationalError" in caplog.text
