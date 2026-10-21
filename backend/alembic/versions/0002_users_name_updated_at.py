"""add name and updated_at to users table

Revision ID: 0002_users_name_updated_at
Revises: 0001_baseline
Create Date: 2026-10-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_users_name_updated_at"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS name TEXT;")
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at "
        "TIMESTAMPTZ NOT NULL DEFAULT now();"
    )


def downgrade() -> None:
    pass
