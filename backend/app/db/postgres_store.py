"""PostgreSQL implementation of the ``Store`` interface.

Uses SQLAlchemy 2.0 style (``select`` / ``insert`` / ``delete``) and the
models the teammate already created. Every change that touches more than one
statement runs inside a single transaction.

Safety notes:

* The raw session id is never written; only its SHA-256 digest.
* GitHub user access tokens never reach this module.
* Table structure is owned by the teammate: this file never creates or
  alters tables.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.models import (
    SelectedRepository,
    Session as SessionModel,
    User,
    UserInstallation,
)
from app.services.store import (
    InstallationNotAvailableError,
    Session,
    Store,
    clean_installations,
)

# How often we bother writing last_seen_at. Updating it on every request
# would be a lot of database writes for little benefit.
LAST_SEEN_WRITE_SECONDS = 60


def _aware(value: datetime) -> datetime:
    """Make sure a datetime is timezone-aware (UTC).

    The columns are timestamptz, but being defensive here avoids comparing
    a naive datetime with an aware one.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _session_dict(row: UserInstallation) -> dict:
    """Turn a database row into the plain dict the routes already use."""

    return {
        "installation_id": row.installation_id,
        "account_login": row.account_login,
        "account_id": row.account_id,
        "account_type": row.account_type,
        # The database does not store GitHub's target_type. account_type is
        # the same User/Organization value, so use it to keep the shape.
        "target_type": row.account_type,
        "repository_selection": row.repository_selection,
    }


