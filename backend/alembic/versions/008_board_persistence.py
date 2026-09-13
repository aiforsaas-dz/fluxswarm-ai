"""P4/2: board & workspace persistence — survive Render free-tier restarts.

Boards (tasks + task_events) and workspace artifacts live on the instance's
local disk (``HERMES_HOME/kanban/boards/<slug>``), which is NOT persistent on
the free host: any sleep/cold-start restart wipes it while the Neon projects
rows survive. These tables let the thin drivers snapshot each board into the
same Postgres that already holds the users, so a fresh boot can restore the
board + its artifacts exactly as they were.

Tables:
  board_states         one row per board slug + kind(project/demo) + seal flag
  board_tasks          the tasks table mirror (same columns as kanban.db)
  board_task_events    the task_events table mirror (same columns)
  board_files          every workspace artifact as ``rel_path`` -> ``content``

Revision ID: 008
Revises: 007
Create Date: 2026-09-13
"""
from alembic import op
import sqlalchemy as sa

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "board_states",
        sa.Column("board_slug", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False, server_default="project"),
        sa.Column("sealed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "board_tasks",
        sa.Column("board_slug", sa.Text(), primary_key=True),
        sa.Column("task_id", sa.Text(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("assignee", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.BigInteger(), nullable=True),
        sa.Column("completed_at", sa.BigInteger(), nullable=True),
        sa.Column("last_heartbeat_at", sa.BigInteger(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("worker_pid", sa.BigInteger(), nullable=True),
    )
    op.create_table(
        "board_task_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("board_slug", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_board_task_events_slug", "board_task_events", ["board_slug", "task_id"])
    op.create_table(
        "board_files",
        sa.Column("board_slug", sa.Text(), primary_key=True),
        sa.Column("rel_path", sa.Text(), primary_key=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("board_files")
    op.drop_index("ix_board_task_events_slug", table_name="board_task_events")
    op.drop_table("board_task_events")
    op.drop_table("board_tasks")
    op.drop_table("board_states")