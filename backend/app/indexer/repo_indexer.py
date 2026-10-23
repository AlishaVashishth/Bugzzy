"""Repository code indexer.

Synchronizes GitHub repository trees into `repo_files`, and extracts symbols
and dependency edges into `code_symbols` and `symbol_edges`.

Importable with zero environment variables set (reads settings lazily).
Uses SQLAlchemy async sessions and existing database engine.
"""

from __future__ import annotations

import base64
import logging
import posixpath
from collections import defaultdict
from typing import Any

import httpx
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_sessionmaker
from app.db.models import CodeSymbol, RepoFile, Repository, SelectedRepository, SymbolEdge
from app.indexer.python_parser import ParseResult, parse_python_source
from app.services import github_app
import builtins

_PYTHON_BUILTINS = frozenset(dir(builtins))

def _is_builtin(name: str) -> bool:
    """True for bare built-in names like round, len, ValueError."""
    return "." not in name and name in _PYTHON_BUILTINS

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
MAX_FILE_SIZE_BYTES = 1024 * 1024  # 1 MB

SKIP_DIRS = {
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    "dist",
    "build",
    ".idea",
    ".vscode",
}

LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".html": "html",
    ".css": "css",
    ".json": "json",
    ".md": "markdown",
    ".txt": "text",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".sql": "sql",
}


def _skip_path(path: str) -> bool:
    return any(part in SKIP_DIRS for part in path.split("/"))


def _to_file_row(repo_id: int, entry: dict, commit_sha: str) -> dict[str, Any]:
    path = entry["path"]
    ext = posixpath.splitext(path)[1].lower()
    is_dir = entry["type"] == "tree"
    return {
        "repo_id": repo_id,
        "path": path,
        "parent_path": posixpath.dirname(path),
        "name": posixpath.basename(path),
        "node_type": "dir" if is_dir else "file",
        "language": None if is_dir else LANGUAGES.get(ext),
        "size_bytes": entry.get("size"),
        "content_hash": entry["sha"],
        "indexed_sha": commit_sha,
    }


async def sync_installation_repos(
    session: AsyncSession, token: str, installation_id: int
) -> list[dict[str, Any]]:
    """Fetch repos accessible by the installation and upsert into repositories table."""
    upserted_repos: list[dict[str, Any]] = []
    page = 1

    async with httpx.AsyncClient(
        base_url=GITHUB_API,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=60.0,
    ) as gh:
        while True:
            resp = await gh.get(
                "/installation/repositories", params={"per_page": 100, "page": page}
            )
            resp.raise_for_status()
            repos = resp.json().get("repositories", [])
            if not repos:
                break

            for repo_data in repos:
                github_repo_id = repo_data["id"]
                owner = repo_data["owner"]["login"]
                name = repo_data["name"]
                default_branch = repo_data.get("default_branch") or "main"

                stmt = (
                    pg_insert(Repository)
                    .values(
                        github_repo_id=github_repo_id,
                        owner=owner,
                        name=name,
                        default_branch=default_branch,
                        installation_id=installation_id,
                    )
                    .on_conflict_do_update(
                        index_elements=[Repository.github_repo_id],
                        set_={
                            "owner": owner,
                            "name": name,
                            "default_branch": default_branch,
                            "installation_id": installation_id,
                        },
                    )
                    .returning(Repository.id, Repository.github_repo_id, Repository.owner, Repository.name, Repository.default_branch)
                )
                res = await session.execute(stmt)
                row = res.one()
                upserted_repos.append(
                    {
                        "id": row.id,
                        "github_repo_id": row.github_repo_id,
                        "owner": row.owner,
                        "name": row.name,
                        "default_branch": row.default_branch,
                    }
                )

            page += 1

    await session.commit()
    return upserted_repos


