"""算法 A · 分桶对账（recon_engine）。

输入：批次（表对集合、时间窗、桶数 N∈{64,128,256}、抽样率）。
步骤：
1. 主键 → 稳定哈希：bucket = int(md5(pk)[:8], 16) mod N，跨表对共用同一桶空间；
2. 每桶计算 count 与多列 checksum：行级 h = md5('|'.join(列值规范化))，
   桶级 checksum = sum(int(h[:8], 16)) mod 2^32（可复现、与行序无关）；
3. 桶内 count 与 checksum 都相等 → match（跳过，不抽样）；
4. 不一致桶：缺失/多出行按主键行集比对必然暴露（全量），字段级值差异按抽样率
   由 int(md5(pk + '#sample')[:8], 16) % 100 < 抽样率 命中后下钻到 diff_engine；
5. 输出桶矩阵、跳过率 = match 桶数 / 总桶数，并估算全量扫描成本 = 桶数 × 单桶平均耗时。

坏记录隔离（v2）：源表主键含 NULL 不再整批 failed——坏记录带「所属表对 / 原主键位置 /
隔离原因 / 整行快照」落入 quarantine_records，不参与 checksum、不计入一致；同批其他表
正常出结论。批次状态：全部表可验收 → completed；存在含隔离记录（或阻塞）的表 → partial。
清理隔离项后重跑只重算受影响表，已完成表重复执行结论与计数保持不变（幂等、不重复计数）。
空桶（0/0）记 empty，不计不一致；主键重复在 diff_engine 中标 duplicate_key（不参与相似度）。
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.database import session_scope
from src.constants.enums import (
    BatchStatus,
    BucketStatus,
    PairRunStatus,
    QuarantineReason,
)
from src.models.base import now
from src.models.catalog import TablePair
from src.models.demo_tables import DEMO_TABLES
from src.models.recon import BucketResult, QuarantineRecord, ReconBatch
from src.models.trace import DiffDetail
from src.services import audit_service, diff_engine
from src.utils.hashing import bucket_index, checksum_int, is_sampled, row_checksum
from src.utils.logger import get_logger

MOD32 = 1 << 32
logger = get_logger("paritycheck.recon_engine")


@dataclass
class BucketStat:
    src_count: int = 0
    tgt_count: int = 0
    src_sum: int = 0
    tgt_sum: int = 0
    pair_stats: dict[str, dict[str, int]] = field(default_factory=dict)

    def pair(self, code: str) -> dict[str, int]:
        if code not in self.pair_stats:
            self.pair_stats[code] = {"src_count": 0, "tgt_count": 0, "src_sum": 0, "tgt_sum": 0}
        return self.pair_stats[code]


def _timestamp_column(columns: list[str]) -> str | None:
    for name in ("created_at", "paid_at"):
        if name in columns:
            return name
    return None


def _json_safe(value: Any) -> Any:
    """把行值转成可落 JSON 列的原生类型（datetime / Decimal 无法直接进 JSON）。"""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return value


def load_pair_rows(db: Session, pair: TablePair, window_days: int) -> dict[str, Any]:
    """读取一个表对在时间窗内的源行/目标行（只要 field_rules 里的列）。"""
    src_table = DEMO_TABLES[pair.src_table]
    tgt_table = DEMO_TABLES[pair.tgt_table]
    columns = [str(rule["name"]) for rule in pair.field_rules or []]
    ts_column = _timestamp_column(columns)
    cutoff = datetime.now() - timedelta(days=window_days)

    src_stmt = select(*[src_table.c[name] for name in columns])
    tgt_stmt = select(*[tgt_table.c[name] for name in columns])
    if ts_column:
        src_stmt = src_stmt.where(src_table.c[ts_column] >= cutoff)
        tgt_stmt = tgt_stmt.where(tgt_table.c[ts_column] >= cutoff)

    src_rows = [dict(row._mapping) for row in db.execute(src_stmt).all()]
    tgt_rows = [dict(row._mapping) for row in db.execute(tgt_stmt).all()]
    return {
        "pair": pair,
        "columns": columns,
        "src_rows": src_rows,
        "tgt_rows": tgt_rows,
    }


def quarantine_null_primary_keys(
    db: Session,
    batch: ReconBatch,
    pair: TablePair,
    rows: list[dict[str, Any]],
    *,
    source_side: str = "src",
) -> tuple[list[dict[str, Any]], list[QuarantineRecord]]:
    """隔离主键为空的坏记录：返回 (干净行, 本次新落库的隔离记录)。

    坏记录不参与分桶 checksum；隔离项带表对、来源端、原主键位置（结果集行号）、
    隔离原因与整行快照。唯一键 (batch, pair, side, row_position, reason) 保证重跑不重复落库。
    """
    clean: list[dict[str, Any]] = []
    added: list[QuarantineRecord] = []
    for position, row in enumerate(rows):
        if row.get(pair.primary_key) is not None:
            clean.append(row)
            continue
        existing = (
            db.query(QuarantineRecord)
            .filter(
                QuarantineRecord.batch_id == batch.id,
                QuarantineRecord.pair_code == pair.code,
                QuarantineRecord.source_side == source_side,
                QuarantineRecord.row_position == position,
                QuarantineRecord.reason == QuarantineReason.NULL_PRIMARY_KEY.value,
            )
            .one_or_none()
        )
        if existing is None:
            record = QuarantineRecord(
                batch_id=batch.id,
                pair_code=pair.code,
                source_side=source_side,
                row_position=position,
                primary_key_name=pair.primary_key,
                primary_key_value=None,
                reason=QuarantineReason.NULL_PRIMARY_KEY.value,
                row_snapshot={name: _json_safe(value) for name, value in row.items()},
                resolved=0,
            )
            db.add(record)
            db.flush()
            added.append(record)
            logger.warning(
                "bad row quarantined batch_id=%s pair=%s side=%s position=%s reason=%s",
                batch.id,
                pair.code,
                source_side,
                position,
                QuarantineReason.NULL_PRIMARY_KEY.value,
            )
    return clean, added


def resolve_cleared_quarantine(
    db: Session,
    batch: ReconBatch,
    pair: TablePair,
    rows: list[dict[str, Any]],
    *,
    source_side: str = "src",
) -> list[QuarantineRecord]:
    """重跑时把该表已修数（原主键位置上的行已恢复非空主键）的隔离项标记 resolved。

    判定口径与 load_pair_rows 的结果集一致（含相同时间窗）：仍处于未解决状态的隔离项，
    若其原位置上的行已恢复非空主键，说明修数生效；若原位置行已被删除则保留待人工确认。
    """
    open_records = (
        db.query(QuarantineRecord)
        .filter(
            QuarantineRecord.batch_id == batch.id,
            QuarantineRecord.pair_code == pair.code,
            QuarantineRecord.source_side == source_side,
            QuarantineRecord.resolved == 0,
        )
        .all()
    )
    resolved: list[QuarantineRecord] = []
    for record in open_records:
        if record.row_position >= len(rows):
            continue  # 原位置行已不存在（删行修数），隔离项保留待人工确认
        current = rows[record.row_position]
        if current.get(pair.primary_key) is not None:
            record.resolved = 1
            record.resolved_at = now()
            resolved.append(record)
    return resolved


def _mark_failed(db: Session, batch: ReconBatch, reason: str, detail: dict[str, Any] | None = None) -> None:
    batch.status = BatchStatus.FAILED.value
    batch.failure_reason = reason
    batch.finished_at = now()
    if detail:
        batch.params_json = {**(batch.params_json or {}), "failure_detail": detail}
    db.commit()
    audit_service.record(
        db,
        action="batch.failed",
        entity_type="ReconBatch",
        entity_id=str(batch.id),
        actor_id=batch.created_by,
        actor_kind="system",
        payload={"reason": reason, "detail": detail or {}},
    )
    logger.warning("batch failed batch_id=%s reason=%s", batch.id, reason)


def _pair_bucket_status(pair_stat: dict[str, int] | None) -> str:
    if not pair_stat or (pair_stat["src_count"] == 0 and pair_stat["tgt_count"] == 0):
        return BucketStatus.EMPTY.value
    if pair_stat["src_count"] == pair_stat["tgt_count"] and pair_stat["src_sum"] == pair_stat["tgt_sum"]:
        return BucketStatus.MATCH.value
    return BucketStatus.MISMATCH.value


def _drill_bucket(
    pair: TablePair,
    columns: list[str],
    src_rows: list[dict[str, Any]],
    tgt_rows: list[dict[str, Any]],
    *,
    bucket_no: int,
    sample_rate: int,
) -> list[dict[str, Any]]:
    """不一致桶下钻：缺失/多出行全量暴露；值差异按确定性抽样率抽行做字段级 diff。"""
    pk_name = pair.primary_key
    src_map: dict[str, dict[str, Any]] = {}
    tgt_map: dict[str, dict[str, Any]] = {}
    duplicates: list[tuple[str, str]] = []

    for row in src_rows:
        key = str(row.get(pk_name))
        if key in src_map:
            duplicates.append((key, "src"))
        else:
            src_map[key] = row
    for row in tgt_rows:
        key = str(row.get(pk_name))
        if key in tgt_map:
            duplicates.append((key, "tgt"))
        else:
            tgt_map[key] = row

    diffs: list[dict[str, Any]] = []
    for key, row in src_map.items():
        if key not in tgt_map:
            diffs.append(diff_engine.missing_row_diff(pair, key, bucket_no=bucket_no))
            continue
        if not is_sampled(key, sample_rate):
            continue
        diffs.extend(diff_engine.compare_rows(pair, row, tgt_map[key], bucket_no=bucket_no))
    for key in tgt_map:
        if key not in src_map:
            diffs.append(diff_engine.missing_row_diff(pair, key, bucket_no=bucket_no, extra=True))
    seen: set[tuple[str, str]] = set()
    for key, side in duplicates:
        if (key, side) in seen:
            continue
        seen.add((key, side))
        diffs.append(diff_engine.duplicate_key_diff(pair, key, bucket_no=bucket_no, side=side))
    return diffs


def _active_quarantine_counts(db: Session, batch_id: int) -> dict[str, int]:
    rows = (
        db.query(QuarantineRecord.pair_code)
        .filter(QuarantineRecord.batch_id == batch_id, QuarantineRecord.resolved == 0)
        .all()
    )
    counts: dict[str, int] = defaultdict(int)
    for (pair_code,) in rows:
        counts[pair_code] += 1
    return dict(counts)


def run_batch(batch_id: int, bucket_limit: int | None = None) -> dict[str, Any]:
    """执行一个批次（Celery worker / seed 同步调用共用）。

    隔离语义：坏记录只隔离其所属表，其他表照常出结论；重跑只重算受影响表，
    已完成表的桶结果、差异与计数保持不变（重复执行不重复计数）。
    bucket_limit 用于演示 running 状态（只算前 N 个桶）。
    """
    started_at = time.perf_counter()
    with session_scope() as db:
        batch = db.get(ReconBatch, batch_id)
        if batch is None:
            return {"ok": False, "reason": "batch_not_found", "batch_id": batch_id}
        if batch.status == BatchStatus.COMPLETED.value and not batch.pair_results_json:
            return {"ok": True, "batch_id": batch.id, "status": batch.status, "idempotent": True}

        pairs = db.query(TablePair).filter(TablePair.code.in_(batch.pair_codes or [])).all()
        if not pairs:
            _mark_failed(db, batch, "no_pairs")
            return {"ok": False, "reason": "no_pairs", "batch_id": batch.id}

        # 历史结论与未解决隔离项必须在任何 commit（audit_service.record 也会提交）之前快照，
        # 避免 session 过期后读到空值，导致“只重算受影响表”的跳过判断失效。
        prior_results: dict[str, Any] = dict(batch.pair_results_json or {})
        open_quarantine = _active_quarantine_counts(db, batch.id)

        # 1) 决定本轮真正重算的表（受影响表），必须在置 running 之前判定：
        #    - 首轮（无历史结论）：全部 active 表参与；
        #    - 重跑：仅重算「含未解决隔离记录」或「结论非 verified」的表；
        #      已 verified 且无未解决隔离项的表直接跳过，桶结果/差异/计数原样保留，天然不重复计数。
        blocked_codes: list[str] = []
        skipped_codes: list[str] = []
        to_load: list[TablePair] = []
        for pair in pairs:
            prior = prior_results.get(pair.code) or {}
            if pair.status != "active":
                # 表对被暂停属于配置级阻塞：不拖垮其他表，仅该表标 blocked。
                blocked_codes.append(pair.code)
                logger.warning("pair inactive, mark blocked batch_id=%s pair=%s", batch.id, pair.code)
                continue
            if (
                prior
                and prior.get("status") == PairRunStatus.VERIFIED.value
                and open_quarantine.get(pair.code, 0) == 0
                and bucket_limit is None
            ):
                skipped_codes.append(pair.code)
                logger.info("pair already verified, skip rerun batch_id=%s pair=%s", batch.id, pair.code)
                continue
            to_load.append(pair)

        if not to_load:
            # 全部表均已验收（或阻塞）且无未解决隔离项：幂等返回，状态/桶结果/差异/计数一律不动。
            logger.info(
                "no affected pair, idempotent skip batch_id=%s skipped=%s blocked=%s",
                batch.id,
                skipped_codes,
                blocked_codes,
            )
            return {
                "ok": True,
                "batch_id": batch.id,
                "status": batch.status,
                "idempotent": True,
                "skipped_pairs": skipped_codes,
                "reran_pairs": [],
            }

        batch.status = BatchStatus.RUNNING.value
        batch.started_at = batch.started_at or now()
        batch.failure_reason = None
        db.commit()

        loaded: list[dict[str, Any]] = []
        for pair in to_load:
            data = load_pair_rows(db, pair, batch.window_days)
            resolve_cleared_quarantine(db, batch, pair, data["src_rows"])
            clean_src, added = quarantine_null_primary_keys(db, batch, pair, data["src_rows"])
            if added:
                audit_service.record(
                    db,
                    action="batch.quarantined",
                    entity_type="ReconBatch",
                    entity_id=str(batch.id),
                    actor_id=batch.created_by,
                    actor_kind="system",
                    payload={
                        "pair_code": pair.code,
                        "reason": QuarantineReason.NULL_PRIMARY_KEY.value,
                        "rows": len(added),
                        "positions": [item.row_position for item in added],
                    },
                )
            data["src_rows"] = clean_src
            data["quarantined"] = len(added)
            loaded.append(data)
        db.flush()

        total_buckets = batch.bucket_count if bucket_limit is None else min(bucket_limit, batch.bucket_count)
        stats: dict[int, BucketStat] = defaultdict(BucketStat)
        pair_rows: dict[str, dict[str, dict[int, list[dict[str, Any]]]]] = {}
        affected_codes = [data["pair"].code for data in loaded]

        # 2) 受影响表分桶（坏记录已剔除，不参与 count/checksum）。
        for data in loaded:
            pair: TablePair = data["pair"]
            columns: list[str] = data["columns"]
            src_buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
            tgt_buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for row in data["src_rows"]:
                bucket = bucket_index(row[pair.primary_key], batch.bucket_count)
                if bucket >= total_buckets:
                    continue
                row_hash = checksum_int(row_checksum([row[name] for name in columns]))
                stat = stats[bucket]
                stat.src_count += 1
                stat.src_sum = (stat.src_sum + row_hash) % MOD32
                pair_stat = stat.pair(pair.code)
                pair_stat["src_count"] += 1
                pair_stat["src_sum"] = (pair_stat["src_sum"] + row_hash) % MOD32
                src_buckets[bucket].append(row)
            for row in data["tgt_rows"]:
                bucket = bucket_index(row[pair.primary_key], batch.bucket_count)
                if bucket >= total_buckets:
                    continue
                row_hash = checksum_int(row_checksum([row[name] for name in columns]))
                stat = stats[bucket]
                stat.tgt_count += 1
                stat.tgt_sum = (stat.tgt_sum + row_hash) % MOD32
                pair_stat = stat.pair(pair.code)
                pair_stat["tgt_count"] += 1
                pair_stat["tgt_sum"] = (pair_stat["tgt_sum"] + row_hash) % MOD32
                tgt_buckets[bucket].append(row)
            pair_rows[pair.code] = {"src": src_buckets, "tgt": tgt_buckets}

        # 3) 增量重算受影响桶：先剥离这些表在旧桶结果里的贡献，再合并新结果。
        existing_buckets = {
            item.bucket_no: item
            for item in db.query(BucketResult).filter(BucketResult.batch_id == batch.id).all()
        }
        diff_payloads: list[dict[str, Any]] = []
        matched = mismatched = empty = 0
        src_total = tgt_total = 0
        bucket_elapsed: list[float] = []
        new_bucket_rows: list[BucketResult] = []

        for bucket_no in range(total_buckets):
            tick = time.perf_counter()
            old = existing_buckets.get(bucket_no)
            breakdown: list[dict[str, Any]] = [dict(item) for item in (old.pair_breakdown if old else [])]
            breakdown = [item for item in breakdown if item["pair_code"] not in affected_codes]
            base_src_count = sum(item["src_count"] for item in breakdown)
            base_tgt_count = sum(item["tgt_count"] for item in breakdown)
            base_src_sum = sum(item["src_checksum"] for item in breakdown) % MOD32
            base_tgt_sum = sum(item["tgt_checksum"] for item in breakdown) % MOD32

            stat = stats.get(bucket_no) or BucketStat()
            bucket_status = BucketStatus.EMPTY.value
            for data in loaded:
                pair = data["pair"]
                pair_stat = stat.pair_stats.get(pair.code)
                p_status = _pair_bucket_status(pair_stat)
                if p_status == BucketStatus.MISMATCH.value:
                    bucket_status = BucketStatus.MISMATCH.value
                    diff_payloads.extend(
                        _drill_bucket(
                            pair,
                            data["columns"],
                            pair_rows[pair.code]["src"].get(bucket_no, []),
                            pair_rows[pair.code]["tgt"].get(bucket_no, []),
                            bucket_no=bucket_no,
                            sample_rate=batch.sample_rate,
                        )
                    )
                if pair_stat:
                    breakdown.append(
                        {
                            "pair_code": pair.code,
                            "src_count": pair_stat["src_count"],
                            "tgt_count": pair_stat["tgt_count"],
                            "src_checksum": pair_stat["src_sum"],
                            "tgt_checksum": pair_stat["tgt_sum"],
                            "status": p_status,
                        }
                    )

            final_src_count = base_src_count + stat.src_count
            final_tgt_count = base_tgt_count + stat.tgt_count
            final_src_sum = (base_src_sum + stat.src_sum) % MOD32
            final_tgt_sum = (base_tgt_sum + stat.tgt_sum) % MOD32
            if bucket_status != BucketStatus.MISMATCH.value:
                if final_src_count == 0 and final_tgt_count == 0:
                    bucket_status = BucketStatus.EMPTY.value
                elif final_src_count == final_tgt_count and final_src_sum == final_tgt_sum:
                    bucket_status = BucketStatus.MATCH.value
                else:
                    bucket_status = BucketStatus.MISMATCH.value

            breakdown.sort(key=lambda item: item["pair_code"])
            if bucket_status == BucketStatus.MATCH.value:
                matched += 1
            elif bucket_status == BucketStatus.MISMATCH.value:
                mismatched += 1
            else:
                empty += 1
            src_total += final_src_count
            tgt_total += final_tgt_count
            bucket_elapsed.append((time.perf_counter() - tick) * 1000)
            new_bucket_rows.append(
                BucketResult(
                    batch_id=batch.id,
                    bucket_no=bucket_no,
                    src_count=final_src_count,
                    tgt_count=final_tgt_count,
                    src_checksum=final_src_sum,
                    tgt_checksum=final_tgt_sum,
                    status=bucket_status,
                    pair_breakdown=breakdown,
                    compared_rows=0,
                    elapsed_ms=int(bucket_elapsed[-1]),
                )
            )

        # 4) 落库：只替换受影响表/桶的旧结果，未重算的桶原样保留。
        if existing_buckets:
            db.query(BucketResult).filter(
                BucketResult.batch_id == batch.id,
                BucketResult.bucket_no < total_buckets,
            ).delete()
        db.add_all(new_bucket_rows)
        db.flush()

        db.query(DiffDetail).filter(
            DiffDetail.batch_id == batch.id,
            DiffDetail.pair_code.in_(affected_codes),
        ).delete(synchronize_session=False)
        db.flush()
        for payload in diff_payloads:
            db.add(
                DiffDetail(
                    batch_id=batch.id,
                    bucket_no=payload["bucket_no"],
                    pair_code=payload["pair_code"],
                    primary_key_value=payload["primary_key_value"],
                    field_name=payload["field_name"],
                    src_value=payload["src_value"],
                    tgt_value=payload["tgt_value"],
                    diff_type=payload["diff_type"],
                    similarity=Decimal(str(payload["similarity"])),
                    attribution=payload["attribution"],
                    suggestion=payload["suggestion"],
                )
            )
        # autoflush=False：必须显式 flush，后续按 DB 聚合的 diff_count / diff_types 才能读到新写入的明细。
        db.flush()

        # 5) 表级验收结论 + 批次汇总（基于本轮开始前的快照，避免 commit 后属性过期）。
        active_quarantine = _active_quarantine_counts(db, batch.id)
        pair_results: dict[str, Any] = dict(prior_results)
        final_buckets = {
            item.bucket_no: item
            for item in db.query(BucketResult).filter(BucketResult.batch_id == batch.id).all()
        }
        finished = bucket_limit is None
        for code in batch.pair_codes or []:
            entry = dict(pair_results.get(code) or {})
            if code in blocked_codes:
                entry.update(
                    {
                        "status": PairRunStatus.BLOCKED.value,
                        "runs": int(entry.get("runs", 0)) + 1,
                        "last_run_at": now().isoformat(),
                    }
                )
                pair_results[code] = entry
                continue
            if code not in affected_codes:
                pair_results[code] = entry  # 未参与本轮（理论上不会出现），保持原结论
                continue
            pair_buckets = [
                item
                for item in final_buckets.values()
                if any(b["pair_code"] == code for b in item.pair_breakdown)
            ]
            verified_buckets = sum(
                1
                for item in pair_buckets
                if next(b for b in item.pair_breakdown if b["pair_code"] == code)["status"]
                == BucketStatus.MATCH.value
            )
            mismatched_buckets = sum(
                1
                for item in pair_buckets
                if next(b for b in item.pair_breakdown if b["pair_code"] == code)["status"]
                == BucketStatus.MISMATCH.value
            )
            q_count = active_quarantine.get(code, 0)
            if not finished:
                # 部分桶演示运行：尚未核完全部桶，表结论保持 pending，且不计执行次数，
                # 否则后续全量补跑会被「已 verified」跳过、剩余桶永不计算。
                entry.update(
                    {
                        "status": PairRunStatus.PENDING.value,
                        "quarantine_count": q_count,
                        "verified_buckets": verified_buckets,
                        "mismatched_buckets": mismatched_buckets,
                        "last_run_at": now().isoformat(),
                    }
                )
            else:
                entry.update(
                    {
                        "status": PairRunStatus.QUARANTINED.value if q_count else PairRunStatus.VERIFIED.value,
                        "quarantine_count": q_count,
                        "verified_buckets": verified_buckets,
                        "mismatched_buckets": mismatched_buckets,
                        "runs": int(entry.get("runs", 0)) + 1,
                        "last_run_at": now().isoformat(),
                    }
                )
            pair_results[code] = entry

        if finished:
            for code, entry in pair_results.items():
                if not entry.get("status"):
                    entry["status"] = PairRunStatus.PENDING.value
            all_verified = all(
                entry.get("status") == PairRunStatus.VERIFIED.value for entry in pair_results.values()
            ) and len(pair_results) == len(batch.pair_codes or [])
            has_blocked = any(
                entry.get("status") == PairRunStatus.BLOCKED.value for entry in pair_results.values()
            )
            has_quarantine = any(
                entry.get("status") == PairRunStatus.QUARANTINED.value for entry in pair_results.values()
            )
            if has_blocked and not has_quarantine and all(
                entry.get("status") in (PairRunStatus.VERIFIED.value, PairRunStatus.BLOCKED.value)
                for entry in pair_results.values()
            ):
                batch.status = BatchStatus.PARTIAL.value
                batch.failure_reason = "pair_inactive"
            elif all_verified:
                batch.status = BatchStatus.COMPLETED.value
                batch.failure_reason = None
            else:
                batch.status = BatchStatus.PARTIAL.value
                batch.failure_reason = "quarantined_records" if has_quarantine else batch.failure_reason
        else:
            batch.status = BatchStatus.RUNNING.value

        all_final_buckets = sorted(final_buckets.values(), key=lambda item: item.bucket_no)
        final_matched = sum(1 for item in all_final_buckets if item.status == BucketStatus.MATCH.value)
        final_mismatched = sum(1 for item in all_final_buckets if item.status == BucketStatus.MISMATCH.value)
        final_empty = sum(1 for item in all_final_buckets if item.status == BucketStatus.EMPTY.value)
        final_src = sum(item.src_count for item in all_final_buckets)
        final_tgt = sum(item.tgt_count for item in all_final_buckets)
        final_diff_count = (
            db.query(DiffDetail).filter(DiffDetail.batch_id == batch.id).count()
        )
        final_quarantine = sum(active_quarantine.values())
        total_final_buckets = len(all_final_buckets)

        elapsed_ms = (time.perf_counter() - started_at) * 1000
        avg_bucket_ms = elapsed_ms / total_buckets if total_buckets else 0.0
        skip_rate = round(final_matched / total_final_buckets * 100, 2) if total_final_buckets else 0.0
        batch.buckets_total = total_final_buckets
        batch.buckets_matched = final_matched
        batch.buckets_mismatched = final_mismatched
        batch.buckets_empty = final_empty
        batch.src_rows = final_src
        batch.tgt_rows = final_tgt
        batch.skip_rate = Decimal(str(skip_rate))
        batch.diff_count = final_diff_count
        batch.quarantine_count = final_quarantine
        batch.pair_results_json = pair_results
        batch.finished_at = now() if finished else None
        batch.scan_cost_json = {
            "computed_buckets": total_final_buckets,
            "time_window_days": batch.window_days,
            "elapsed_ms": round(elapsed_ms, 1),
            "avg_bucket_ms": round(avg_bucket_ms, 3),
            "estimated_full_scan_ms": round(avg_bucket_ms * batch.bucket_count, 1),
            "skipped_buckets": final_matched,
            "skip_rate_pct": skip_rate,
            "diff_types": diff_engine.summarize_types(
                [
                    {"diff_type": diff_type}
                    for (diff_type,) in db.query(DiffDetail.diff_type)
                    .filter(DiffDetail.batch_id == batch.id)
                    .all()
                ]
            ),
            "reran_pairs": affected_codes,
            "skipped_pairs": skipped_codes,
            "quarantine_open": final_quarantine,
        }
        batch.params_json = {
            **(batch.params_json or {}),
            "engine": "bucket_md5_checksum_v2_quarantine",
            "bucket_formula": "int(md5(pk)[:8],16) mod N",
            "checksum_formula": "sum(int(md5(row)[:8],16)) mod 2^32",
            "sample_formula": "int(md5(pk+'#sample')[:8],16) % 100 < sample_rate",
            "quarantine_policy": "null_pk_rows_isolated_per_pair_others_continue",
        }
        db.commit()
        if finished:
            audit_service.record(
                db,
                action="batch.completed" if batch.status == BatchStatus.COMPLETED.value else "batch.partial",
                entity_type="ReconBatch",
                entity_id=str(batch.id),
                actor_id=batch.created_by,
                actor_kind="system",
                payload={
                    "status": batch.status,
                    "buckets": total_final_buckets,
                    "mismatched": final_mismatched,
                    "skip_rate": skip_rate,
                    "diffs": final_diff_count,
                    "quarantine": final_quarantine,
                    "pair_results": pair_results,
                    "reran_pairs": affected_codes,
                },
            )
        logger.info(
            "batch run batch_id=%s status=%s buckets=%s mismatched=%s skip_rate=%s diffs=%s quarantine=%s "
            "reran=%s elapsed_ms=%.1f",
            batch.id,
            batch.status,
            total_final_buckets,
            final_mismatched,
            skip_rate,
            final_diff_count,
            final_quarantine,
            affected_codes,
            elapsed_ms,
        )
        return {
            "ok": True,
            "batch_id": batch.id,
            "status": batch.status,
            "buckets": total_final_buckets,
            "matched": final_matched,
            "mismatched": final_mismatched,
            "empty": final_empty,
            "skip_rate": skip_rate,
            "diffs": final_diff_count,
            "quarantine": final_quarantine,
            "reran_pairs": affected_codes,
            "skipped_pairs": skipped_codes,
            "elapsed_ms": round(elapsed_ms, 1),
        }


def estimate_scan_cost(db: Session, pair_codes: list[str], bucket_count: int, sample_rate: int) -> dict[str, Any]:
    """批次创建页的预估：行数、桶数、预计抽样行数、按历史批次均速估算扫描耗时。"""
    pairs = db.query(TablePair).filter(TablePair.code.in_(pair_codes)).all()
    src_rows = sum(pair.row_estimate_src for pair in pairs)
    tgt_rows = sum(pair.row_estimate_tgt for pair in pairs)
    estimated_scan_ms = 0.0
    last_batch = (
        db.query(ReconBatch)
        .filter(ReconBatch.status == BatchStatus.COMPLETED.value)
        .order_by(ReconBatch.id.desc())
        .first()
    )
    if last_batch and last_batch.scan_cost_json:
        avg_bucket_ms = float(last_batch.scan_cost_json.get("avg_bucket_ms", 0) or 0)
        estimated_scan_ms = round(avg_bucket_ms * bucket_count, 1)
    return {
        "pair_count": len(pairs),
        "bucket_count": bucket_count,
        "sample_rate": sample_rate,
        "src_rows": src_rows,
        "tgt_rows": tgt_rows,
        "rows_per_bucket": round((src_rows + tgt_rows) / bucket_count, 1) if bucket_count else 0,
        "estimated_sampled_rows": int(round(src_rows * sample_rate / 100)),
        "estimated_scan_ms": estimated_scan_ms,
        "source_batch": last_batch.batch_no if last_batch else None,
    }
