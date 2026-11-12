"""坏记录隔离区服务：按批次/表对/处理状态查询隔离记录。"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from src.constants import error_codes as codes
from src.models.recon import QuarantineRecord, ReconBatch
from src.schemas.quarantine import serialize_quarantine
from src.utils.errors import AppError


def list_quarantine(
    db: Session,
    batch_id: int,
    *,
    pair_code: str | None = None,
    resolved: bool | None = None,
) -> dict[str, Any]:
    batch = db.get(ReconBatch, batch_id)
    if batch is None:
        raise AppError(codes.NOT_FOUND, "批次不存在", f"batch_id={batch_id}")
    query = db.query(QuarantineRecord).filter(QuarantineRecord.batch_id == batch_id)
    if pair_code:
        query = query.filter(QuarantineRecord.pair_code == pair_code)
    if resolved is not None:
        query = query.filter(QuarantineRecord.resolved == (1 if resolved else 0))
    rows = query.order_by(
        QuarantineRecord.pair_code.asc(),
        QuarantineRecord.row_position.asc(),
        QuarantineRecord.id.asc(),
    ).all()
    open_count = (
        db.query(QuarantineRecord)
        .filter(QuarantineRecord.batch_id == batch_id, QuarantineRecord.resolved == 0)
        .count()
    )
    resolved_count = (
        db.query(QuarantineRecord)
        .filter(QuarantineRecord.batch_id == batch_id, QuarantineRecord.resolved == 1)
        .count()
    )
    return {
        "batch_id": batch_id,
        "total": len(rows),
        "open_count": open_count,
        "resolved_count": resolved_count,
        "items": [serialize_quarantine(row) for row in rows],
    }