async def fetch_tree(
    gh: httpx.AsyncClient, owner: str, name: str, branch: str
) -> tuple[str, list[dict]]:
    """Fetch repository git tree via recursive tree endpoint."""
    c = await gh.get(f"/repos/{owner}/{name}/commits/{branch}")
    c.raise_for_status()
    commit_sha = c.json()["sha"]
    tree_sha = c.json()["commit"]["tree"]["sha"]

    t = await gh.get(f"/repos/{owner}/{name}/git/trees/{tree_sha}", params={"recursive": "1"})
    t.raise_for_status()
    data = t.json()
    if data.get("truncated"):
        logger.warning("Tree truncated by GitHub for %s/%s", owner, name)
    return commit_sha, data.get("tree", [])


async def index_repo(session: AsyncSession, token: str, repo_id: int) -> None:
    """Index one repository's file tree and code symbols."""
    repo = (
        await session.execute(select(Repository).where(Repository.id == repo_id))
    ).scalar_one_or_none()
    if not repo:
        raise ValueError(f"Repository with id {repo_id} not found")

    owner, name, branch = repo.owner, repo.name, repo.default_branch

    async with httpx.AsyncClient(
        base_url=GITHUB_API,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=60.0,
    ) as gh:
        commit_sha, tree = await fetch_tree(gh, owner, name, branch)

        entries = [
            e for e in tree if e.get("type") in ("blob", "tree") and not _skip_path(e.get("path", ""))
        ]
        file_rows = [_to_file_row(repo_id, e, commit_sha) for e in entries]
        live_paths = [r["path"] for r in file_rows]

        # 1. Upsert files into repo_files
        for r in file_rows:
            stmt = (
                pg_insert(RepoFile)
                .values(
                    repo_id=r["repo_id"],
                    path=r["path"],
                    parent_path=r["parent_path"],
                    name=r["name"],
                    node_type=r["node_type"],
                    language=r["language"],
                    size_bytes=r["size_bytes"],
                    content_hash=r["content_hash"],
                    indexed_sha=r["indexed_sha"],
                )
                .on_conflict_do_update(
                    index_elements=[RepoFile.repo_id, RepoFile.path],
                    set_={
                        "parent_path": r["parent_path"],
                        "name": r["name"],
                        "node_type": r["node_type"],
                        "language": r["language"],
                        "size_bytes": r["size_bytes"],
                        "content_hash": r["content_hash"],
                        "indexed_sha": r["indexed_sha"],
                        "deleted_at": None,
                        "updated_at": func.now(),
                    },
                    where=(
                        (RepoFile.content_hash.is_distinct_from(r["content_hash"]))
                        | (RepoFile.deleted_at.is_not(None))
                    ),
                )
            )
            await session.execute(stmt)

        # 2. Tombstone disappeared files
        if live_paths:
            await session.execute(
                update(RepoFile)
                .where(
                    RepoFile.repo_id == repo_id,
                    RepoFile.deleted_at.is_(None),
                    ~RepoFile.path.in_(live_paths),
                )
                .values(deleted_at=func.now(), updated_at=func.now())
            )

        # 3. Update last_indexed_sha
        await session.execute(
            update(Repository)
            .where(Repository.id == repo_id)
            .values(last_indexed_sha=commit_sha)
        )
        await session.commit()

        # 4. Index symbols for Python files
        await index_symbols(session, gh, repo_id, commit_sha)


async def _fetch_blob_content(gh: httpx.AsyncClient, owner: str, name: str, blob_sha: str) -> str:
    res = await gh.get(f"/repos/{owner}/{name}/git/blobs/{blob_sha}")
    res.raise_for_status()
    data = res.json()
    encoding = data.get("encoding", "base64")
    raw_content = data.get("content", "")
    if encoding == "base64":
        return base64.b64decode(raw_content).decode("utf-8", errors="replace")
    return raw_content


