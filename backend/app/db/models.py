"""
SQLAlchemy 2.0 models for the GitHub PR Debugger database (Neon / PostgreSQL).

These mirror schema.sql exactly. Suggested location: backend/app/db/models.py
Point Alembic at them:  target_metadata = Base.metadata
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _ts(**kw):
    """timestamptz NOT NULL DEFAULT now()"""
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), **kw)


# =====================================================================
# PART A: core tables
# =====================================================================
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    github_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    encrypted_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="user")
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sessions: Mapped[list["Session"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    installations: Mapped[list["UserInstallation"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("owner", "name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    github_repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    default_branch: Mapped[str] = mapped_column(Text, nullable=False, server_default="main")
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    last_indexed_sha: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()

    files: Mapped[list["RepoFile"]] = relationship(back_populates="repository", cascade="all, delete-orphan")


class RepoFile(Base):
    __tablename__ = "repo_files"
    __table_args__ = (
        UniqueConstraint("repo_id", "path"),
        CheckConstraint("node_type IN ('file','dir')"),
        Index("idx_files_parent", "repo_id", "parent_path"),
        Index("idx_files_lang", "repo_id", "language"),
        Index("idx_files_path_trgm", "path", postgresql_using="gin", postgresql_ops={"path": "gin_trgm_ops"}),
        Index("idx_files_path_prefix", "repo_id", "path", postgresql_ops={"path": "text_pattern_ops"}),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    repo_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    parent_path: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    node_type: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(Text)
    indexed_sha: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # tombstone
    updated_at: Mapped[datetime] = _ts(onupdate=func.now())

    repository: Mapped["Repository"] = relationship(back_populates="files")
    symbols: Mapped[list["CodeSymbol"]] = relationship(back_populates="file", cascade="all, delete-orphan")


class CodeSymbol(Base):
    __tablename__ = "code_symbols"
    __table_args__ = (
        UniqueConstraint("file_id", "qualified_name", "kind"),
        CheckConstraint("kind IN ('function','method','class','module')"),
        Index("idx_sym_repo_name", "repo_id", "name"),
        Index("idx_sym_file", "file_id"),
        Index(
            "idx_sym_qname_trgm",
            "qualified_name",
            postgresql_using="gin",
            postgresql_ops={"qualified_name": "gin_trgm_ops"},
        ),
        Index("idx_sym_search", "search_vec", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    repo_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repo_files.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    qualified_name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str | None] = mapped_column(Text)
    docstring: Mapped[str | None] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(Text)
    # generated column: never write to it, Postgres fills it in
    search_vec = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(qualified_name,'') || ' ' || coalesce(docstring,''))",
            persisted=True,
        ),
        deferred=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    file: Mapped["RepoFile"] = relationship(back_populates="symbols")


class SymbolEdge(Base):
    """source --(calls/imports/inherits/tests)--> target. target is NULL while unresolved."""

    __tablename__ = "symbol_edges"
    __table_args__ = (
        UniqueConstraint("source_symbol_id", "target_name", "relation", "call_line"),
        CheckConstraint("relation IN ('calls','imports','inherits','tests')"),
        Index("idx_edge_target", "target_symbol_id", "relation"),
        Index("idx_edge_source", "source_symbol_id", "relation"),
        Index(
            "idx_edge_unresolved",
            "repo_id",
            "target_name",
            postgresql_where=text("target_symbol_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    repo_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    source_symbol_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("code_symbols.id", ondelete="CASCADE"), nullable=False
    )
    target_symbol_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("code_symbols.id", ondelete="SET NULL")
    )
    target_name: Mapped[str] = mapped_column(Text, nullable=False)
    relation: Mapped[str] = mapped_column(Text, nullable=False)
    call_line: Mapped[int | None] = mapped_column(Integer)


# =====================================================================
# PART B: auth + GitHub App installations
# =====================================================================
class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("idx_sessions_user", "user_id"),
        Index("idx_sessions_expires", "expires_at"),
    )

    # SHA-256 of the cookie value. The raw session id is never stored.
    session_id_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    csrf_token: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts()
    last_seen_at: Mapped[datetime] = _ts()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped["User"] = relationship(back_populates="sessions")


class UserInstallation(Base):
    __tablename__ = "user_installations"
    __table_args__ = (
        CheckConstraint("account_type IN ('User','Organization')"),
        CheckConstraint("repository_selection IN ('all','selected')"),
        Index("idx_user_inst_installation", "installation_id"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    installation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_login: Mapped[str] = mapped_column(Text, nullable=False)
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_type: Mapped[str] = mapped_column(Text, nullable=False)
    repository_selection: Mapped[str] = mapped_column(Text, nullable=False)
    is_connected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts(onupdate=func.now())

    user: Mapped["User"] = relationship(back_populates="installations")
    selected_repositories: Mapped[list["SelectedRepository"]] = relationship(
        back_populates="installation", cascade="all, delete-orphan"
    )


class SelectedRepository(Base):
    __tablename__ = "selected_repositories"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "installation_id"],
            ["user_installations.user_id", "user_installations.installation_id"],
            ondelete="CASCADE",
        ),
        Index("idx_selected_repo", "repo_id"),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    installation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # GitHub's repo id
    created_at: Mapped[datetime] = _ts()

    installation: Mapped["UserInstallation"] = relationship(back_populates="selected_repositories")
