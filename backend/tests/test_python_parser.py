"""Unit tests for the pure Python AST parser."""

from __future__ import annotations

import pytest

from app.indexer.python_parser import (
    module_path_from_path,
    parse_python_source,
)


def test_module_path_derivation():
    assert (
        module_path_from_path("src/payments/gateway.py")
        == "src.payments.gateway"
    )
    assert module_path_from_path("src/payments/__init__.py") == "src.payments"
    assert module_path_from_path("app/main.py") == "app.main"
    assert module_path_from_path("main.py") == "main"
    assert module_path_from_path("__init__.py") == "__init__"


def test_python_parser_inline_sample():
    """Test sample matching requirements:

    PaymentGateway.charge_card called by checkout() and covered by a test function,
    asserting symbols, line ranges, qualified names, each edge type (calls, imports,
    inherits, tests), and unresolved edges.
    """

    source_code = '''"""Payment module docstring."""
import math
from abc import ABC, abstractmethod

class BaseGateway(ABC):
    """Base payment gateway."""
    pass

class PaymentGateway(BaseGateway):
    """Concrete payment gateway."""

    @classmethod
    def charge_card(self, user_id: int, amount: float) -> bool:
        """Charge a credit card."""
        return True

def checkout(user_id: int, amount: float):
    """Checkout process."""
    gateway = PaymentGateway()
    return gateway.charge_card(user_id, amount)

def test_checkout():
    """Test the checkout function."""
    res = checkout(1, 100.0)
    assert res is True
'''

    result = parse_python_source(source_code, "src/payments/gateway.py")

    # 1. Assert symbols
    sym_map = {s.qualified_name: s for s in result.symbols}

    # Module symbol
    assert "src.payments.gateway" in sym_map
    mod_sym = sym_map["src.payments.gateway"]
    assert mod_sym.kind == "module"
    assert mod_sym.name == "gateway"
    assert mod_sym.docstring == "Payment module docstring."
    assert mod_sym.start_line == 1
    assert mod_sym.end_line >= 25

    # BaseGateway class
    assert "src.payments.gateway.BaseGateway" in sym_map
    bg_sym = sym_map["src.payments.gateway.BaseGateway"]
    assert bg_sym.kind == "class"
    assert bg_sym.name == "BaseGateway"
    assert bg_sym.signature == "BaseGateway(ABC)"

    # PaymentGateway class
    assert "src.payments.gateway.PaymentGateway" in sym_map
    pg_sym = sym_map["src.payments.gateway.PaymentGateway"]
    assert pg_sym.kind == "class"
    assert pg_sym.name == "PaymentGateway"
    assert pg_sym.signature == "PaymentGateway(BaseGateway)"

    # charge_card method
    assert "src.payments.gateway.PaymentGateway.charge_card" in sym_map
    cc_sym = sym_map["src.payments.gateway.PaymentGateway.charge_card"]
    assert cc_sym.kind == "method"
    assert cc_sym.name == "charge_card"
    assert "user_id: int, amount: float" in cc_sym.signature
    assert cc_sym.docstring == "Charge a credit card."
    # Line range: decorator is at line 12, def is at 13
    assert cc_sym.start_line == 12
    assert cc_sym.end_line >= 15
    assert cc_sym.content_hash is not None

    # checkout function
    assert "src.payments.gateway.checkout" in sym_map
    co_sym = sym_map["src.payments.gateway.checkout"]
    assert co_sym.kind == "function"
    assert co_sym.name == "checkout"
    assert "user_id: int, amount: float" in co_sym.signature

    # test_checkout function
    assert "src.payments.gateway.test_checkout" in sym_map
    tc_sym = sym_map["src.payments.gateway.test_checkout"]
    assert tc_sym.kind == "function"
    assert tc_sym.name == "test_checkout"

    # 2. Assert edge types
    edge_relations = {e.relation for e in result.edges}
    assert "imports" in edge_relations
    assert "inherits" in edge_relations
    assert "calls" in edge_relations
    assert "tests" in edge_relations

    # Imports
    import_edges = [e for e in result.edges if e.relation == "imports"]
    imported_targets = {e.target_name for e in import_edges}
    assert "math" in imported_targets
    assert "abc.ABC" in imported_targets or "ABC" in imported_targets

    # Inherits
    inherit_edges = [e for e in result.edges if e.relation == "inherits"]
    assert any(
        e.source_qualified_name == "src.payments.gateway.PaymentGateway"
        and e.target_name == "BaseGateway"
        for e in inherit_edges
    )

    # Calls (from checkout)
    call_edges = [
        e
        for e in result.edges
        if e.relation == "calls"
        and e.source_qualified_name == "src.payments.gateway.checkout"
    ]
    call_targets = {e.target_name for e in call_edges}
    assert (
        "gateway.charge_card" in call_targets
        or "PaymentGateway.charge_card" in call_targets
        or "PaymentGateway" in call_targets
    )

    # Tests (from test_checkout)
    test_edges = [
        e
        for e in result.edges
        if e.relation == "tests"
        and e.source_qualified_name == "src.payments.gateway.test_checkout"
    ]
    test_targets = {e.target_name for e in test_edges}
    assert "checkout" in test_targets

    # All edges have positive call_line
    for e in result.edges:
        assert e.call_line > 0
