/** /batches/:id 批次详情：桶热力矩阵 + 进度 + 逐表验收结论 + 隔离区 + 归因摘要 + 导出。 */
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { exportAttribution, getBuckets, getQuarantine, runBatch } from '../api/batches';
import { getSummary } from '../api/diffs';
import { AttributionList } from '../components/common/AttributionList';
import { BucketMatrix } from '../components/common/BucketMatrix';
import { CountUp } from '../components/common/CountUp';
import { ErrorNote } from '../components/common/ErrorNote';
import { LoadingRule } from '../components/common/LoadingRule';
import { PairVerdictPanel } from '../components/common/PairVerdictPanel';
import { QuarantinePanel } from '../components/common/QuarantinePanel';
import { StatLedger } from '../components/common/StatLedger';
import { StatusSeal } from '../components/common/StatusSeal';
import { BATCH_STATUS_LABELS, DIFF_TYPE_LABELS, STRATEGY_LABELS } from '../constants/enums';
import { KNOWN_FAILURE_LABELS } from '../constants/labels';
import { useBatchPolling } from '../hooks/useBatchPolling';
import { useAuthStore } from '../stores/authStore';
import type { Bucket, DiffSummary, QuarantineResponse } from '../types/models';
import { errorMessage } from '../utils/errorMessage';
import { formatInt, formatMs, formatPercent } from '../utils/formatNumber';
import { formatTime } from '../utils/formatTime';

