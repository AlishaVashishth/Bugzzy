"""Post the engine's findings as ONE comment on a pull request.

The comment carries a hidden marker. On later pushes we find our own earlier
comment by that marker and edit it, instead of adding a new one each time.
"""

from __future__ import annotations

from typing import Any

import httpx

GITHUB_API = "https://api.github.com"
MARKER = "<!-- bugzzy-review -->"
MAX_FINDINGS_SHOWN = 10
_PER_PAGE = 100
_MAX_PAGES = 30


def _clean(text: Any) -> str:
    """Make text safe to show: no @mentions that would ping people, no backticks."""
    return str(text).replace("@", "@\u200b").replace("`", "'")


def _location(finding: dict[str, Any]) -> str:
    path = _clean(finding["file_path"])
    start, end = finding["start_line"], finding["end_line"]
    if start == end:
        return f"`{path}` line {start}"
    return f"`{path}` lines {start}-{end}"


def format_comment(findings: list[dict[str, Any]]) -> str:
    lines = [MARKER, "## Bugzzy review", ""]
    if not findings:
        lines.append("No potential issues found in the latest changes.")
        return "\n".join(lines)

    count = len(findings)
    lines.append(f"Found {count} potential issue{'s' if count != 1 else ''}:")
    lines.append("")
    for f in findings[:MAX_FINDINGS_SHOWN]:
        severity = _clean(f["severity"]).upper()
        confidence = _clean(f["confidence"])
        lines.append(f"- **{severity}** ({confidence} confidence): {_clean(f['summary'])}")
        lines.append(f"  {_location(f)}")
    if count > MAX_FINDINGS_SHOWN:
        lines.append("")
        lines.append(f"...and {count - MAX_FINDINGS_SHOWN} more.")
    lines.append("")
    lines.append("_Automated static analysis. Please verify before acting._")
    return "\n".join(lines)


async def _find_existing_comment_id(
    gh: httpx.AsyncClient, owner: str, name: str, pr_number: int
) -> int | None:
    """Id of our own earlier comment (marker + posted by a bot), or None."""
    for page in range(1, _MAX_PAGES + 1):
        resp = await gh.get(
            f"/repos/{owner}/{name}/issues/{pr_number}/comments",
            params={"per_page": _PER_PAGE, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        for comment in batch:
            is_bot = (comment.get("user") or {}).get("type") == "Bot"
            if is_bot and MARKER in (comment.get("body") or ""):
                return comment["id"]
        if len(batch) < _PER_PAGE:
            break
    return None


async def upsert_review_comment(
    token: str,
    owner: str,
    name: str,
    pr_number: int,
    findings: list[dict[str, Any]],
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Create or update our comment. Returns "created", "updated" or "skipped"."""
    body = format_comment(findings)
    async with httpx.AsyncClient(
        base_url=GITHUB_API,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30.0,
        transport=transport,
    ) as gh:
        existing_id = await _find_existing_comment_id(gh, owner, name, pr_number)
        if existing_id is None:
            if not findings:
                return "skipped"  # nothing to say and nothing to correct
            resp = await gh.post(
                f"/repos/{owner}/{name}/issues/{pr_number}/comments", json={"body": body}
            )
            resp.raise_for_status()
            return "created"
        resp = await gh.patch(
            f"/repos/{owner}/{name}/issues/comments/{existing_id}", json={"body": body}
        )
        resp.raise_for_status()
        return "updated"