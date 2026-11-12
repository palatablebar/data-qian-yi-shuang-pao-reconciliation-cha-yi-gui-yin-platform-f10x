/** 坏记录隔离区：所属表对、原主键位置、隔离原因、整行快照；隔离记录不计入一致。 */
import { LedgerTable, type LedgerColumn } from './LedgerTable';
import { StatusSeal } from './StatusSeal';
import type { QuarantineRecord } from '../../types/models';
import { formatTime } from '../../utils/formatTime';

interface Props {
  records: QuarantineRecord[];
  openCount: number;
  resolvedCount: number;
}

export function QuarantinePanel({ records, openCount, resolvedCount }: Props) {
  const columns: LedgerColumn<QuarantineRecord>[] = [
    {
      key: 'pair',
      label: '所属表对 / 来源端',
      render: (row) => (
        <span>
          <strong>{row.pair_code}</strong>
          <span className="mono muted" style={{ marginLeft: 8, fontSize: 'var(--step--1)' }}>
            {row.source_side}
          </span>
        </span>
      ),
    },
    {
      key: 'position',
      label: '原主键位置',
      align: 'right',
      render: (row) => (
        <span className="mono">
          {row.primary_key_name} · 第 {row.row_position} 行
        </span>
      ),
    },
    {
      key: 'reason',
      label: '隔离原因',
      render: (row) => <span className="red">{row.reason_label}</span>,
    },
    {
      key: 'snapshot',
      label: '坏记录整行快照',
      render: (row) => (
        <span className="mono muted" style={{ fontSize: 'var(--step--1)', wordBreak: 'break-all' }}>
          {Object.entries(row.row_snapshot)
            .map(([key, value]) => `${key}=${value === null ? 'NULL' : String(value)}`)
            .join(' · ')}
        </span>
      ),
    },
    {
      key: 'state',
      label: '处理状态',
      align: 'right',
      render: (row) =>
        row.resolved ? (
          <StatusSeal status="verified" label={`已清理 · ${formatTime(row.resolved_at)}`} tone="green" />
        ) : (
          <StatusSeal status="quarantined" label="待修数" />
        ),
    },
  ];
  return (
    <div>
      <p className="mono muted" style={{ fontSize: 'var(--step--1)', margin: '0 0 10px' }}>
        待处理 <span className="red">{openCount}</span> 条 · 已清理 {resolvedCount} 条；
        隔离记录不参与桶 checksum、不计入一致，修数后重跑只重算受影响表。
      </p>
      <LedgerTable
        columns={columns}
        rows={records}
        rowKey={(row) => row.id}
        emptyHint="无隔离记录：本批次未发现主键为空等坏记录。"
      />
    </div>
  );
}
