"""Database integration tests for the PostgreSQL store.

These tests only run when TEST_DATABASE_URL is set, and they refuse to run
against the main database. They never create or change tables (no DDL);
the only writes are normal rows, and every test deletes its rows again.

No URL or connection string is ever printed.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.db.engine import build_test_engine, same_database_target

# Read the two URLs from settings. They are never printed.
_settings = get_settings()
TEST_DATABASE_URL = _settings.test_database_url
MAIN_DATABASE_URL = _settings.database_url

# Skip the whole module when no test database is configured.
if not TEST_DATABASE_URL:
    pytest.skip(
        "TEST_DATABASE_URL is not set; skipping database tests.",
        allow_module_level=True,
    )

# Refuse to touch the main database. The message names no URLs.
if MAIN_DATABASE_URL and same_database_target(
    TEST_DATABASE_URL, MAIN_DATABASE_URL
):
    raise RuntimeError(
        "Refusing to run database tests: TEST_DATABASE_URL points at the "
        "same database as DATABASE_URL. Use a separate test database or "
        "Neon branch."
    )

# Imported only after the safety checks above.
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.db.models import (  # noqa: E402
    SelectedRepository,
    Session as SessionModel,
    User,
    UserInstallation,
)
from app.db.postgres_store import PostgresStore  # noqa: E402
from app.main import app  # noqa: E402
from app.services import github_oauth  # noqa: E402
from app.services.store import InMemoryStore, InstallationNotAvailableError, set_store  # noqa: E402

# A separate engine with NullPool, as required for tests.
ENGINE = build_test_engine(TEST_DATABASE_URL)
SessionMaker = async_sessionmaker(ENGINE, expire_on_commit=False)

# Delete children before parents. Only DELETE statements, never DDL.
_CLEANUP_ORDER = (
    "selected_repositories",
    "user_installations",
    "sessions",
    "users",
)


async def _delete_all_rows() -> None:
    async with ENGINE.begin() as connection:
        for table in _CLEANUP_ORDER:
            await connection.execute(text(f"DELETE FROM {table}"))


@pytest_asyncio.fixture()
async def store(force_in_memory_store):
    """A Postgres store wired into the app, with clean tables around it.

    Depending on the shared fixture makes sure it runs first, so this
    fixture is the one that finally replaces the in-memory store.
    """

    await _delete_all_rows()
    pg_store = PostgresStore(SessionMaker)
    set_store(pg_store)
    try:
        yield pg_store
    finally:
        set_store(InMemoryStore())
        await _delete_all_rows()


def _digest(raw_session_id: str) -> bytes:
    return hashlib.sha256(raw_session_id.encode("utf-8")).digest()


async def _make_user(
    store: PostgresStore,
    github_id: int = 1001,
    login: str = "octocat",
    avatar: str = "https://example.invalid/a.png",
) -> dict:
    return await store.upsert_user(
        {"id": github_id, "login": login, "avatar_url": avatar}
    )


async def _installation_rows(user_id: int) -> list[UserInstallation]:
    async with SessionMaker() as session:
        result = await session.execute(
            select(UserInstallation)
            .where(UserInstallation.user_id == user_id)
            .order_by(UserInstallation.installation_id)
        )
        return list(result.scalars().all())


def _installation(
    installation_id: int,
    login: str = "octocat",
    account_id: int = 999,
    account_type: str = "User",
    selection: str = "all",
) -> dict:
    return {
        "installation_id": installation_id,
        "account_login": login,
        "account_id": account_id,
        "account_type": account_type,
        "target_type": account_type,
        "repository_selection": selection,
    }


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_user_twice_updates_one_row(store: PostgresStore) -> None:
    first = await _make_user(store, login="first", avatar="a1")
    second = await _make_user(store, login="second", avatar="a2")

    assert first["id"] == second["id"]

    async with SessionMaker() as session:
        rows = (
            await session.execute(
                select(User).where(User.github_user_id == 1001)
            )
        ).scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.username == "second"
    assert row.avatar_url == "a2"
    assert row.last_login_at is not None
    # We never write these columns on login.
    assert row.email is None
    assert row.encrypted_token is None
    assert row.role == "user"


@pytest.mark.asyncio
async def test_different_github_users_get_different_ids(
    store: PostgresStore,
) -> None:
    first = await _make_user(store, github_id=1, login="one")
    second = await _make_user(store, github_id=2, login="two")

    assert first["id"] != second["id"]
    assert await store.get_user_by_id(first["id"]) is not None
    assert await store.get_user_by_id(second["id"]) is not None


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_create_and_hash(store: PostgresStore) -> None:
    user = await _make_user(store)
    raw, csrf = await store.create_session(user["id"])

    session = await store.get_session(raw)
    assert session is not None
    assert session.user_id == user["id"]
    assert session.csrf_token == csrf

    async with SessionMaker() as db:
        rows = (await db.execute(select(SessionModel))).scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.session_id_hash == _digest(raw)
    assert raw.encode("utf-8") not in row.session_id_hash
    assert raw != row.csrf_token
    assert row.expires_at is not None


@pytest.mark.asyncio
async def test_expired_session_is_rejected_and_deleted(
    store: PostgresStore,
) -> None:
    user = await _make_user(store)
    raw, _ = await store.create_session(user["id"])

    async with SessionMaker() as db:
        row = await db.get(SessionModel, _digest(raw))
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        await db.commit()

    assert await store.get_session(raw) is None

    async with SessionMaker() as db:
        assert await db.get(SessionModel, _digest(raw)) is None


@pytest.mark.asyncio
async def test_idle_session_is_rejected_and_deleted(
    store: PostgresStore,
) -> None:
    user = await _make_user(store)
    raw, _ = await store.create_session(user["id"])

    idle = get_settings().session_idle_timeout_seconds
    now = datetime.now(timezone.utc)

    async with SessionMaker() as db:
        row = await db.get(SessionModel, _digest(raw))
        row.last_seen_at = now - timedelta(seconds=idle + 10)
        row.expires_at = now + timedelta(seconds=3600)
        await db.commit()

    assert await store.get_session(raw) is None

    async with SessionMaker() as db:
        assert await db.get(SessionModel, _digest(raw)) is None


@pytest.mark.asyncio
async def test_last_seen_not_rewritten_within_a_minute(
    store: PostgresStore,
) -> None:
    user = await _make_user(store)
    raw, _ = await store.create_session(user["id"])

    async with SessionMaker() as db:
        before = (await db.get(SessionModel, _digest(raw))).last_seen_at

    assert await store.get_session(raw) is not None

    async with SessionMaker() as db:
        after = (await db.get(SessionModel, _digest(raw))).last_seen_at

    assert after == before


@pytest.mark.asyncio
async def test_delete_session_and_delete_all_for_user(
    store: PostgresStore,
) -> None:
    user = await _make_user(store)
    first, _ = await store.create_session(user["id"])
    second, _ = await store.create_session(user["id"])

    await store.delete_session(first)
    assert await store.get_session(first) is None
    assert await store.get_session(second) is not None

    removed = await store.delete_all_sessions_for_user(user["id"])
    assert removed == 1
    assert await store.get_session(second) is None


@pytest.mark.asyncio
async def test_purge_expired_removes_only_expired_rows(
    store: PostgresStore,
) -> None:
    user = await _make_user(store)
    old_raw, _ = await store.create_session(user["id"])
    new_raw, _ = await store.create_session(user["id"])

    async with SessionMaker() as db:
        row = await db.get(SessionModel, _digest(old_raw))
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

    removed = await store.purge_expired_sessions()
    assert removed == 1

    assert await store.get_session(old_raw) is None
    assert await store.get_session(new_raw) is not None


# --------------------------------------------------------------------------
# Installations
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_installations_replaces_and_new_rows_are_not_connected(
    store: PostgresStore,
) -> None:
    user = await _make_user(store)

    await store.set_user_installations(
        user["id"], [_installation(111), _installation(222)]
    )
    rows = await _installation_rows(user["id"])
    assert [row.installation_id for row in rows] == [111, 222]
    assert all(row.is_connected is False for row in rows)

    # Replace with a different list.
    await store.set_user_installations(
        user["id"], [_installation(222), _installation(333)]
    )
    rows = await _installation_rows(user["id"])
    assert [row.installation_id for row in rows] == [222, 333]
    assert all(row.is_connected is False for row in rows)


@pytest.mark.asyncio
async def test_removed_installation_deletes_its_selections(
    store: PostgresStore,
) -> None:
    user = await _make_user(store)
    await store.set_user_installations(
        user["id"], [_installation(111), _installation(222)]
    )
    await store.set_connected_installation(user["id"], 111)
    await store.set_selected_repositories(user["id"], 111, [5001])

    # 111 is no longer returned by GitHub.
    await store.set_user_installations(user["id"], [_installation(222)])

    assert await store.get_connected_installation(user["id"]) is None
    assert await store.get_selected_repositories(user["id"], 111) == []


@pytest.mark.asyncio
async def test_is_connected_survives_a_refresh(store: PostgresStore) -> None:
    user = await _make_user(store)
    await store.set_user_installations(
        user["id"], [_installation(111), _installation(222)]
    )
    await store.set_connected_installation(user["id"], 111)

    # GitHub returns the same installations again at the next login.
    await store.set_user_installations(
        user["id"], [_installation(111), _installation(222)]
    )

    assert await store.get_connected_installation(user["id"]) == 111
    rows = await _installation_rows(user["id"])
    assert sum(1 for row in rows if row.is_connected) == 1


@pytest.mark.asyncio
async def test_connect_keeps_exactly_one_true(store: PostgresStore) -> None:
    user = await _make_user(store)
    await store.set_user_installations(
        user["id"], [_installation(111), _installation(222)]
    )

    await store.set_connected_installation(user["id"], 111)
    await store.set_connected_installation(user["id"], 222)

    rows = await _installation_rows(user["id"])
    connected = [row.installation_id for row in rows if row.is_connected]
    assert connected == [222]
    assert await store.get_connected_installation(user["id"]) == 222


@pytest.mark.asyncio
async def test_connect_unknown_installation_fails(
    store: PostgresStore,
) -> None:
    user = await _make_user(store)
    await store.set_user_installations(user["id"], [_installation(111)])

    with pytest.raises(InstallationNotAvailableError):
        await store.set_connected_installation(user["id"], 999)


@pytest.mark.asyncio
async def test_disconnect_clears_connection_and_selections(
    store: PostgresStore,
) -> None:
    user = await _make_user(store)
    await store.set_user_installations(user["id"], [_installation(111)])
    await store.set_connected_installation(user["id"], 111)
    await store.set_selected_repositories(user["id"], 111, [5001, 5002])

    await store.set_connected_installation(user["id"], None)

    assert await store.get_connected_installation(user["id"]) is None
    assert await store.get_selected_repositories(user["id"], 111) == []


@pytest.mark.asyncio
async def test_unsupported_account_type_is_skipped(
    store: PostgresStore,
) -> None:
    user = await _make_user(store)

    await store.set_user_installations(
        user["id"],
        [
            _installation(111),
            _installation(222, account_type="Enterprise"),
        ],
    )

    rows = await _installation_rows(user["id"])
    assert [row.installation_id for row in rows] == [111]


@pytest.mark.asyncio
async def test_second_user_is_isolated(store: PostgresStore) -> None:
    first = await _make_user(store, github_id=1, login="one")
    second = await _make_user(store, github_id=2, login="two")

    # The same installation id may exist for both users (composite key).
    await store.set_user_installations(first["id"], [_installation(111)])
    await store.set_user_installations(second["id"], [_installation(111)])

    await store.set_connected_installation(first["id"], 111)
    await store.set_selected_repositories(first["id"], 111, [5001])

    assert await store.get_connected_installation(first["id"]) == 111
    assert await store.get_connected_installation(second["id"]) is None
    assert await store.get_selected_repositories(second["id"], 111) == []


# --------------------------------------------------------------------------
# Selected repositories
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_selected_repositories_replace_and_deduplicate(
    store: PostgresStore,
) -> None:
    user = await _make_user(store)
    await store.set_user_installations(user["id"], [_installation(111)])

    await store.set_selected_repositories(
        user["id"], 111, [5001, 5002, 5002, 5003]
    )
    selected = await store.get_selected_repositories(user["id"], 111)
    assert selected == [5001, 5002, 5003]
    assert all(isinstance(repo_id, int) for repo_id in selected)

    # Replace semantics.
    await store.set_selected_repositories(user["id"], 111, [5003])
    assert await store.get_selected_repositories(user["id"], 111) == [5003]

    # Empty list clears.
    await store.set_selected_repositories(user["id"], 111, [])
    assert await store.get_selected_repositories(user["id"], 111) == []


# --------------------------------------------------------------------------
# End to end through the HTTP app, using the test database
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_end_to_end_login_connect_select_logout(
    store: PostgresStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_exchange(code: str, code_verifier: str | None) -> str:
        return "gho_FAKE_TOKEN_NOT_STORED"

    async def fake_user(access_token: str) -> dict:
        return {
            "id": 7001,
            "login": "e2e-user",
            "name": "E2E User",
            "avatar_url": "https://example.invalid/e2e.png",
        }

    async def fake_installations(access_token: str) -> list[dict]:
        return [_installation(4444, login="e2e-user")]

    async def fake_repositories(
        installation_id: int, force_refresh: bool = False
    ) -> list[dict]:
        return [
            {
                "id": 5555,
                "name": "demo",
                "full_name": "e2e-user/demo",
                "owner_login": "e2e-user",
                "private": False,
                "archived": False,
                "default_branch": "main",
                "html_url": "https://example.invalid/e2e-user/demo",
                "language": "Python",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        ]

    monkeypatch.setattr(github_oauth, "exchange_code_for_token", fake_exchange)
    monkeypatch.setattr(github_oauth, "fetch_github_user", fake_user)
    monkeypatch.setattr(
        github_oauth, "fetch_user_installations", fake_installations
    )
    monkeypatch.setattr(
        "app.api.routes.repositories.list_installation_repositories",
        fake_repositories,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        login = await client.get(
            "/api/v1/auth/github/login", follow_redirects=False
        )
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]

        callback = await client.get(
            "/api/v1/auth/github/callback",
            params={"code": "the-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 302

        # The user and a session row were persisted.
        async with SessionMaker() as db:
            users = (
                await db.execute(
                    select(User).where(User.github_user_id == 7001)
                )
            ).scalars().all()
            assert len(users) == 1
            sessions = (
                await db.execute(
                    select(SessionModel).where(
                        SessionModel.user_id == users[0].id
                    )
                )
            ).scalars().all()
            assert len(sessions) == 1

        me = await client.get("/api/v1/auth/me")
        assert me.status_code == 200
        csrf = me.json()["csrf_token"]

        installations = await client.get("/api/v1/installations")
        assert [
            item["installation_id"]
            for item in installations.json()["installations"]
        ] == [4444]

        connect = await client.post(
            "/api/v1/installations/4444/connect",
            headers={"X-CSRF-Token": csrf},
        )
        assert connect.status_code == 200

        selected = await client.post(
            "/api/v1/repositories/select",
            json={"repository_ids": [5555]},
            headers={"X-CSRF-Token": csrf},
        )
        assert selected.status_code == 200
        assert selected.json()["selected_repository_ids"] == [5555]

        async with SessionMaker() as db:
            stored = (
                await db.execute(select(SelectedRepository))
            ).scalars().all()
            assert [row.repo_id for row in stored] == [5555]

        logout = await client.post(
            "/api/v1/auth/logout", headers={"X-CSRF-Token": csrf}
        )
        assert logout.status_code == 200

        # The session row is gone.
        async with SessionMaker() as db:
            count = (
                await db.execute(
                    select(func.count()).select_from(SessionModel)
                )
            ).scalar_one()
            assert count == 0
