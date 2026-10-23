"""Tests for turning PR files + indexed symbols into changed symbols. Pure."""

from app.services.pr_changes import FileChange, analyze_pr_files


def sym(qname, start, end):
    return {"qualified_name": qname, "start_line": start, "end_line": end}


BILLING = {
    "billing.py": [sym("billing.calculate_tax", 1, 3), sym("billing.final_price", 6, 8)]
}

REAL_PATCH = "\n".join(
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


def test_real_pr_finds_final_price():
    files = [{"path": "billing.py", "status": "modified", "patch": REAL_PATCH}]
    assert analyze_pr_files(files, BILLING) == [
        FileChange("billing.py", "modified", ["billing.final_price"], False, None)
    ]


def test_non_python_file_is_skipped():
    files = [{"path": "README.md", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"}]
    result = analyze_pr_files(files, {})
    assert result[0].note == "not a python file"
    assert result[0].changed_symbols == []


def test_new_file_is_reported_as_new():
    files = [{"path": "new.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+x"}]
    assert analyze_pr_files(files, {})[0].note == "new file"


def test_removed_file_lists_all_its_symbols():
    files = [{"path": "billing.py", "status": "removed", "patch": "@@ -1,8 +0,0 @@"}]
    result = analyze_pr_files(files, BILLING)[0]
    assert result.changed_symbols == ["billing.calculate_tax", "billing.final_price"]
    assert result.note == "file removed"


def test_renamed_file_uses_old_path_for_lookup():
    files = [
        {
            "path": "pricing.py",
            "previous_path": "billing.py",
            "status": "renamed",
            "patch": REAL_PATCH,
        }
    ]
    result = analyze_pr_files(files, BILLING)[0]
    assert result.changed_symbols == ["billing.final_price"]


def test_pure_rename_without_patch():
    files = [
        {"path": "pricing.py", "previous_path": "billing.py", "status": "renamed", "patch": None}
    ]
    assert analyze_pr_files(files, BILLING)[0].note == "renamed without content changes"


def test_file_not_in_index():
    files = [{"path": "other.py", "status": "modified", "patch": REAL_PATCH}]
    assert analyze_pr_files(files, BILLING)[0].note == "no indexed symbols for this file"


def test_modified_file_without_patch():
    files = [{"path": "billing.py", "status": "modified", "patch": None}]
    assert analyze_pr_files(files, BILLING)[0].note == "no patch from GitHub"