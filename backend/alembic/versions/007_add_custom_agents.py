"""P4: custom_agents — user-defined squad members.

Each row is a user-owned agent definition (name + objective + skills). Owners
attach them to a launch via agent_ids; the thin project driver then adds one
lane per custom agent, writing its artifact into the same project workspace.
Users can only ever see/manage their own rows.

Revision ID: 007
Revises: 006
Create Date: 2026-09-13
"""
from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "custom_agents",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("skills", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_custom_agents_user_id_users"),
    )
    op.create_index("ix_custom_agents_user_id", "custom_agents", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_custom_agents_user_id", table_name="custom_agents")
    op.drop_table("custom_agents")