async def index_symbols(
    session: AsyncSession, gh: httpx.AsyncClient, repo_id: int, commit_sha: str
) -> None:
    """Index functions, classes, and call relationships for changed Python files."""
    repo = (
        await session.execute(select(Repository).where(Repository.id == repo_id))
    ).scalar_one()
    owner, name = repo.owner, repo.name

    # Identify .py files that are new or changed this run, or have 0 symbols
    files_query = text("""
        SELECT f.id, f.path, f.content_hash, f.size_bytes
        FROM repo_files f
        LEFT JOIN code_symbols s ON s.file_id = f.id AND s.deleted_at IS NULL
        WHERE f.repo_id = :repo_id
          AND f.language = 'python'
          AND f.node_type = 'file'
          AND f.deleted_at IS NULL
          AND (f.size_bytes IS NULL OR f.size_bytes <= :max_size)
          AND (f.indexed_sha = :commit_sha OR s.id IS NULL)
        GROUP BY f.id, f.path, f.content_hash, f.size_bytes;
    """)

    res = await session.execute(
        files_query, {"repo_id": repo_id, "commit_sha": commit_sha, "max_size": MAX_FILE_SIZE_BYTES}
    )
    changed_files = [dict(row._mapping) for row in res]
    if not changed_files:
        return

    # Preload existing active repo symbols for edge resolution
    existing_symbols_res = await session.execute(
        select(CodeSymbol.id, CodeSymbol.name, CodeSymbol.qualified_name).where(
            CodeSymbol.repo_id == repo_id, CodeSymbol.deleted_at.is_(None)
        )
    )
    repo_symbols = {row.qualified_name: row.id for row in existing_symbols_res}

    for file_info in changed_files:
        file_id = file_info["id"]
        file_path = file_info["path"]
        content_hash = file_info["content_hash"]

        try:
            source_code = await _fetch_blob_content(gh, owner, name, content_hash)
        except Exception as exc:
            logger.warning("Failed to fetch blob for %s: %s", file_path, exc)
            continue

        try:
            parse_result = parse_python_source(source_code, file_path)
        except SyntaxError as exc:
            logger.warning("Syntax error parsing %s: %s", file_path, exc)
            continue
        except Exception as exc:
            logger.error("Unexpected error parsing %s: %s", file_path, exc)
            continue

        # Upsert symbols for this file
        current_qnames: list[str] = []
        file_symbol_ids: dict[str, int] = {}

        for sym in parse_result.symbols:
            current_qnames.append(sym.qualified_name)
            stmt = (
                pg_insert(CodeSymbol)
                .values(
                    repo_id=repo_id,
                    file_id=file_id,
                    name=sym.name,
                    qualified_name=sym.qualified_name,
                    kind=sym.kind,
                    signature=sym.signature,
                    docstring=sym.docstring,
                    start_line=sym.start_line,
                    end_line=sym.end_line,
                    content_hash=sym.content_hash,
                    deleted_at=None,
                )
                .on_conflict_do_update(
                    index_elements=[CodeSymbol.file_id, CodeSymbol.qualified_name, CodeSymbol.kind],
                    set_={
                        "name": sym.name,
                        "signature": sym.signature,
                        "docstring": sym.docstring,
                        "start_line": sym.start_line,
                        "end_line": sym.end_line,
                        "content_hash": sym.content_hash,
                        "deleted_at": None,
                    },
                )
                .returning(CodeSymbol.id, CodeSymbol.qualified_name)
            )
            ret = await session.execute(stmt)
            row = ret.one()
            file_symbol_ids[row.qualified_name] = row.id
            repo_symbols[row.qualified_name] = row.id

        # Tombstone symbols removed from this file
        if current_qnames:
            await session.execute(
                update(CodeSymbol)
                .where(
                    CodeSymbol.file_id == file_id,
                    CodeSymbol.deleted_at.is_(None),
                    ~CodeSymbol.qualified_name.in_(current_qnames),
                )
                .values(deleted_at=func.now())
            )

        # Delete outgoing edges for this file's symbols
        await session.execute(
            delete(SymbolEdge).where(
                SymbolEdge.source_symbol_id.in_(
                    select(CodeSymbol.id).where(CodeSymbol.file_id == file_id)
                )
            )
        )

        # Insert new edges
        for edge in parse_result.edges:
            source_id = file_symbol_ids.get(edge.source_qualified_name)
            if not source_id:
                continue

            raw_target = getattr(edge, "raw_target", "") or edge.target_name
            target_id = _resolve_target(
                edge.source_qualified_name,
                edge.target_name,
                parse_result.imported_names,
                repo_symbols,
                raw_target=raw_target,
            )
            # Built-ins can never be in the repo, so don't store them as gaps.
            if target_id is None and _is_builtin(raw_target):
                continue

            stmt = (
                pg_insert(SymbolEdge)
                .values(
                    repo_id=repo_id,
                    source_symbol_id=source_id,
                    target_symbol_id=target_id,
                    target_name=edge.target_name,
                    relation=edge.relation,
                    call_line=edge.call_line,
                )
                .on_conflict_do_update(
                    index_elements=[
                        SymbolEdge.source_symbol_id,
                        SymbolEdge.target_name,
                        SymbolEdge.relation,
                        SymbolEdge.call_line,
                    ],
                    set_={"target_symbol_id": target_id},
                )
            )
            await session.execute(stmt)

        await session.commit()

    # Repo-wide pass resolving unresolved edges
    await _resolve_unresolved_edges(session, repo_id)


