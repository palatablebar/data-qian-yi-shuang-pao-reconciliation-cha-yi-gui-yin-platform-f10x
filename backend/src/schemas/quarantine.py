"""坏记录隔离区序列化与表级结论输出。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from src.constants.enums import PAIR_RUN_STATUS_LABELS, QUARANTINE_REASON_LABELS


def serialize_quarantine(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "batch_id": row.batch_id,
        "pair_code": row.pair_code,
        "source_side": row.source_side,
        "row_position": row.row_position,
        "primary_key_name": row.primary_key_name,
        "primary_key_value": row.primary_key_value,
        "reason": row.reason,
        "reason_label": QUARANTINE_REASON_LABELS.get(row.reason, row.reason),
        "row_snapshot": row.row_snapshot,
        "resolved": bool(row.resolved),
        "created_at": row.created_at.isoformat() if isinstance(row.created_at, datetime) else row.created_at,
        "resolved_at": row.resolved_at.isoformat() if isinstance(row.resolved_at, datetime) else row.resolved_at,
    }


def serialize_pair_result(pair_code: str, entry: dict[str, Any]) -> dict[str, Any]:
    status = str(entry.get("status", "pending"))
    return {
        "pair_code": pair_code,
        "status": status,
        "status_label": PAIR_RUN_STATUS_LABELS.get(status, status),
        "quarantine_count": int(entry.get("quarantine_count", 0) or 0),
        "verified_buckets": int(entry.get("verified_buckets", 0) or 0),
        "mismatched_buckets": int(entry.get("mismatched_buckets", 0) or 0),
        "runs": int(entry.get("runs", 0) or 0),
        "last_run_at": entry.get("last_run_at"),
    }
