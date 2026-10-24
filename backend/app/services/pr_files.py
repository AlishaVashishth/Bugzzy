"""Fetch the list of files changed in a pull request."""

from __future__ import annotations

from typing import Any

import httpx

GITHUB_API = "https://api.github.com"
_PER_PAGE = 100
_MAX_PAGES = 30  # GitHub returns at most 3000 files for one pull request


async def fetch_pr_files(
    token: str,
    owner: str,
    name: str,
    pr_number: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Return the files changed in a pull request, one dict per file.

    `transport` exists only so tests can replace the network.
    """
    files: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        base_url=GITHUB_API,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=60.0,
        transport=transport,
    ) as gh:
        for page in range(1, _MAX_PAGES + 1):
            resp = await gh.get(
                f"/repos/{owner}/{name}/pulls/{pr_number}/files",
                params={"per_page": _PER_PAGE, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()
            for item in batch:
                files.append(
                    {
                        "path": item["filename"],
                        "status": item.get("status"),
                        "additions": item.get("additions", 0),
                        "deletions": item.get("deletions", 0),
                        "previous_path": item.get("previous_filename"),
                        "patch": item.get("patch"),
                    }
                )
            if len(batch) < _PER_PAGE:
                break
    return files