def _resolve_target(
    source_qname: str,
    target_name: str,
    imported_names: dict[str, str],
    repo_symbols: dict[str, int],
    raw_target: str = "",
) -> int | None:
    """Resolve target symbol id using the 5-step resolution rules."""
    lookup_target = raw_target or target_name

    # 1. Same-class self.method
    if lookup_target.startswith("self."):
        method_name = lookup_target[len("self."):]
        parts = source_qname.split(".")
        if len(parts) >= 2:
            class_qname = ".".join(parts[:-1])
            candidate = f"{class_qname}.{method_name}"
            if candidate in repo_symbols:
                return repo_symbols[candidate]

    # 2. Same-module names
    module_parts = source_qname.split(".")
    if len(module_parts) > 1:
        for i in range(len(module_parts) - 1, 0, -1):
            parent_scope = ".".join(module_parts[:i])
            candidate = f"{parent_scope}.{lookup_target}"
            if candidate in repo_symbols:
                return repo_symbols[candidate]
            candidate_short = f"{parent_scope}.{target_name}"
            if candidate_short in repo_symbols:
                return repo_symbols[candidate_short]

    # 3. Imported names resolved to repo_files paths
    if lookup_target in imported_names:
        imported_target = imported_names[lookup_target]
        if imported_target in repo_symbols:
            return repo_symbols[imported_target]
    if target_name in imported_names:
        imported_target = imported_names[target_name]
        if imported_target in repo_symbols:
            return repo_symbols[imported_target]

    for alias, full_import in imported_names.items():
        if lookup_target.startswith(f"{alias}."):
            remainder = lookup_target[len(f"{alias}."):]
            candidate = f"{full_import}.{remainder}"
            if candidate in repo_symbols:
                return repo_symbols[candidate]

    # 4. Direct exact qualified_name match
    if lookup_target in repo_symbols:
        return repo_symbols[lookup_target]
    if target_name in repo_symbols:
        return repo_symbols[target_name]

    # 5. Unique name match in repo symbols (ONLY for bare calls, not unknown object attributes)
    if "." not in lookup_target:
        short_target = target_name
        matches = [
            sym_id
            for qname, sym_id in repo_symbols.items()
            if qname.endswith(f".{short_target}") or qname == short_target
        ]
        if len(matches) == 1:
            return matches[0]

    return None


