"""对账批次、桶结果与坏记录隔离区。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, now


class ReconBatch(Base):
    __tablename__ = "recon_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, index=True)
    pair_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    strategy: Mapped[str] = mapped_column(String(20), nullable=False, default="checksum")
    bucket_count: Mapped[int] = mapped_column(Integer, nullable=False, default=128)
    sample_rate: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    window_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    buckets_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    buckets_matched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    buckets_mismatched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    buckets_empty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    src_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tgt_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skip_rate: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, default=0)
    diff_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quarantine_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # 表级验收结论：{pair_code: {"status": PairRunStatus, "quarantine_count": n,
    #   "verified_buckets": n, "mismatched_buckets": n, "runs": n, "last_run_at": iso}}
    pair_results_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    scan_cost_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False, index=True)


class BucketResult(Base):
    __tablename__ = "bucket_results"
    __table_args__ = (UniqueConstraint("batch_id", "bucket_no", name="uq_bucket_batch_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    bucket_no: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    src_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tgt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    src_checksum: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tgt_checksum: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="match")
    pair_breakdown: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    compared_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)


class QuarantineRecord(Base):
    """坏记录隔离区：不参与桶 checksum、不计入一致，也不阻塞同批其他表出结论。

    - pair_code：坏记录所属表对（表）；
    - source_side：来源端（src / tgt）；
    - row_position：坏记录在原结果集中的位置（从 0 开始的行号，即“原主键位置”）；
    - primary_key_name：该表主键列名（主键为空时仍可定位是哪一列）；
    - primary_key_value：主键值（隔离原因为主键为空时落 NULL）；
    - reason：隔离原因（QuarantineReason）；
    - row_snapshot：坏记录整行快照（JSON，便于修数时核对，不依赖原表行序）；
    - resolved：修数清理后由重跑流程置位（重跑时该位置已无坏记录）。
    """

    __tablename__ = "quarantine_records"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "pair_code",
            "source_side",
            "row_position",
            "reason",
            name="uq_quarantine_batch_pair_pos_reason",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    pair_code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_side: Mapped[str] = mapped_column(String(8), nullable=False, default="src")
    row_position: Mapped[int] = mapped_column(Integer, nullable=False)
    primary_key_name: Mapped[str] = mapped_column(String(64), nullable=False)
    primary_key_value: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reason: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    row_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    resolved: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
