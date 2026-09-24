"""worker heartbeat table

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-25
"""
import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("worker_heartbeats",
                    sa.Column("worker_id", sa.String(128), primary_key=True), sa.Column("hostname", sa.String(255), nullable=False, server_default=""),
                    sa.Column("pid", sa.Integer(), nullable=False, server_default="0"), sa.Column("started_at", sa.DateTime(), nullable=False),
                    sa.Column("last_heartbeat_at", sa.DateTime(), nullable=False), sa.Column("state", sa.String(16), nullable=False, server_default="idle"),
                    sa.Column("current_job_id", sa.String(36)))
    op.create_index("ix_worker_heartbeats_last_heartbeat_at", "worker_heartbeats", ["last_heartbeat_at"])


def downgrade():
    op.drop_index("ix_worker_heartbeats_last_heartbeat_at", "worker_heartbeats")
    op.drop_table("worker_heartbeats")
