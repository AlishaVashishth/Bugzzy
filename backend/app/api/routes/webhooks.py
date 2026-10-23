"""GitHub webhook receiver.

GitHub calls this endpoint. It verifies the HMAC signature using the webhook
secret from the application settings and hands the real work to background
tasks, so the response is always fast.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import threading
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.config import get_settings
from app.db.engine import get_sessionmaker
from app.db.models import Repository
from app.indexer.repo_indexer import index_single_repository, index_user_installation
from app.services.github_app import get_installation_token
from app.services.pr_files import fetch_pr_files
from app.services.pr_changes import analyze_pr_files, load_symbols_by_path
from app.services.engine_review import run_engine_review
from app.services.pr_comments import upsert_review_comment

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

# Remember recent delivery ids so a redelivered event is not processed twice.
_MAX_DELIVERIES = 1000
_recent_deliveries: OrderedDict[str, None] = OrderedDict()
_lock = threading.Lock()

# Installation actions that must not trigger indexing.
_SKIPPED_ACTIONS = {"deleted", "suspend", "removed"}

# Pull request actions that should start a review.
_REVIEW_ACTIONS = {"opened", "synchronize", "reopened"}


def _is_duplicate(delivery_id: str) -> bool:
    """Return True if this delivery id was seen before; remember it otherwise."""
    with _lock:
        if delivery_id in _recent_deliveries:
            return True
        _recent_deliveries[delivery_id] = None
        while len(_recent_deliveries) > _MAX_DELIVERIES:
            _recent_deliveries.popitem(last=False)
        return False


async def _index_by_github_id(github_repo_id: int) -> None:
    """Find our internal repository id for a GitHub repo id, then index it.

    The webhook payload carries GitHub's repository id, but
    index_single_repository expects our own repositories.id.
    """
    try:
        maker = get_sessionmaker()
        async with maker() as session:
            repo_id = (
                await session.execute(
                    select(Repository.id).where(Repository.github_repo_id == github_repo_id)
                )
            ).scalar_one_or_none()
        if repo_id is None:
            logger.warning(
                "Webhook push for unknown GitHub repository %s; skipping", github_repo_id
            )
            return
        await index_single_repository(repo_id)
    except Exception:
        logger.exception("Indexing after push failed for GitHub repository %s", github_repo_id)


async def _safe_index_user_installation(installation_id: int) -> None:
    """Run index_user_installation, logging errors instead of raising them."""
    try:
        await index_user_installation(installation_id)
    except Exception:
        logger.exception("Indexing failed for installation %s", installation_id)


def _comments_enabled() -> bool:
    """Posting comments to GitHub is opt-in: set BUGZZY_POST_COMMENTS=1."""
    return os.getenv("BUGZZY_POST_COMMENTS", "").strip() == "1"


async def _review_pull_request(github_repo_id: int, pr_number: int, action: str) -> None:
    """Start a review for a pull request.

    Fetches the changed files, works out which indexed symbols they touch,
    runs the context engine, and logs what it finds. Posting results back to
    GitHub plugs in here later.
    """
    try:
        maker = get_sessionmaker()
        async with maker() as session:
            repo = (
                await session.execute(
                    select(
                        Repository.id,
                        Repository.owner,
                        Repository.name,
                        Repository.installation_id,
                    ).where(Repository.github_repo_id == github_repo_id)
                )
            ).one_or_none()
        if repo is None:
            logger.warning("Pull request for unknown GitHub repository %s; skipping", github_repo_id)
            return
        if not repo.installation_id:
            logger.warning("Repository %s/%s has no installation; skipping", repo.owner, repo.name)
            return

        token = await get_installation_token(repo.installation_id)
        files = await fetch_pr_files(token, repo.owner, repo.name, pr_number)

        old_paths = sorted(
            {
                f.get("previous_path") or f["path"]
                for f in files
                if f["path"].endswith(".py") and f.get("status") != "added"
            }
        )
        async with maker() as session:
            symbols_by_path = await load_symbols_by_path(session, repo.id, old_paths)
        changes = analyze_pr_files(files, symbols_by_path)

        logger.info(
            "PR #%s (action=%s) in %s/%s changes %d file(s)",
            pr_number,
            action,
            repo.owner,
            repo.name,
            len(changes),
        )
        for change in changes:
            logger.info(
                "  %s (%s): changed symbols=%s, outside symbols=%s, note=%s",
                change.path,
                change.status,
                change.changed_symbols,
                change.outside_symbols,
                change.note,
            )

        # The engine does blocking network calls, so run it in a worker thread
        # to keep the server responsive for everyone else.
        try:
            findings = await asyncio.to_thread(
                run_engine_review, repo.owner, repo.name, pr_number, token
            )
        except Exception:
            logger.exception(
                "Engine review failed for PR #%s in %s/%s", pr_number, repo.owner, repo.name
            )
            return

        logger.info("Engine found %d potential issue(s) in PR #%s", len(findings), pr_number)
        for f in findings:
            logger.info(
                "  [%s] %s:%s-%s %s (severity=%s, confidence=%s)",
                f["finding_id"],
                f["file_path"],
                f["start_line"],
                f["end_line"],
                f["summary"],
                f["severity"],
                f["confidence"],
            )
        if _comments_enabled():
            try:
                outcome = await upsert_review_comment(
                    token, repo.owner, repo.name, pr_number, findings
                )
                logger.info("Review comment %s on PR #%s", outcome, pr_number)
            except Exception:
                logger.exception("Posting review comment failed for PR #%s", pr_number)
        else:
            logger.info("Comment posting is off (set BUGZZY_POST_COMMENTS=1 to turn it on)")
    except Exception:
        logger.exception("Review failed for PR #%s in repository %s", pr_number, github_repo_id)


@router.post("/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> Any:
    """Receive GitHub webhooks.

    No session or CSRF protection: GitHub calls this directly, and the HMAC
    signature in X-Hub-Signature-256 is the authentication.
    """
    secret = (get_settings().webhook_secret or "").encode("utf-8")
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook secret is not configured",
        )

    signature_header = request.headers.get("X-Hub-Signature-256", "")
    if not signature_header.startswith("sha256="):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    raw_body = await request.body()
    computed = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    provided = signature_header[len("sha256="):]
    if not hmac.compare_digest(computed.encode("utf-8"), provided.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    delivery_id = request.headers.get("X-GitHub-Delivery")
    if delivery_id and _is_duplicate(delivery_id):
        return {"ignored": "duplicate"}

    try:
        payload = json.loads(raw_body)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload")

    event = request.headers.get("X-GitHub-Event", "")

    if event == "ping":
        return {"ok": True}

    if event == "push":
        repo = payload.get("repository") or {}
        default_branch = repo.get("default_branch") or ""
        if not default_branch or payload.get("ref") != f"refs/heads/{default_branch}":
            return {"ignored": "non-default branch"}
        github_repo_id = repo.get("id")
        if not github_repo_id:
            return {"ignored": "no repository id"}
        background_tasks.add_task(_index_by_github_id, github_repo_id)
        return {"queued": "repository indexing"}

    if event in {"installation", "installation_repositories"}:
        if payload.get("action") in _SKIPPED_ACTIONS:
            return {"ignored": f"action {payload.get('action')}"}
        installation_id = (payload.get("installation") or {}).get("id")
        if installation_id is None:
            return {"ignored": "no installation id"}
        background_tasks.add_task(_safe_index_user_installation, installation_id)
        return {"queued": "installation indexing"}

    if event == "pull_request":
        action = payload.get("action")
        if action not in _REVIEW_ACTIONS:
            return {"ignored": f"action {action}"}
        github_repo_id = (payload.get("repository") or {}).get("id")
        pr_number = (payload.get("pull_request") or {}).get("number") or payload.get("number")
        if not github_repo_id or not pr_number:
            return {"ignored": "missing repository id or pull request number"}
        background_tasks.add_task(_review_pull_request, github_repo_id, pr_number, action)
        return JSONResponse(status_code=202, content={"queued": "pull request review"})

    return {"ignored": event}