class PostgresStore(Store):
    """Store backed by the Neon PostgreSQL database."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    # --- Users -----------------------------------------------------------
    async def upsert_user(self, github_user: dict) -> dict:
        github_user_id = github_user.get("id")
        username = github_user.get("login") or ""

        # INSERT ... ON CONFLICT (github_user_id) DO UPDATE. We only write
        # the columns we own; email, encrypted_token and role are left at
        # their defaults. The GitHub token is never stored.
        insert_stmt = pg_insert(User).values(
            github_user_id=github_user_id,
            username=username,
            avatar_url=github_user.get("avatar_url"),
            last_login_at=func.now(),
        )
        statement = insert_stmt.on_conflict_do_update(
            index_elements=[User.github_user_id],
            set_={
                "username": insert_stmt.excluded.username,
                "avatar_url": insert_stmt.excluded.avatar_url,
                "last_login_at": func.now(),
            },
        ).returning(
            User.id, User.github_user_id, User.username, User.avatar_url
        )

        async with self._sessionmaker() as session:
            result = await session.execute(statement)
            row = result.one()
            await session.commit()

        return {
            "id": row.id,
            "github_user_id": row.github_user_id,
            "username": row.username,
            # The users table has no "name" column. Keep the key for the
            # routes, but there is nothing to fill it with.
            "name": None,
            "avatar_url": row.avatar_url,
        }

    async def get_user_by_id(self, local_user_id: int) -> dict | None:
        statement = select(
            User.id, User.github_user_id, User.username, User.avatar_url
        ).where(User.id == local_user_id)

        async with self._sessionmaker() as session:
            row = (await session.execute(statement)).first()

        if row is None:
            return None

        return {
            "id": row.id,
            "github_user_id": row.github_user_id,
            "username": row.username,
            "name": None,
            "avatar_url": row.avatar_url,
        }

    # --- GitHub App installations ---------------------------------------
    async def set_user_installations(
        self, local_user_id: int, installations: list[dict]
    ) -> None:
        cleaned = clean_installations(installations)
        incoming_ids = {int(item["installation_id"]) for item in cleaned}

        async with self._sessionmaker() as session:
            # One transaction: upsert the fresh rows and remove the ones
            # GitHub no longer returned.
            async with session.begin():
                for item in cleaned:
                    installation_id = int(item["installation_id"])

                    insert_stmt = pg_insert(UserInstallation).values(
                        user_id=local_user_id,
                        installation_id=installation_id,
                        account_login=item.get("account_login"),
                        account_id=item.get("account_id"),
                        account_type=item.get("account_type"),
                        repository_selection=item.get("repository_selection"),
                        # New rows must never auto-connect. The database
                        # default is true, so set it explicitly.
                        is_connected=False,
                        updated_at=func.now(),
                    )
                    # On a refresh, update only the profile columns. Leaving
                    # is_connected out preserves the user's choice.
                    statement = insert_stmt.on_conflict_do_update(
                        index_elements=[
                            UserInstallation.user_id,
                            UserInstallation.installation_id,
                        ],
                        set_={
                            "account_login": insert_stmt.excluded.account_login,
                            "account_id": insert_stmt.excluded.account_id,
                            "account_type": insert_stmt.excluded.account_type,
                            "repository_selection": (
                                insert_stmt.excluded.repository_selection
                            ),
                            "updated_at": func.now(),
                        },
                    )
                    await session.execute(statement)

                if incoming_ids:
                    await session.execute(
                        delete(UserInstallation).where(
                            UserInstallation.user_id == local_user_id,
                            UserInstallation.installation_id.notin_(incoming_ids),
                        )
                    )
                else:
                    # GitHub returned nothing: remove all of this user's
                    # installations. The foreign key cascades to selections.
                    await session.execute(
                        delete(UserInstallation).where(
                            UserInstallation.user_id == local_user_id
                        )
                    )

    async def get_user_installations(self, local_user_id: int) -> list[dict]:
        statement = (
            select(UserInstallation)
            .where(UserInstallation.user_id == local_user_id)
            .order_by(UserInstallation.installation_id)
        )

        async with self._sessionmaker() as session:
            rows = (await session.execute(statement)).scalars().all()

        return [_session_dict(row) for row in rows]

    async def set_connected_installation(
        self, local_user_id: int, installation_id: int | None
    ) -> None:
        """Set the user's one connected installation (or clear it with None).

        This is the ONE place that defines what "connected" means: exactly
        one of the user's installation rows has is_connected = true. If the
        teammate later wants different rules, change this function only.
        """

        async with self._sessionmaker() as session:
            async with session.begin():
                current = await session.scalar(
                    select(UserInstallation.installation_id).where(
                        UserInstallation.user_id == local_user_id,
                        UserInstallation.is_connected.is_(True),
                    )
                )

                if installation_id is None:
                    # Disconnecting also drops the selections that belonged
                    # to the installation we are leaving.
                    if current is not None:
                        await session.execute(
                            delete(SelectedRepository).where(
                                SelectedRepository.user_id == local_user_id,
                                SelectedRepository.installation_id == current,
                            )
                        )
                    await session.execute(
                        update(UserInstallation)
                        .where(UserInstallation.user_id == local_user_id)
                        .values(is_connected=False)
                    )
                    return

                # The id must already belong to this user.
                exists = await session.scalar(
                    select(UserInstallation.installation_id).where(
                        UserInstallation.user_id == local_user_id,
                        UserInstallation.installation_id == installation_id,
                    )
                )
                if exists is None:
                    raise InstallationNotAvailableError(
                        "That installation is not in this user's list."
                    )

                # Changing away from a different installation clears its
                # old selections.
                if current is not None and current != installation_id:
                    await session.execute(
                        delete(SelectedRepository).where(
                            SelectedRepository.user_id == local_user_id,
                            SelectedRepository.installation_id == current,
                        )
                    )

                # At most one true per user: clear them all, then set one.
                await session.execute(
                    update(UserInstallation)
                    .where(UserInstallation.user_id == local_user_id)
                    .values(is_connected=False)
                )
                await session.execute(
                    update(UserInstallation)
                    .where(
                        UserInstallation.user_id == local_user_id,
                        UserInstallation.installation_id == installation_id,
                    )
                    .values(is_connected=True, updated_at=func.now())
                )

    async def get_connected_installation(self, local_user_id: int) -> int | None:
        statement = select(UserInstallation.installation_id).where(
            UserInstallation.user_id == local_user_id,
            UserInstallation.is_connected.is_(True),
        )

        async with self._sessionmaker() as session:
            return await session.scalar(statement)

    # --- Selected repositories ------------------------------------------
    async def set_selected_repositories(
        self, local_user_id: int, installation_id: int, repo_ids: list[int]
    ) -> None:
        # dict.fromkeys removes duplicates while keeping the original order.
        unique_ids = list(dict.fromkeys(int(repo_id) for repo_id in repo_ids))

        async with self._sessionmaker() as session:
            async with session.begin():
                # Replace semantics: remove the old selection first.
                await session.execute(
                    delete(SelectedRepository).where(
                        SelectedRepository.user_id == local_user_id,
                        SelectedRepository.installation_id == installation_id,
                    )
                )

                if unique_ids:
                    await session.execute(
                        pg_insert(SelectedRepository),
                        [
                            {
                                "user_id": local_user_id,
                                "installation_id": installation_id,
                                "repo_id": repo_id,
                            }
                            for repo_id in unique_ids
                        ],
                    )

    async def get_selected_repositories(
        self, local_user_id: int, installation_id: int
    ) -> list[int]:
        statement = (
            select(SelectedRepository.repo_id)
            .where(
                SelectedRepository.user_id == local_user_id,
                SelectedRepository.installation_id == installation_id,
            )
            .order_by(SelectedRepository.repo_id)
        )

        async with self._sessionmaker() as session:
            rows = (await session.execute(statement)).scalars().all()

        return list(rows)

    async def clear_selected_repositories(
        self, local_user_id: int, installation_id: int
    ) -> None:
        async with self._sessionmaker() as session:
            await session.execute(
                delete(SelectedRepository).where(
                    SelectedRepository.user_id == local_user_id,
                    SelectedRepository.installation_id == installation_id,
                )
            )
            await session.commit()

    # --- Sessions --------------------------------------------------------
    async def create_session(self, user_id: int) -> tuple[str, str]:
        settings = get_settings()
        raw_session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw_session_id.encode("utf-8")).digest()
        now = datetime.now(timezone.utc)

        row = SessionModel(
            session_id_hash=digest,
            user_id=user_id,
            csrf_token=csrf_token,
            created_at=now,
            last_seen_at=now,
            expires_at=now
            + timedelta(seconds=settings.session_max_age_seconds),
        )

        async with self._sessionmaker() as session:
            session.add(row)
            await session.commit()

        return raw_session_id, csrf_token

    async def get_session(self, raw_session_id: str | None) -> Session | None:
        if not raw_session_id:
            return None

        digest = hashlib.sha256(raw_session_id.encode("utf-8")).digest()
        settings = get_settings()
        now = datetime.now(timezone.utc)

        async with self._sessionmaker() as session:
            row = await session.get(SessionModel, digest)
            if row is None:
                return None

            created_at = _aware(row.created_at)
            last_seen_at = _aware(row.last_seen_at)
            expires_at = _aware(row.expires_at)

            # Absolute lifetime: the row says when it must die.
            if now > expires_at:
                await session.delete(row)
                await session.commit()
                return None

            # Idle timeout: must have been used recently.
            if now - last_seen_at > timedelta(
                seconds=settings.session_idle_timeout_seconds
            ):
                await session.delete(row)
                await session.commit()
                return None

            # Still valid. Only write last_seen_at when it is at least a
            # minute old, to avoid one write per request.
            updated_last_seen = last_seen_at
            if now - last_seen_at > timedelta(seconds=LAST_SEEN_WRITE_SECONDS):
                row.last_seen_at = now
                await session.commit()
                updated_last_seen = now

        return Session(
            user_id=row.user_id,
            csrf_token=row.csrf_token,
            created_at=created_at.timestamp(),
            last_seen_at=updated_last_seen.timestamp(),
        )

    async def delete_session(self, raw_session_id: str | None) -> None:
        if not raw_session_id:
            return

        digest = hashlib.sha256(raw_session_id.encode("utf-8")).digest()

        async with self._sessionmaker() as session:
            await session.execute(
                delete(SessionModel).where(
                    SessionModel.session_id_hash == digest
                )
            )
            await session.commit()

    async def delete_all_sessions_for_user(self, user_id: int) -> int:
        async with self._sessionmaker() as session:
            result = await session.execute(
                delete(SessionModel).where(SessionModel.user_id == user_id)
            )
            await session.commit()

        return result.rowcount or 0

    async def purge_expired_sessions(self) -> int:
        now = datetime.now(timezone.utc)

        async with self._sessionmaker() as session:
            result = await session.execute(
                delete(SessionModel).where(SessionModel.expires_at < now)
            )
            await session.commit()

        return result.rowcount or 0