async def _resolve_unresolved_edges(session: AsyncSession, repo_id: int) -> None:
    """Repo-wide pass that resolves symbol_edges where target_symbol_id IS NULL,
    and nullifies edges pointing to deleted symbols."""
    # First: Nullify target_symbol_id for edges pointing to soft-deleted or non-existent symbols
    await session.execute(
        update(SymbolEdge)
        .where(
            SymbolEdge.repo_id == repo_id,
            SymbolEdge.target_symbol_id.is_not(None),
            SymbolEdge.target_symbol_id.in_(
                select(CodeSymbol.id).where(
                    CodeSymbol.repo_id == repo_id,
                    CodeSymbol.deleted_at.is_not(None),
                )
            ),
        )
        .values(target_symbol_id=None)
    )

    # Preload active symbols
    symbols_res = await session.execute(
        select(CodeSymbol.id, CodeSymbol.name, CodeSymbol.qualified_name).where(
            CodeSymbol.repo_id == repo_id, CodeSymbol.deleted_at.is_(None)
        )
    )
    repo_symbols = {row.qualified_name: row.id for row in symbols_res}

    # Group by name for unique resolution
    name_to_ids = defaultdict(list)
    for qname, sym_id in repo_symbols.items():
        name = qname.split(".")[-1]
        name_to_ids[name].append(sym_id)

    unresolved_query = select(SymbolEdge.id, SymbolEdge.target_name, SymbolEdge.relation).where(
        SymbolEdge.repo_id == repo_id, SymbolEdge.target_symbol_id.is_(None)
    )
    unresolved_edges = (await session.execute(unresolved_query)).all()

    for edge in unresolved_edges:
        target_name = edge.target_name
        resolved_id = repo_symbols.get(target_name)
        if not resolved_id and edge.relation in ("imports", "inherits"):
            candidates = name_to_ids.get(target_name, [])
            if len(candidates) == 1:
                resolved_id = candidates[0]

        if resolved_id:
            await session.execute(
                update(SymbolEdge)
                .where(SymbolEdge.id == edge.id)
                .values(target_symbol_id=resolved_id)
            )

    await session.commit()


# ---------------------------------------------------------------- Entry point
async def index_user_installation(installation_id: int, user_id: int | None = None) -> None:
    """Background task entry point: index only selected repositories for an installation."""
    maker = get_sessionmaker()
    token = await github_app.get_installation_token(installation_id)

    async with maker() as session:
        # 1. Sync repositories accessible by this installation
        synced_repos = await sync_installation_repos(session, token, installation_id)

        # 2. Filter to ONLY repos in selected_repositories for that user and installation
        if user_id is not None:
            selected_stmt = select(SelectedRepository.repo_id).where(
                SelectedRepository.user_id == user_id,
                SelectedRepository.installation_id == installation_id,
            )
        else:
            selected_stmt = select(SelectedRepository.repo_id).where(
                SelectedRepository.installation_id == installation_id
            )

        selected_github_ids = set((await session.execute(selected_stmt)).scalars().all())

        # If user has specific selections, filter to those; otherwise skip unselected
        target_repos = [
            r for r in synced_repos if r["github_repo_id"] in selected_github_ids
        ]

        logger.info(
            "Indexing %d selected repositories for installation %d (user %s)",
            len(target_repos),
            installation_id,
            user_id,
        )

        for repo_info in target_repos:
            repo_id = repo_info["id"]
            try:
                await index_repo(session, token, repo_id)
                logger.info("Successfully indexed repo %s/%s", repo_info["owner"], repo_info["name"])
            except Exception as exc:
                logger.exception("Failed to index repo %s (id %s): %s", repo_info.get("name"), repo_id, exc)


async def index_single_repository(repo_id: int) -> None:
    """Index or re-index a single repository by its local primary key."""
    maker = get_sessionmaker()
    async with maker() as session:
        repo = (
            await session.execute(select(Repository).where(Repository.id == repo_id))
        ).scalar_one_or_none()
        if not repo:
            raise ValueError(f"Repository {repo_id} not found")
        if not repo.installation_id:
            raise ValueError(f"Repository {repo_id} has no installation_id")

        token = await github_app.get_installation_token(repo.installation_id)
        await index_repo(session, token, repo_id)
