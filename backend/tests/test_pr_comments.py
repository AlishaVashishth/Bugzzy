"""Tests for the PR comment poster. GitHub is replaced by a fake transport."""

import asyncio
import json

import httpx

from app.services.pr_comments import MARKER, format_comment, upsert_review_comment

FINDING = {
    "finding_id": "BUG-001",
    "file_path": "billing.py",
    "start_line": 7,
    "end_line": 7,
    "summary": "Incompatible call to 'calculate_tax': missing required argument(s): 'rate'",
    "severity": "high",
    "confidence": "strong",
}


def _bot(comment_id, body):
    return {"id": comment_id, "body": body, "user": {"type": "Bot"}}


def _human(comment_id, body):
    return {"id": comment_id, "body": body, "user": {"type": "User"}}


def _run(findings, pages, calls):
    def handler(request):
        payload = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, payload))
        if request.method == "GET":
            return httpx.Response(200, json=pages.get(int(request.url.params["page"]), []))
        return httpx.Response(201 if request.method == "POST" else 200, json={})

    return asyncio.run(
        upsert_review_comment(
            "tok", "o", "n", 5, findings, transport=httpx.MockTransport(handler)
        )
    )


def test_format_comment_shows_the_finding():
    body = format_comment([FINDING])
    assert body.startswith(MARKER)
    assert "HIGH" in body and "strong confidence" in body
    assert "missing required argument(s): 'rate'" in body
    assert "`billing.py` line 7" in body


def test_format_comment_defuses_at_mentions():
    finding = dict(FINDING, summary="ping @someone now")
    body = format_comment([finding])
    assert "@someone" not in body
    assert "@\u200bsomeone" in body


def test_format_comment_caps_the_list():
    body = format_comment([dict(FINDING) for _ in range(12)])
    assert body.count("- **HIGH**") == 10
    assert "...and 2 more." in body


def test_format_comment_with_no_findings():
    body = format_comment([])
    assert body.startswith(MARKER)
    assert "No potential issues found" in body


def test_creates_a_comment_when_none_exists():
    calls = []
    assert _run([FINDING], {1: []}, calls) == "created"
    method, path, payload = calls[-1]
    assert (method, path) == ("POST", "/repos/o/n/issues/5/comments")
    assert MARKER in payload["body"]


def test_updates_our_comment_found_on_a_later_page():
    calls = []
    pages = {
        1: [_human(i, "hello") for i in range(100)],
        2: [_bot(99, MARKER + "\nold text")],
    }
    assert _run([FINDING], pages, calls) == "updated"
    method, path, payload = calls[-1]
    assert (method, path) == ("PATCH", "/repos/o/n/issues/comments/99")
    assert "missing required argument" in payload["body"]


def test_ignores_a_marker_copied_by_a_human():
    calls = []
    assert _run([FINDING], {1: [_human(5, MARKER)]}, calls) == "created"
    assert calls[-1][0] == "POST"


def test_skips_when_nothing_to_report_and_nothing_to_fix():
    calls = []
    assert _run([], {1: []}, calls) == "skipped"
    assert [c[0] for c in calls] == ["GET"]


def test_existing_comment_is_corrected_when_issues_disappear():
    calls = []
    assert _run([], {1: [_bot(7, MARKER + "\nold")]}, calls) == "updated"
    assert "No potential issues found" in calls[-1][2]["body"]