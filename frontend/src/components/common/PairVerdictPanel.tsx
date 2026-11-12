/** 批次内逐表验收结论：哪些表可验收、哪些仍需处理（含隔离/阻塞），隔离不计入一致。 */
import { LedgerTable, type LedgerColumn } from './LedgerTable';
import { StatusSeal } from './StatusSeal';
import { PAIR_RUN_STATUS_LABELS } from '../../constants/enums';
import type { Pair, PairRunResult } from '../../types/models';
import { formatInt } from '../../utils/formatNumber';

interface Props {
  pairResults: PairRunResult[];
  pairs: Pair[];
}

export function PairVerdictPanel({ pairResults, pairs }: Props) {
  const nameByCode = new Map(pairs.map((pair) => [pair.code, pair.display_name]));
  const columns: LedgerColumn<PairRunResult>[] = [
    {
      key: 'pair',
      label: '表对',
      render: (row) => (
        <span>
          <strong>{nameByCode.get(row.pair_code) || row.pair_code}</strong>
          <span className="mono muted" style={{ marginLeft: 8, fontSize: 'var(--step--1)' }}>
            {row.pair_code}
          </span>
        </span>
      ),
    },
    {
      key: 'status',
      label: '验收结论',
      render: (row) => (
        <StatusSeal
          status={row.status}
          label={PAIR_RUN_STATUS_LABELS[row.status] || row.status_label}
        />
      ),
    },
    {
      key: 'buckets',
      label: '一致桶 / 不一致桶',
      align: 'right',
      render: (row) => (
        <span className="mono">
          <span className="green">{formatInt(row.verified_buckets)}</span>
          {' / '}
          <span className={row.mismatched_buckets > 0 ? 'red' : 'muted'}>
            {formatInt(row.mismatched_buckets)}
          </span>
        </span>
      ),
    },
    {
      key: 'quarantine',
      label: '隔离记录（不计一致）',
      align: 'right',
      render: (row) => (
        <span className={`mono ${row.quarantine_count > 0 ? 'red' : 'muted'}`}>
          {formatInt(row.quarantine_count)}
        </span>
      ),
    },
    {
      key: 'runs',
      label: '执行次数',
      align: 'right',
      render: (row) => <span className="mono muted">{formatInt(row.runs)}</span>,
    },
  ];
  return (
    <LedgerTable
      columns={columns}
      rows={pairResults}
      rowKey={(row) => row.pair_code}
      emptyHint="本批次尚未执行，暂无表级结论。"
    />
  );
}
