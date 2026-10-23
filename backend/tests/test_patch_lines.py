"""Tests for the patch parser. Pure functions: no database, no network."""

from app.services.patch_lines import PatchLines, parse_patch


def _patch(*lines):
    return "\n".join(lines)


def test_real_pr_diff_from_testing_repo():
    patch = _patch(
        "@@ -4,5 +4,5 @@ def calculate_tax(subtotal, rate):",
        " ",
        " ",
        " def final_price(subtotal, rate):",
        "-    tax = calculate_tax(subtotal, rate)",
        "+    tax = calculate_tax(subtotal)",
        "     return subtotal + tax",
    )
    result = parse_patch(patch)
    assert result.removed_old_lines == [7]
    assert result.added_new_lines == [7]
    assert result.insertion_anchors == []


def test_replacement_is_not_an_insertion():
    patch = _patch("@@ -1,3 +1,3 @@", " a", "-b", "+B", " c")
    assert parse_patch(patch) == PatchLines([2], [2], [])


def test_pure_deletion():
    patch = _patch("@@ -1,3 +1,2 @@", " a", "-b", " c")
    assert parse_patch(patch) == PatchLines([2], [], [])


def test_insertion_in_the_middle_has_an_anchor():
    patch = _patch("@@ -1,2 +1,3 @@", " a", "+b", " c")
    assert parse_patch(patch) == PatchLines([], [2], [1])


def test_insertion_at_top_of_file_anchors_at_zero():
    patch = _patch("@@ -0,0 +1,2 @@", "+x", "+y")
    assert parse_patch(patch) == PatchLines([], [1, 2], [0])


def test_insertion_with_zero_old_count_mid_file():
    patch = _patch("@@ -5,0 +6,2 @@", "+x", "+y")
    assert parse_patch(patch) == PatchLines([], [6, 7], [5])


def test_multiple_hunks():
    patch = _patch(
        "@@ -1,2 +1,2 @@",
        "-a",
        "+A",
        " b",
        "@@ -10,2 +10,3 @@",
        " x",
        "+new",
        " y",
    )
    assert parse_patch(patch) == PatchLines([1], [1, 11], [10])


def test_empty_and_missing_patch():
    assert parse_patch(None) == PatchLines([], [], [])
    assert parse_patch("") == PatchLines([], [], [])


def test_no_newline_marker_is_ignored():
    patch = _patch(
        "@@ -1,1 +1,1 @@",
        "-a",
        "\\ No newline at end of file",
        "+b",
        "\\ No newline at end of file",
    )
    assert parse_patch(patch) == PatchLines([1], [1], [])


def test_removed_line_that_looks_like_a_header_still_counts():
    patch = _patch("@@ -1,2 +1,1 @@", "--- note", " keep")
    assert parse_patch(patch) == PatchLines([1], [], [])