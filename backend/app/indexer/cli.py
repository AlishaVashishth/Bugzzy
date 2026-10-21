"""CLI script to index one repository and display index counts.

Usage:
    python -m app.indexer.cli owner/name
"""

from __future__ import annotations

import asyncio
import sys
from sqlalchemy import select, text

from app.db.engine import get_sessionmaker
from app.db.models import Repository, UserInstallation
from app.indexer.repo_indexer import index_repo
from app.services import github_app


async def main(repo_slug: str) -> None:
    if "/" not in repo_slug:
        print(f"Error: expected format 'owner/name', got '{repo_slug}'")
        sys.exit(1)

    owner, name = repo_slug.split("/", 1)
    maker = get_sessionmaker()

    async with maker() as session:
        # Find existing repo record
        repo = (
            await session.execute(
                select(Repository).where(
                    Repository.owner.ilike(owner), Repository.name.ilike(name)
                )
            )
        ).scalar_one_or_none()

        installation_id = None
        if repo and repo.installation_id:
            installation_id = repo.installation_id
        else:
            # Fall back to first available user installation
            inst = (
                await session.execute(select(UserInstallation.installation_id).limit(1))
            ).scalar_one_or_none()
            if inst:
                installation_id = inst

        if not installation_id:
            print(f"Error: No GitHub installation found for repository {repo_slug}")
            sys.exit(1)

        token = await github_app.get_installation_token(installation_id)

        # Upsert repository row if not present
        if not repo:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            stmt = (
                pg_insert(Repository)
                .values(
                    github_repo_id=abs(hash(f"{owner}/{name}")) % 100000000,
                    owner=owner,
                    name=name,
                    default_branch="main",
                    installation_id=installation_id,
                )
                .on_conflict_do_update(
                    index_elements=[Repository.owner, Repository.name],
                    set_={"installation_id": installation_id},
                )
                .returning(Repository.id)
            )
            res = await session.execute(stmt)
            repo_id = res.scalar_one()
            await session.commit()
        else:
            repo_id = repo.id

        print(f"Indexing repository {owner}/{name} (id={repo_id}, installation={installation_id})...")
        await index_repo(session, token, repo_id)
        print("Indexing completed successfully!")

        # Display counts
        counts_query = text("""
            SELECT 
                (SELECT count(*) FROM repo_files WHERE deleted_at IS NULL) AS files,
                (SELECT count(*) FROM code_symbols) AS symbols,
                (SELECT count(*) FROM symbol_edges) AS edges;
        """)
        counts_res = (await session.execute(counts_query)).first()
        if counts_res:
            print(f"\nCurrent Database Counts:")
            print(f"  files:   {counts_res.files}")
            print(f"  symbols: {counts_res.symbols}")
            print(f"  edges:   {counts_res.edges}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m app.indexer.cli owner/name")
        sys.exit(1)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(main(sys.argv[1]))
