"""worker lease, shared app settings, relative subtitle paths

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-25
"""

from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _relative_to_data_dir(stored: str | None, data_dir: Path) -> str | None:
    """Absolute path inside DATA_DIR -> relative posix path; anything else is left alone."""
    if not stored:
        return None
    path = Path(stored)
    if not path.is_absolute():
        return None
    try:
        return path.resolve().relative_to(data_dir).as_posix()
    except (ValueError, OSError):
        return None


def upgrade():
    op.add_column("jobs", sa.Column("worker_id", sa.String(128)))
    op.add_column("jobs", sa.Column("lease_expires_at", sa.DateTime()))
    op.create_table(
        "app_settings", sa.Column("key", sa.String(64), primary_key=True), sa.Column("value", sa.Text(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False)
    )

    # Conservative data step: only rewrite absolute paths that live inside the current DATA_DIR.
    from app.config import settings

    data_dir = settings.data_dir.resolve()
    connection = op.get_bind()
    for table, column in (("subtitles", "file_path"), ("videos", "subtitle_path")):
        rows = connection.execute(sa.text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")).fetchall()
        for row_id, stored in rows:
            relative = _relative_to_data_dir(stored, data_dir)
            if relative:
                connection.execute(sa.text(f"UPDATE {table} SET {column} = :value WHERE id = :id"), {"value": relative, "id": row_id})


def downgrade():
    op.drop_table("app_settings")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("lease_expires_at")
        batch.drop_column("worker_id")
