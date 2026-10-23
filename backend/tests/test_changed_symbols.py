"""Tests for the changed-symbol finder. Pure functions: no database, no network."""

from app.services.changed_symbols import ChangedSymbols, find_changed_symbols
from app.services.patch_lines import PatchLines, parse_patch


def sym(qname, start, end):
    return {"qualified_name": qname, "start_line": start, "end_line": end}


BILLING = [sym("billing.calculate_tax", 1, 3), sym("billing.final_price", 6, 8)]


def test_real_pr_diff_end_to_end():
    patch = "\n".join(
        [
            "@@ -4,5 +4,5 @@ def calculate_tax(subtotal, rate):",
            " ",
            " ",
            " def final_price(subtotal, rate):",
            "-    tax = calculate_tax(subtotal, rate)",
            "+    tax = calculate_tax(subtotal)",
            "     return subtotal + tax",
        ]
    )
    result = find_changed_symbols(BILLING, parse_patch(patch))
    assert result == ChangedSymbols(["billing.final_price"], False)


def test_nested_symbol_prefers_innermost():
    symbols = [sym("cart.Cart", 1, 20), sym("cart.Cart.total", 5, 10)]
    result = find_changed_symbols(symbols, PatchLines([7], [7], []))
    assert result.symbols == ["cart.Cart.total"]


def test_class_body_line_matches_the_class():
    symbols = [sym("cart.Cart", 1, 20), sym("cart.Cart.total", 5, 10)]
    result = find_changed_symbols(symbols, PatchLines([3], [3], []))
    assert result.symbols == ["cart.Cart"]


def test_module_level_line_is_outside_symbols():
    result = find_changed_symbols(BILLING, PatchLines([4], [4], []))
    assert result == ChangedSymbols([], True)


def test_insertion_inside_a_function():
    result = find_changed_symbols(BILLING, PatchLines([], [8], [7]))
    assert result == ChangedSymbols(["billing.final_price"], False)


def test_insertion_at_top_of_file_is_outside():
    result = find_changed_symbols(BILLING, PatchLines([], [1], [0]))
    assert result == ChangedSymbols([], True)


def test_insertion_between_functions_is_outside():
    result = find_changed_symbols(BILLING, PatchLines([], [5], [4]))
    assert result == ChangedSymbols([], True)


def test_multiple_changes_are_sorted_and_unique():
    result = find_changed_symbols(BILLING, PatchLines([7, 8, 2], [7, 8, 2], []))
    assert result.symbols == ["billing.calculate_tax", "billing.final_price"]


def test_symbols_without_line_numbers_are_ignored():
    symbols = [{"qualified_name": "x", "start_line": None, "end_line": None}]
    result = find_changed_symbols(symbols, PatchLines([1], [1], []))
    assert result == ChangedSymbols([], True)


def test_no_changes():
    assert find_changed_symbols(BILLING, PatchLines([], [], [])) == ChangedSymbols([], False)