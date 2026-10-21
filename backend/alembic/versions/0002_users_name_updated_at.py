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
    op.add_column("users", sa.Column("name", sa.Text(), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "updated_at")
    op.drop_column("users", "name")
