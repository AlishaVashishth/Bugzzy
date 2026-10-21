"""Contract tests for the code indexer.

Validates that code_symbols and symbol_edges satisfy all teammate contract rules,
and that the teammate's exact query executes correctly.
"""

from __future__ import annotations

import re
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.db.engine import build_test_engine, same_database_target
from app.db.models import CodeSymbol, RepoFile, Repository, SymbolEdge
from app.indexer.repo_indexer import _resolve_unresolved_edges, index_repo

_settings = get_settings()
TEST_DATABASE_URL = _settings.test_database_url
MAIN_DATABASE_URL = _settings.database_url

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL is not set; skipping contract tests.", allow_module_level=True)

if MAIN_DATABASE_URL and same_database_target(TEST_DATABASE_URL, MAIN_DATABASE_URL):
    raise RuntimeError("Refusing to run DB tests against main database target.")

_engine = build_test_engine(TEST_DATABASE_URL)
TestSession = async_sessionmaker(_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def db_session():
    async with TestSession() as session:
        yield session


GATEWAY_PY = '''"""Payment gateway implementation."""
import a.b
from a.b import x
import mod

class Base:
    pass

class PaymentGateway(mod.Base):
    def charge_card(self, amount: float) -> bool:
        """Charge card transaction."""
        return True

    def retry(self) -> bool:
        return self.charge_card(10.0)
'''

CHECKOUT_PY = '''"""Checkout service."""
from payments.gateway import PaymentGateway

def checkout():
    gateway = PaymentGateway()
    return gateway.charge_card(100.0)
'''

TEST_PAY_PY = '''"""Tests for payment flow."""
from payments.gateway import PaymentGateway

def test_pay_flow():
    gateway = PaymentGateway()
    assert gateway.charge_card(50.0) is True
'''

CALLER_UNRESOLVED_PY = '''"""External or dynamic caller."""

def process_payment(unresolvable_obj):
    return unresolvable_obj.charge_card(25.0)
'''


TEAMMATE_SQL = """
   SELECT DISTINCT f.path
   FROM repositories r
   JOIN symbol_edges e ON e.repo_id = r.id
   JOIN code_symbols src_sym ON src_sym.id = e.source_symbol_id
   JOIN repo_files f ON f.id = src_sym.file_id
   LEFT JOIN code_symbols tgt_sym ON tgt_sym.id = e.target_symbol_id
   WHERE LOWER(r.owner) = LOWER(%s) AND LOWER(r.name) = LOWER(%s)
     AND e.relation IN ('calls','imports','tests','inherits')
     AND src_sym.deleted_at IS NULL AND f.deleted_at IS NULL
     AND ((tgt_sym.id IS NOT NULL AND tgt_sym.deleted_at IS NULL AND tgt_sym.name = ANY(%s))
          OR (e.target_symbol_id IS NULL AND e.target_name = ANY(%s)))
   ORDER BY f.path LIMIT %s;
"""


@pytest.mark.asyncio
async def test_indexer_contract(db_session, monkeypatch):
    """Assert all contract rules a-f and execute teammate's exact SQL query."""
    test_repo_github_id = 998877665
    raw_owner = "BugzzyOwner"
    raw_name = "ContractRepo"

    # Clean up existing test data
    await db_session.execute(
        text("DELETE FROM repositories WHERE github_repo_id = :gid"),
        {"gid": test_repo_github_id},
    )
    await db_session.commit()

    repo = Repository(
        github_repo_id=test_repo_github_id,
        owner=raw_owner,
        name=raw_name,
        default_branch="main",
        installation_id=98765,
    )
    db_session.add(repo)
    await db_session.commit()
    await db_session.refresh(repo)
    repo_id = repo.id

    try:
        # Fixture repo files
        tree = [
            {"path": "payments/gateway.py", "type": "blob", "sha": "sha_gateway", "size": len(GATEWAY_PY)},
            {"path": "orders/checkout.py", "type": "blob", "sha": "sha_checkout", "size": len(CHECKOUT_PY)},
            {"path": "tests/test_pay.py", "type": "blob", "sha": "sha_test_pay", "size": len(TEST_PAY_PY)},
            {"path": "external/caller.py", "type": "blob", "sha": "sha_caller", "size": len(CALLER_UNRESOLVED_PY)},
        ]

        async def fake_fetch_tree(gh, owner, name, branch):
            return "commit_sha_contract", tree

        async def fake_fetch_blob(gh, owner, name, blob_sha):
            if blob_sha == "sha_gateway":
                return GATEWAY_PY
            if blob_sha == "sha_checkout":
                return CHECKOUT_PY
            if blob_sha == "sha_test_pay":
                return TEST_PAY_PY
            if blob_sha == "sha_caller":
                return CALLER_UNRESOLVED_PY
            return ""

        monkeypatch.setattr("app.indexer.repo_indexer.fetch_tree", fake_fetch_tree)
        monkeypatch.setattr("app.indexer.repo_indexer._fetch_blob_content", fake_fetch_blob)

        # 1. Index repository
        await index_repo(db_session, "fake_token", repo_id)

        # Query all symbols and edges
        symbols_res = await db_session.execute(
            select(CodeSymbol).where(CodeSymbol.repo_id == repo_id)
        )
        symbols = symbols_res.scalars().all()

        edges_res = await db_session.execute(
            select(SymbolEdge).where(SymbolEdge.repo_id == repo_id)
        )
        edges = edges_res.scalars().all()

        # Rule a: code_symbols.name never contains "."
        for s in symbols:
            assert "." not in s.name, f"Symbol name contains dot: {s.name} ({s.qualified_name})"

        # Rule b: symbol_edges.target_name never contains "."
        # self.charge_card(), obj.charge_card(), pkg.mod.charge_card() all store "charge_card"
        # "from a.b import x" stores "x"
        # "import a.b" stores "b"
        # "class X(mod.Base)" stores "Base"
        for e in edges:
            assert "." not in e.target_name, f"Edge target_name contains dot: {e.target_name}"

        target_names = {e.target_name for e in edges}
        assert "charge_card" in target_names
        assert "x" in target_names
        assert "b" in target_names
        assert "Base" in target_names

        # Rule c: every edge has repo_id set; import edges come from module symbol;
        # inherits edges from class symbol; tests edges from test functions
        sym_map = {s.id: s for s in symbols}
        for e in edges:
            assert e.repo_id == repo_id, f"Edge repo_id mismatch: {e.repo_id} != {repo_id}"
            src = sym_map.get(e.source_symbol_id)
            assert src is not None, f"Source symbol {e.source_symbol_id} not found"
            if e.relation == "imports":
                assert src.kind == "module", f"Import edge source {src.name} is not module: {src.kind}"
            elif e.relation == "inherits":
                assert src.kind == "class", f"Inherits edge source {src.name} is not class: {src.kind}"
            elif e.relation == "tests":
                assert src.kind in ("function", "method") and (src.name.startswith("test_") or "test" in src.qualified_name)

        # Rule d: relation is only calls, imports, tests, inherits
        valid_relations = {"calls", "imports", "tests", "inherits"}
        for e in edges:
            assert e.relation in valid_relations, f"Invalid relation: {e.relation}"

        # Rule f: repositories.owner and name are stored exactly as GitHub payload gives them
        stored_repo = (await db_session.execute(
            select(Repository).where(Repository.id == repo_id)
        )).scalar_one()
        assert stored_repo.owner == raw_owner
        assert stored_repo.name == raw_name

        # Execute teammate's query via psycopg with exact SQL string
        import psycopg

        sync_url = TEST_DATABASE_URL
        if sync_url.startswith("postgresql+asyncpg://"):
            sync_url = sync_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        elif sync_url.startswith("postgresql+psycopg://"):
            sync_url = sync_url.replace("postgresql+psycopg://", "postgresql://", 1)

        with psycopg.connect(sync_url) as conn:
            with conn.cursor() as cur:
                # Query for ["charge_card"]:
                # Must return checkout file, tests file, and unresolved caller file
                cur.execute(TEAMMATE_SQL, (raw_owner, raw_name, ["charge_card"], ["charge_card"], 50))
                rows = [r[0] for r in cur.fetchall()]
                assert sorted(rows) == sorted([
                    "external/caller.py",
                    "orders/checkout.py",
                    "payments/gateway.py",  # self.charge_card() inside PaymentGateway.retry
                    "tests/test_pay.py",
                ]) or sorted(rows) == sorted([
                    "external/caller.py",
                    "orders/checkout.py",
                    "tests/test_pay.py",
                ])

                # Query for ["does_not_exist"]: returns []
                cur.execute(TEAMMATE_SQL, (raw_owner, raw_name, ["does_not_exist"], ["does_not_exist"], 50))
                rows_empty = [r[0] for r in cur.fetchall()]
                assert rows_empty == []

        # Rule e: soft-delete charge_card, re-run resolution pass:
        # edges that pointed to it have target_symbol_id NULL again and still match by target_name
        charge_card_sym = next(s for s in symbols if s.name == "charge_card" and s.kind == "method")
        await db_session.execute(
            text("UPDATE code_symbols SET deleted_at = now() WHERE id = :sym_id"),
            {"sym_id": charge_card_sym.id},
        )
        await db_session.commit()

        # Re-run resolution pass
        await _resolve_unresolved_edges(db_session, repo_id)

        # Edges with target_name == 'charge_card' must now have target_symbol_id IS NULL
        updated_edges = (await db_session.execute(
            select(SymbolEdge).where(SymbolEdge.repo_id == repo_id, SymbolEdge.target_name == "charge_card")
        )).scalars().all()
        for e in updated_edges:
            assert e.target_symbol_id is None, f"Edge target_symbol_id not nullified: {e.id}"

        # Teammate's query should still match all caller files via target_name
        with psycopg.connect(sync_url) as conn:
            with conn.cursor() as cur:
                cur.execute(TEAMMATE_SQL, (raw_owner, raw_name, ["charge_card"], ["charge_card"], 50))
                rows_after_delete = [r[0] for r in cur.fetchall()]
                assert "orders/checkout.py" in rows_after_delete
                assert "tests/test_pay.py" in rows_after_delete
                assert "external/caller.py" in rows_after_delete

    finally:
        # Clean up test repo
        await db_session.execute(
            text("DELETE FROM repositories WHERE id = :id"), {"id": repo_id}
        )
        await db_session.commit()
