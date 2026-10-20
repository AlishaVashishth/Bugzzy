"""baseline: users, repositories, code index, auth/installation tables

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-19

Suggested location: backend/alembic/versions/0001_baseline.py

- Fresh database / new Neon branch:   alembic upgrade head
- The existing Neon database (tables already created from schema.sql):
                                       alembic stamp head
"""
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

# One statement per entry (works with every Postgres driver).
STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",

    # ---- Part A: core ----
    """
    CREATE TABLE users (
        id               BIGSERIAL PRIMARY KEY,
        github_user_id   BIGINT NOT NULL UNIQUE,
        username         TEXT NOT NULL,
        email            TEXT,
        avatar_url       TEXT,
        encrypted_token  BYTEA,
        role             TEXT NOT NULL DEFAULT 'user',
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_login_at    TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE repositories (
        id                BIGSERIAL PRIMARY KEY,
        github_repo_id    BIGINT NOT NULL UNIQUE,
        owner             TEXT NOT NULL,
        name              TEXT NOT NULL,
        default_branch    TEXT NOT NULL DEFAULT 'main',
        installation_id   BIGINT,
        last_indexed_sha  TEXT,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (owner, name)
    )
    """,
    """
    CREATE TABLE repo_files (
        id            BIGSERIAL PRIMARY KEY,
        repo_id       BIGINT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
        path          TEXT NOT NULL,
        parent_path   TEXT NOT NULL,
        name          TEXT NOT NULL,
        node_type     TEXT NOT NULL CHECK (node_type IN ('file','dir')),
        language      TEXT,
        description   TEXT,
        size_bytes    INT,
        content_hash  TEXT,
        indexed_sha   TEXT,
        deleted_at    TIMESTAMPTZ,
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (repo_id, path)
    )
    """,
    "CREATE INDEX idx_files_parent      ON repo_files (repo_id, parent_path)",
    "CREATE INDEX idx_files_lang        ON repo_files (repo_id, language)",
    "CREATE INDEX idx_files_path_trgm   ON repo_files USING gin (path gin_trgm_ops)",
    "CREATE INDEX idx_files_path_prefix ON repo_files (repo_id, path text_pattern_ops)",
    """
    CREATE TABLE code_symbols (
        id              BIGSERIAL PRIMARY KEY,
        repo_id         BIGINT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
        file_id         BIGINT NOT NULL REFERENCES repo_files(id)   ON DELETE CASCADE,
        name            TEXT NOT NULL,
        qualified_name  TEXT NOT NULL,
        kind            TEXT NOT NULL CHECK (kind IN ('function','method','class','module')),
        signature       TEXT,
        docstring       TEXT,
        start_line      INT NOT NULL,
        end_line        INT NOT NULL,
        content_hash    TEXT,
        search_vec      TSVECTOR GENERATED ALWAYS AS (
            to_tsvector('simple', coalesce(qualified_name,'') || ' ' || coalesce(docstring,''))
        ) STORED,
        deleted_at      TIMESTAMPTZ,
        UNIQUE (file_id, qualified_name, kind)
    )
    """,
    "CREATE INDEX idx_sym_repo_name  ON code_symbols (repo_id, name)",
    "CREATE INDEX idx_sym_file       ON code_symbols (file_id)",
    "CREATE INDEX idx_sym_qname_trgm ON code_symbols USING gin (qualified_name gin_trgm_ops)",
    "CREATE INDEX idx_sym_search     ON code_symbols USING gin (search_vec)",
    """
    CREATE TABLE symbol_edges (
        id                BIGSERIAL PRIMARY KEY,
        repo_id           BIGINT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
        source_symbol_id  BIGINT NOT NULL REFERENCES code_symbols(id) ON DELETE CASCADE,
        target_symbol_id  BIGINT REFERENCES code_symbols(id) ON DELETE SET NULL,
        target_name       TEXT NOT NULL,
        relation          TEXT NOT NULL CHECK (relation IN ('calls','imports','inherits','tests')),
        call_line         INT,
        UNIQUE (source_symbol_id, target_name, relation, call_line)
    )
    """,
    "CREATE INDEX idx_edge_target ON symbol_edges (target_symbol_id, relation)",
    "CREATE INDEX idx_edge_source ON symbol_edges (source_symbol_id, relation)",
    "CREATE INDEX idx_edge_unresolved ON symbol_edges (repo_id, target_name) WHERE target_symbol_id IS NULL",

    # ---- Part B: auth + installations ----
    """
    CREATE TABLE sessions (
        session_id_hash  BYTEA PRIMARY KEY,
        user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf_token       TEXT NOT NULL,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at       TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX idx_sessions_user    ON sessions (user_id)",
    "CREATE INDEX idx_sessions_expires ON sessions (expires_at)",
    """
    CREATE TABLE user_installations (
        user_id               BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        installation_id       BIGINT NOT NULL,
        account_login         TEXT NOT NULL,
        account_id            BIGINT NOT NULL,
        account_type          TEXT NOT NULL CHECK (account_type IN ('User','Organization')),
        repository_selection  TEXT NOT NULL CHECK (repository_selection IN ('all','selected')),
        is_connected          BOOLEAN NOT NULL DEFAULT true,
        created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, installation_id)
    )
    """,
    "CREATE INDEX idx_user_inst_installation ON user_installations (installation_id)",
    """
    CREATE TABLE selected_repositories (
        user_id          BIGINT NOT NULL,
        installation_id  BIGINT NOT NULL,
        repo_id          BIGINT NOT NULL,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, installation_id, repo_id),
        FOREIGN KEY (user_id, installation_id)
            REFERENCES user_installations (user_id, installation_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX idx_selected_repo ON selected_repositories (repo_id)",
]


def upgrade() -> None:
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    # Reverse dependency order. The pg_trgm extension is left installed.
    for table in (
        "selected_repositories",
        "user_installations",
        "sessions",
        "symbol_edges",
        "code_symbols",
        "repo_files",
        "repositories",
        "users",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
