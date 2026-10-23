"""Tests for fetching the files changed in a pull request. No real network."""

import asyncio

import httpx

from app.services.pr_files import fetch_pr_files


def _item(i):
    return {"filename": f"f{i}.py", "status": "modified", "additions": 1, "deletions": 0}


def test_fetch_pr_files_maps_fields():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(
            200,
            json=[
                {
                    "filename": "new.py",
                    "previous_filename": "old.py",
                    "status": "renamed",
                    "additions": 2,
                    "deletions": 3,
                    "patch": "@@ -1 +1 @@",
                }
            ],
        )

    files = asyncio.run(
        fetch_pr_files("tok", "o", "n", 5, transport=httpx.MockTransport(handler))
    )
    assert files == [
        {
            "path": "new.py",
            "status": "renamed",
            "additions": 2,
            "deletions": 3,
            "previous_path": "old.py",
            "patch": "@@ -1 +1 @@",
        }
    ]


def test_fetch_pr_files_follows_pagination():
    def handler(request):
        page = int(request.url.params["page"])
        if page == 1:
            return httpx.Response(200, json=[_item(i) for i in range(100)])
        return httpx.Response(200, json=[_item(100)])

    files = asyncio.run(
        fetch_pr_files("tok", "o", "n", 5, transport=httpx.MockTransport(handler))
    )
    assert len(files) == 101


def test_fetch_pr_files_empty_pr():
    handler = lambda request: httpx.Response(200, json=[])
    files = asyncio.run(
        fetch_pr_files("tok", "o", "n", 5, transport=httpx.MockTransport(handler))
    )
    assert files == []