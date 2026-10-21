"""Database integration tests for the repository indexer and queries.

Runs only when TEST_DATABASE_URL is configured on a test database/branch.
Mocks all GitHub HTTP calls to ensure no external network access during tests.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.db.engine import build_test_engine, same_database_target
from app.db.models import CodeSymbol, RepoFile, Repository, SymbolEdge
from app.indexer import queries
from app.indexer.repo_indexer import index_repo

_settings = get_settings()
TEST_DATABASE_URL = _settings.test_database_url
MAIN_DATABASE_URL = _settings.database_url

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL is not set; skipping indexer DB tests.", allow_module_level=True)

if MAIN_DATABASE_URL and same_database_target(TEST_DATABASE_URL, MAIN_DATABASE_URL):
    raise RuntimeError("Refusing to run DB tests against main database target.")

_engine = build_test_engine(TEST_DATABASE_URL)
TestSession = async_sessionmaker(_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def db_session():
    async with TestSession() as session:
        yield session


# Sample Python files for testing
FILE1_CODE_V1 = '''"""Payment gateway implementation."""
from abc import ABC

class PaymentGateway(ABC):
    @classmethod
    def charge_card(self, user_id: int, amount: float) -> bool:
        """Process card transaction."""
        return True

def checkout(user_id: int, amount: float):
    return PaymentGateway.charge_card(user_id, amount)
'''

FILE1_CODE_V2_REMOVED_FUNC = '''"""Payment gateway implementation."""
from abc import ABC

class PaymentGateway(ABC):
    @classmethod
    def charge_card(self, user_id: int, amount: float) -> bool:
        """Process card transaction."""
        return True
'''

TEST_FILE_CODE = '''"""Tests for checkout."""
from src.payments.gateway import checkout

def test_checkout_flow():
    res = checkout(1, 50.0)
    assert res is True
'''


@pytest.mark.asyncio
async def test_indexer_idempotency_and_removal_and_callers(db_session, monkeypatch):
    """Test:

    1. Initial indexing populates repo_files, code_symbols, and symbol_edges.
    2. Indexing a second time produces identical row counts and stable symbol IDs.
    3. Removing a function in a subsequent commit marks deleted_at.
    4. callers() CTE returns the expected caller with file path and line ranges.
    """
    test_repo_github_id = 987654321

    # Clean up any leftover test data first
    await db_session.execute(
        text("DELETE FROM repositories WHERE github_repo_id = :gid"),
        {"gid": test_repo_github_id},
    )
    await db_session.commit()

    # Create dummy repository
    repo = Repository(
        github_repo_id=test_repo_github_id,
        owner="test-owner",
        name="test-repo",
        default_branch="main",
        installation_id=12345,
    )
    db_session.add(repo)
    await db_session.commit()
    await db_session.refresh(repo)
    repo_id = repo.id

    try:
        # Mock fetch_tree and _fetch_blob_content
        current_tree = [
            {"path": "src/payments/gateway.py", "type": "blob", "sha": "sha_v1_gateway", "size": len(FILE1_CODE_V1)},
            {"path": "tests/test_gateway.py", "type": "blob", "sha": "sha_v1_test", "size": len(TEST_FILE_CODE)},
        ]

        async def fake_fetch_tree(gh, owner, name, branch):
            return "commit_sha_1", current_tree

        async def fake_fetch_blob(gh, owner, name, blob_sha):
            if blob_sha == "sha_v1_gateway":
                return FILE1_CODE_V1
            if blob_sha == "sha_v2_gateway":
                return FILE1_CODE_V2_REMOVED_FUNC
            if blob_sha == "sha_v1_test":
                return TEST_FILE_CODE
            return ""

        monkeypatch.setattr("app.indexer.repo_indexer.fetch_tree", fake_fetch_tree)
        monkeypatch.setattr("app.indexer.repo_indexer._fetch_blob_content", fake_fetch_blob)

        # Run 1: First indexing
        await index_repo(db_session, "fake_token", repo_id)

        # Verify repo_files count
        files_count_1 = (await db_session.execute(
            select(func.count(RepoFile.id)).where(RepoFile.repo_id == repo_id, RepoFile.deleted_at.is_(None))
        )).scalar_one()
        assert files_count_1 == 2

        # Verify symbols count
        symbols_1 = (await db_session.execute(
            select(CodeSymbol).where(CodeSymbol.repo_id == repo_id, CodeSymbol.deleted_at.is_(None))
        )).scalars().all()
        assert len(symbols_1) >= 4  # 2 modules, 1 class, 2 functions/methods
        initial_symbol_ids = {s.qualified_name: s.id for s in symbols_1}

        # Verify edges count
        edges_1 = (await db_session.execute(
            select(SymbolEdge).where(SymbolEdge.repo_id == repo_id)
        )).scalars().all()
        assert len(edges_1) >= 3

        # Run 2: Second indexing (Idempotency test)
        await index_repo(db_session, "fake_token", repo_id)

        symbols_2 = (await db_session.execute(
            select(CodeSymbol).where(CodeSymbol.repo_id == repo_id, CodeSymbol.deleted_at.is_(None))
        )).scalars().all()
        assert len(symbols_2) == len(symbols_1)
        second_symbol_ids = {s.qualified_name: s.id for s in symbols_2}
        assert initial_symbol_ids == second_symbol_ids  # IDs are completely stable!

        edges_2 = (await db_session.execute(
            select(SymbolEdge).where(SymbolEdge.repo_id == repo_id)
        )).scalars().all()
        assert len(edges_2) == len(edges_1)

        # Run 3: Test queries.callers() on checkout
        checkout_sym = (await db_session.execute(
            select(CodeSymbol).where(
                CodeSymbol.repo_id == repo_id,
                CodeSymbol.qualified_name == "src.payments.gateway.checkout",
            )
        )).scalar_one()

        # charge_card callers should include checkout
        charge_card_sym = (await db_session.execute(
            select(CodeSymbol).where(
                CodeSymbol.repo_id == repo_id,
                CodeSymbol.qualified_name == "src.payments.gateway.PaymentGateway.charge_card",
            )
        )).scalar_one()

        charge_card_callers = await queries.callers(db_session, charge_card_sym.id, depth=2)
        assert len(charge_card_callers) >= 1
        assert any(c["qualified_name"] == "src.payments.gateway.checkout" for c in charge_card_callers)
        first_caller = charge_card_callers[0]
        assert first_caller["file_path"] == "src/payments/gateway.py"
        assert first_caller["start_line"] > 0
        assert first_caller["end_line"] >= first_caller["start_line"]

        # Run 4: Test queries.tests_for()
        checkout_tests = await queries.tests_for(db_session, checkout_sym.id)
        assert len(checkout_tests) >= 1
        assert any(t["qualified_name"] == "tests.test_gateway.test_checkout_flow" for t in checkout_tests)

        # Run 5: Test queries.list_folder and files_by_path_prefix
        src_files = await queries.files_by_path_prefix(db_session, repo_id, "src/payments")
        assert len(src_files) == 1
        assert src_files[0]["path"] == "src/payments/gateway.py"

        # Run 6: Remove checkout function and update tree to commit 2
        current_tree = [
            {"path": "src/payments/gateway.py", "type": "blob", "sha": "sha_v2_gateway", "size": len(FILE1_CODE_V2_REMOVED_FUNC)},
            {"path": "tests/test_gateway.py", "type": "blob", "sha": "sha_v1_test", "size": len(TEST_FILE_CODE)},
        ]

        async def fake_fetch_tree_v2(gh, owner, name, branch):
            return "commit_sha_2", current_tree

        monkeypatch.setattr("app.indexer.repo_indexer.fetch_tree", fake_fetch_tree_v2)

        await index_repo(db_session, "fake_token", repo_id)

        # Verify checkout has deleted_at set
        deleted_checkout = (await db_session.execute(
            select(CodeSymbol).where(
                CodeSymbol.repo_id == repo_id,
                CodeSymbol.qualified_name == "src.payments.gateway.checkout",
            )
        )).scalar_one()
        assert deleted_checkout.deleted_at is not None

        # Verify active symbols exclude checkout
        active_symbols = (await db_session.execute(
            select(CodeSymbol).where(CodeSymbol.repo_id == repo_id, CodeSymbol.deleted_at.is_(None))
        )).scalars().all()
        assert all(s.qualified_name != "src.payments.gateway.checkout" for s in active_symbols)

    finally:
        # Clean up test repo
        await db_session.execute(
            text("DELETE FROM repositories WHERE id = :id"), {"id": repo_id}
        )
        await db_session.commit()