export function BatchDetail() {
  const params = useParams();
  const batchId = params.batchId ? Number(params.batchId) : null;
  const { batch, reload, polling } = useBatchPolling(batchId, null);
  const [buckets, setBuckets] = useState<Bucket[]>([]);
  const [summary, setSummary] = useState<DiffSummary | null>(null);
  const [quarantine, setQuarantine] = useState<QuarantineResponse | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [running, setRunning] = useState(false);
  const canRun = useAuthStore((state) => state.can('batches:run'));

  useEffect(() => {
    if (!batchId) return;
    getBuckets(batchId)
      .then((data) => setBuckets(data.items))
      .catch((err) => setError(errorMessage(err)));
  }, [batchId, batch?.status, batch?.buckets_matched, batch?.buckets_mismatched]);

  useEffect(() => {
    if (!batchId || (batch?.status !== 'completed' && batch?.status !== 'partial')) return;
    getSummary(batchId)
      .then((data) => setSummary(data))
      .catch(() => undefined);
  }, [batchId, batch?.status]);

  useEffect(() => {
    if (!batchId || (batch?.status !== 'partial' && batch?.status !== 'failed')) return;
    getQuarantine(batchId)
      .then(setQuarantine)
      .catch(() => undefined);
  }, [batchId, batch?.status, batch?.quarantine_count]);

  const onRun = async () => {
    if (!batchId) return;
    setRunning(true);
    setNotice('');
    try {
      await runBatch(batchId);
      setNotice(
        batch?.status === 'partial'
          ? '已投递 Celery 任务：只重算含隔离记录的受影响表，已验收表重复执行不重复计数…'
          : '已投递 Celery 任务：worker 容器正在真跑分桶对账…',
      );
      reload();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setRunning(false);
    }
  };

  if (!batch) {
    return (
      <>
        <header className="page-head">
          <div>
            <div className="page-head__kicker">Schedule 04 · 批次详情</div>
            <h1>批次底稿</h1>
          </div>
        </header>
        {error ? <ErrorNote message={error} title="读取失败" /> : <LoadingRule label="正在读取批次…" />}
      </>
    );
  }

  const computed = Number(batch.scan_cost_json?.computed_buckets || 0);
  const progress = batch.buckets_total > 0 ? Math.min(100, (computed / batch.buckets_total) * 100) : 0;
  const scanCost = batch.scan_cost_json || {};
  const diffTypes = (scanCost.diff_types || {}) as Record<string, number>;

  return (
    <>
      <header className="page-head">
        <div>
          <div className="page-head__kicker">Schedule 04 · 批次详情</div>
          <h1>
            {batch.batch_no}
            {batch.status === 'completed' ? (
              <span style={{ marginLeft: 16, verticalAlign: 'middle' }}>
                <StatusSeal status="completed" label="已核" tone="green" />
              </span>
            ) : null}
            {batch.status === 'partial' ? (
              <span style={{ marginLeft: 16, verticalAlign: 'middle' }}>
                <StatusSeal status="partial" label="部分完成 · 含隔离" />
              </span>
            ) : null}
          </h1>
        </div>
        <div className="page-head__meta">
          {BATCH_STATUS_LABELS[batch.status]} · {STRATEGY_LABELS[batch.strategy] || batch.strategy}
          <br />
          {batch.bucket_count} 桶 / 抽样 {batch.sample_rate}% / 时间窗 {batch.window_days} 天
          <br />
          发起 {formatTime(batch.created_at)} · 完成 {formatTime(batch.finished_at)}
        </div>
      </header>

      {error ? <ErrorNote message={error} title="读取失败" /> : null}
      {notice ? (
        <div className="error-note" style={{ borderLeftColor: 'var(--stamp-green)' }}>
          <strong style={{ color: 'var(--stamp-green)' }}>任务已入队</strong>
          <div>{notice}</div>
        </div>
      ) : null}

      {batch.status === 'failed' ? (
        <ErrorNote
          title="批次失败（算法失败态落库）"
          message={`${KNOWN_FAILURE_LABELS[batch.failure_reason || ''] || batch.failure_reason || '未知原因'} · 该表对需先修数再重跑`}
        />
      ) : null}

      {batch.status === 'partial' ? (
        <div className="error-note">
          <strong>批次部分完成：坏记录已隔离，正常表照常出结论</strong>
          <div>
            {KNOWN_FAILURE_LABELS[batch.failure_reason || ''] || batch.failure_reason || '存在隔离记录'}
            ；隔离记录不计入一致。请按下方隔离区修数后重跑，系统只重算受影响表，已验收表不重复计数。
          </div>
        </div>
      ) : null}

      <StatLedger
        cells={[
          { label: '桶总数', value: <CountUp value={batch.bucket_count} />, hint: `已计算 ${formatInt(computed)} 桶` },
          {
            label: '不一致桶',
            value: <CountUp value={batch.buckets_mismatched} />,
            hint: `一致 ${formatInt(batch.buckets_matched)} / 空桶 ${formatInt(batch.buckets_empty)}`,
            tone: batch.buckets_mismatched > 0 ? 'red' : 'green',
          },
          {
            label: '跳过率',
            value: <CountUp value={batch.skip_rate} digits={2} suffix="%" />,
            hint: '一致桶直接跳过、不抽样',
          },
          {
            label: '差异行',
            value: <CountUp value={batch.diff_count} />,
            hint: Object.entries(diffTypes)
              .map(([type, count]) => `${DIFF_TYPE_LABELS[type] || type} ${count}`)
              .join(' / ') || '无差异',
            tone: batch.diff_count > 0 ? 'red' : 'green',
          },
          {
            label: '隔离记录',
            value: <CountUp value={batch.quarantine_count || 0} />,
            hint: '坏记录单独隔离，不计入一致',
            tone: (batch.quarantine_count || 0) > 0 ? 'red' : 'green',
          },
          { label: '扫描行数', value: <CountUp value={batch.src_rows} />, hint: `目标端 ${formatInt(batch.tgt_rows)} 行（不含隔离行）` },
          {
            label: '实际耗时',
            value: formatMs(Number(scanCost.elapsed_ms || 0)),
            hint: `全量估算 ${formatMs(Number(scanCost.estimated_full_scan_ms || 0))}`,
          },
        ]}
      />

      {batch.status === 'running' || batch.status === 'pending' ? (
        <div style={{ marginBottom: 22 }}>
          <div className="row" style={{ justifyContent: 'space-between', marginBottom: 6 }}>
            <span className="mono" style={{ fontSize: 'var(--step--1)' }}>
              对账进度 {formatInt(computed)} / {batch.bucket_count} 桶
            </span>
            <span className="mono muted" style={{ fontSize: 'var(--step--1)' }}>
              {polling ? 'worker 运行中，每 2s 刷新…' : ''}
            </span>
          </div>
          <div className="recon-progress">
            <div className="recon-progress__fill" style={{ width: `${Math.max(progress, 4)}%` }} />
          </div>
        </div>
      ) : null}

      <div className="row" style={{ marginBottom: 22 }}>
        {canRun && (batch.status === 'pending' || batch.status === 'failed' || batch.status === 'partial') ? (
          <button className="btn btn--primary" type="button" onClick={onRun} disabled={running}>
            {running
              ? '入队中…'
              : batch.status === 'partial'
                ? '修数后重跑受影响表（Celery）'
                : '运行对账（Celery）'}
          </button>
        ) : null}
        <Link className="btn btn--ghost" to={`/diffs?batch_id=${batch.id}`}>
          去差异下钻
        </Link>
        <button
          className="btn btn--ghost"
          type="button"
          onClick={() => exportAttribution(batch.id).catch((err) => setError(errorMessage(err)))}
        >
          导出归因 CSV
        </button>
        <span className="mono muted" style={{ fontSize: 'var(--step--1)' }}>
          涉及表对：{batch.pair_codes.join(' / ')}
        </span>
      </div>

      {batch.pair_results && batch.pair_results.length > 0 ? (
        <section className="panel">
          <div className="panel__head">
            <h2 style={{ fontSize: 'var(--step-1)' }}>逐表验收结论</h2>
            <span className="panel__note">
              可验收 {formatInt(batch.pair_results.filter((item) => item.status === 'verified').length)} 张 ·
              待处理 {formatInt(batch.pair_results.filter((item) => item.status !== 'verified').length)} 张
            </span>
          </div>
          <PairVerdictPanel pairResults={batch.pair_results} pairs={batch.pairs} />
        </section>
      ) : null}

      {quarantine && quarantine.items.length > 0 ? (
        <section className="panel">
          <div className="panel__head">
            <h2 style={{ fontSize: 'var(--step-1)' }}>坏记录隔离区</h2>
            <span className="panel__note">隔离记录不属于一致结论，修数清理后重跑受影响表即可</span>
          </div>
          <QuarantinePanel
            records={quarantine.items}
            openCount={quarantine.open_count}
            resolvedCount={quarantine.resolved_count}
          />
        </section>
      ) : null}

      <section className="panel">
        <div className="panel__head">
          <h2 style={{ fontSize: 'var(--step-1)' }}>桶热力矩阵（{batch.bucket_count} 桶）</h2>
          <span className="panel__note">
            不一致桶点击后跳到差异下钻；桶号 = int(md5(pk)[:8],16) mod {batch.bucket_count}
          </span>
        </div>
        <BucketMatrix buckets={buckets} bucketCount={batch.bucket_count} />
      </section>

      {batch.pairs.length > 0 ? (
        <section className="panel">
          <div className="panel__head">
            <h2 style={{ fontSize: 'var(--step-1)' }}>本批次表对</h2>
          </div>
          <div className="row">
            {batch.pairs.map((pair) => (
              <span className="mono" key={pair.code}>
                {pair.display_name}（{pair.code}）· {pair.src_table} → {pair.tgt_table}
              </span>
            ))}
          </div>
        </section>
      ) : null}

      {batch.status === 'completed' || batch.status === 'partial' ? (
        <section className="panel">
          <div className="panel__head">
            <h2 style={{ fontSize: 'var(--step-1)' }}>差异归因（后端聚合）</h2>
            <span className="panel__note">
              总差异 {formatInt(summary?.total_diffs ?? batch.diff_count)} 行 · 跳过率 {formatPercent(batch.skip_rate)}
              （不含隔离记录）
            </span>
          </div>
          {summary ? <AttributionList summary={summary} limit={8} /> : <LoadingRule label="正在读取归因摘要…" />}
        </section>
      ) : null}
    </>
  );
}
