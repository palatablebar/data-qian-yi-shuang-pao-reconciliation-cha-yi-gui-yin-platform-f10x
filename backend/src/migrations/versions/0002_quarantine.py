"""坏记录隔离区 + 批次表级验收结论。

注意：0001_initial 用 Base.metadata.create_all 建表，全新库会按当前模型一次性建出
全部表（含本迁移的新列/新表）；而已在旧版本完成 0001 的库则缺少这些对象。
因此本迁移用 inspector 做存在性判断，两种场景都可安全、幂等执行。

Revision ID: 0002_quarantine
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_quarantine"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _existing_columns(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    batch_columns = _existing_columns(bind, "recon_batches")
    if "quarantine_count" not in batch_columns:
        op.add_column(
            "recon_batches",
            sa.Column("quarantine_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if "pair_results_json" not in batch_columns:
        op.add_column(
            "recon_batches",
            sa.Column("pair_results_json", sa.JSON(), nullable=False, server_default="{}"),
        )

    inspector = sa.inspect(bind)
    if "quarantine_records" not in inspector.get_table_names():
        op.create_table(
            "quarantine_records",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("batch_id", sa.Integer(), nullable=False),
            sa.Column("pair_code", sa.String(length=40), nullable=False),
            sa.Column("source_side", sa.String(length=8), nullable=False, server_default="src"),
            sa.Column("row_position", sa.Integer(), nullable=False),
            sa.Column("primary_key_name", sa.String(length=64), nullable=False),
            sa.Column("primary_key_value", sa.String(length=80), nullable=True),
            sa.Column("reason", sa.String(length=40), nullable=False),
            sa.Column("row_snapshot", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("resolved", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint(
                "batch_id",
                "pair_code",
                "source_side",
                "row_position",
                "reason",
                name="uq_quarantine_batch_pair_pos_reason",
            ),
        )
        op.create_index("ix_quarantine_records_batch_id", "quarantine_records", ["batch_id"])
        op.create_index("ix_quarantine_records_pair_code", "quarantine_records", ["pair_code"])
        op.create_index("ix_quarantine_records_reason", "quarantine_records", ["reason"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "quarantine_records" in inspector.get_table_names():
        op.drop_index("ix_quarantine_records_reason", table_name="quarantine_records")
        op.drop_index("ix_quarantine_records_pair_code", table_name="quarantine_records")
        op.drop_index("ix_quarantine_records_batch_id", table_name="quarantine_records")
        op.drop_table("quarantine_records")
    batch_columns = _existing_columns(bind, "recon_batches")
    if "pair_results_json" in batch_columns:
        op.drop_column("recon_batches", "pair_results_json")
    if "quarantine_count" in batch_columns:
        op.drop_column("recon_batches", "quarantine_count")
