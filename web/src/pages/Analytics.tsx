import { useEffect, useMemo, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import Chart from '../components/Chart'
import DataGate from '../components/DataGate'
import SeverityBadge from '../components/SeverityBadge'
import {
  AXIS_LABEL,
  AXIS_LINE,
  asFormatter,
  MONO_FONT,
  SPLIT_LINE,
  TOOLTIP_STYLE,
} from '../components/charts/setup'
import { useDemoStore } from '../store/demoStore'
import { STATIC_RUN, useFeedbackStore } from '../store/feedbackStore'
import { useLabelingStore } from '../store/labelingStore'
import { SEVERITY_DESC, severityColor, severityI18nKey, theme } from '../theme/tokens'
import type { AccidentEvent } from '../types/demo'

/**
 * 数据看板 —— 把"技术能力"翻译成"能指着说的数字"。
 *
 * 布局刻意**不是**一排一模一样的卡片（那是 §6.1 与设计规范都点名要避免的模板感）：
 * 用 3 列网格 + 2:1 跨列，让每屏的信息密度有变化。
 */

/* ────────────────────────── 通用面板 ────────────────────────── */

function Panel({
  title,
  note,
  children,
  className,
  grow,
}: {
  title: string
  note?: string
  children: ReactNode
  className?: string
  /** 图表纵向撑满，用于让并排的两块面板底部对齐 */
  grow?: boolean
}) {
  return (
    <section className={`flex flex-col rounded-lg border border-hair bg-panel ${className ?? ''}`}>
      <header className="flex shrink-0 items-baseline justify-between gap-3 border-b border-hair px-4 py-2.5">
        <h2 className="text-[13px] font-medium text-ink">{title}</h2>
        {note ? <span className="num text-[11px] text-ink-faint">{note}</span> : null}
      </header>
      <div className={`min-h-0 p-4 ${grow ? 'flex flex-1 flex-col justify-center' : ''}`}>
        {children}
      </div>
    </section>
  )
}

/** 大数字块：仪表盘上唯一允许夸张的字号 */
function BigStat({ value, unit, label, hint }: { value: string; unit?: string; label: string; hint?: string }) {
  return (
    <div>
      <div className="flex items-baseline gap-1">
        <span className="num text-[34px] leading-none tracking-tight text-accent">{value}</span>
        {unit ? <span className="text-[12px] text-ink-dim">{unit}</span> : null}
      </div>
      <div className="mt-2 text-[11px] leading-snug text-ink-dim">{label}</div>
      {hint ? <div className="mt-1 text-[11px] leading-snug text-ink-faint">{hint}</div> : null}
    </div>
  )
}

/* ────────────────────────── 1. 算力效率 ────────────────────────── */

function ComputePanel({ className }: { className?: string }) {
  const { t } = useTranslation()
  const data = useDemoStore((s) => s.data)!
  const s = data.stats

  const ratio = (s.frames_sent / s.raw_accident_frames) * 100
  const steps = [
    {
      label: t('analytics.compute.totalFrames'),
      value: data.video.total_frames,
      color: theme.text.tertiary,
      pct: null as number | null,
    },
    {
      label: t('analytics.compute.accidentFrames'),
      value: s.raw_accident_frames,
      color: theme.accent,
      pct: (s.raw_accident_frames / data.video.total_frames) * 100,
    },
    { label: t('analytics.compute.events'), value: s.events, color: theme.series[1], pct: null },
    { label: t('analytics.compute.framesSent'), value: s.frames_sent, color: theme.series[2], pct: ratio },
  ]
  const max = data.video.total_frames

  return (
    <Panel
      title={t('analytics.compute.title')}
      note={t('analytics.compute.note')}
      className={className}
      grow
    >
      <div className="flex gap-5">
        <div className="min-w-0 flex-1 space-y-3 self-center">
          {steps.map((step) => (
            <div key={step.label} className="flex items-center gap-3">
              {/* 98px 是按英文最长标签「Accident frames」定的，再窄就会折行 */}
              <span className="w-[98px] shrink-0 text-[12px] leading-tight text-ink-dim">
                {step.label}
              </span>
              <div className="h-[18px] min-w-0 flex-1 overflow-hidden rounded-sm bg-raised">
                <div
                  className="h-full rounded-sm transition-[width] duration-500"
                  style={{ width: `${Math.max(1.5, (step.value / max) * 100)}%`, background: step.color }}
                />
              </div>
              <span className="num w-[52px] shrink-0 text-right text-[13px] text-ink">{step.value}</span>
              <span className="num w-[46px] shrink-0 text-right text-[11px] text-ink-faint">
                {step.pct === null ? '—' : `${step.pct.toFixed(1)}%`}
              </span>
            </div>
          ))}
        </div>

        <div className="w-[148px] shrink-0 self-stretch border-l border-hair pl-5">
          <div className="flex h-full flex-col justify-center">
            <BigStat
              value={ratio.toFixed(1)}
              unit="%"
              label={t('analytics.compute.ratioLabel')}
              hint={t('analytics.compute.ratioHint', { pct: (100 - ratio).toFixed(1) })}
            />
          </div>
        </div>
      </div>
    </Panel>
  )
}

/* ────────────────────────── 2. 严重等级分布 ────────────────────────── */

function SeverityPanel({ className }: { className?: string }) {
  const { t } = useTranslation()
  const data = useDemoStore((s) => s.data)!
  const dist = data.stats.event_severity_distribution

  const slices = useMemo(
    () =>
      SEVERITY_DESC.map((key) => ({
        key,
        name: t(`severity.${severityI18nKey(key)}`),
        value: dist[key] ?? 0,
        color: severityColor(key),
      })),
    [dist, t],
  )

  const total = slices.reduce((n, s) => n + s.value, 0)

  const option = useMemo(
    () => ({
      tooltip: {
        ...TOOLTIP_STYLE,
        trigger: 'item' as const,
        formatter: asFormatter<{ name: string; value: number; percent: number }>(
          (p) => `${p.name}<br/><b>${p.value}</b> ${t('common.event')} · ${p.percent}%`,
        ),
      },
      series: [
        {
          type: 'pie' as const,
          radius: ['64%', '88%'],
          center: ['50%', '50%'],
          avoidLabelOverlap: false,
          label: { show: false },
          labelLine: { show: false },
          itemStyle: { borderColor: theme.bg.panel, borderWidth: 3, borderRadius: 2 },
          emphasis: { scale: true, scaleSize: 7 },
          data: slices.map((s) => ({ name: s.name, value: s.value, itemStyle: { color: s.color } })),
        },
      ],
    }),
    [slices, t],
  )

  return (
    <Panel title={t('analytics.severity.title')} note={t('analytics.severity.note')} className={className}>
      <div className="flex h-full flex-col justify-center gap-3">
        <div className="relative">
          <Chart option={option} height={140} />
          <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
            <span className="num text-[26px] leading-none text-ink">{total}</span>
            <span className="mt-1 text-[10px] text-ink-faint">{t('common.event')}</span>
          </div>
        </div>

        <div className="space-y-1.5">
          {slices.map((s) => (
            <div key={s.key} className="flex items-center gap-2 text-[12px]">
              <span className="h-2 w-2 shrink-0 rounded-sm" style={{ background: s.color }} />
              <span className="text-ink-dim">{s.name}</span>
              <span className="num ml-auto text-ink">{s.value}</span>
              <span className="num w-[42px] text-right text-[11px] text-ink-faint">
                {total ? `${((s.value / total) * 100).toFixed(0)}%` : '—'}
              </span>
            </div>
          ))}
        </div>
      </div>
    </Panel>
  )
}

/* ────────────────────────── 3. 事件时序 ────────────────────────── */

function TimelinePanel({ className }: { className?: string }) {
  const { t } = useTranslation()
  const data = useDemoStore((s) => s.data)!
  const duration = data.video.duration_sec

  /** demo.json 里事件按 高→中→低 排好了序，但时序图要按时间先后排 */
  const chrono = useMemo(
    () => [...data.events].sort((a, b) => a.start_sec - b.start_sec),
    [data.events],
  )

  const option = useMemo(
    () => {
      type Tip = { data: { ev: AccidentEvent } }
      return {
        // containLabel 而不是固定 left：中文「事件 1」和英文「Incident 1」宽度差一倍，
        // 写死数值必然有一边被裁掉或留出大空档。
        grid: { left: 8, right: 54, top: 6, bottom: 26, containLabel: true },
        tooltip: {
          ...TOOLTIP_STYLE,
          trigger: 'item' as const,
          formatter: asFormatter<Tip>((p) => {
            const e = p.data.ev
            return [
              `${t('common.event')} ${e.event_id} · ${t(`severity.${severityI18nKey(e.event_severity)}`)}`,
              `${e.start_sec.toFixed(2)} – ${e.end_sec.toFixed(2)}s（${t('common.duration')} ${e.duration_sec.toFixed(2)}s）`,
              `${t('common.peakConfidence')} ${e.peak_confidence.toFixed(3)}`,
            ].join('<br/>')
          }),
        },
        xAxis: {
          type: 'value' as const,
          min: 0,
          max: duration,
          axisLabel: { ...AXIS_LABEL, formatter: '{value}s' },
          axisLine: AXIS_LINE,
          axisTick: { show: false },
          splitLine: SPLIT_LINE,
        },
        yAxis: {
          type: 'category' as const,
          inverse: true,
          data: chrono.map((e) => `${t('common.event')} ${e.event_id}`),
          axisLabel: { ...AXIS_LABEL, fontSize: 11 },
          axisLine: { show: false },
          axisTick: { show: false },
        },
        series: [
          {
            // 透明偏移条：把彩色条推到真正的开始时刻
            type: 'bar' as const,
            stack: 'ev',
            silent: true,
            barWidth: 10,
            itemStyle: { color: 'transparent' },
            data: chrono.map((e) => e.start_sec),
          },
          {
            type: 'bar' as const,
            stack: 'ev',
            barWidth: 10,
            data: chrono.map((e) => ({
              value: e.duration_sec,
              ev: e,
              itemStyle: { color: severityColor(e.event_severity), borderRadius: 3 },
            })),
            label: {
              show: true,
              position: 'right' as const,
              distance: 7,
              color: theme.text.tertiary,
              fontSize: 10,
              fontFamily: MONO_FONT,
              formatter: asFormatter<Tip>((p) => `${p.data.ev.duration_sec.toFixed(2)}s`),
            },
          },
        ],
      }
    },
    [chrono, duration, t],
  )

  return (
    <Panel title={t('analytics.timeline.title')} note={t('analytics.timeline.note')} className={className}>
      <Chart option={option} height={252} />
    </Panel>
  )
}

/* ────────────────────────── 4. 推理质量 ────────────────────────── */

function QualityPanel({ className }: { className?: string }) {
  const { t } = useTranslation()
  const data = useDemoStore((s) => s.data)!
  const s = data.stats

  /**
   * 歧义事件从数据里找，不写死事件号——写死的话一旦重跑推理换了数据，
   * 界面上那句"本次涉及事件 6"就变成假话，而这种错最难被发现。
   */
  const ambiguousEvents = data.events
    .filter((e) => e.representatives.some((r) => r.ambiguous))
    .map(
      (e) =>
        `${t('common.event')} ${e.event_id} (${e.event_severity_levels
          .map((k) => t(`severity.${severityI18nKey(k)}`))
          .join(' → ')})`,
    )
    .join(', ')

  const rows: { label: string; value: string; warn?: boolean }[] = [
    { label: t('analytics.quality.sent'), value: String(s.frames_sent) },
    { label: t('analytics.quality.ok'), value: `${s.understood} / ${s.frames_sent}` },
    { label: t('analytics.quality.failed'), value: String(s.failed) },
    { label: t('analytics.quality.degenerate'), value: String(s.degenerate_outputs) },
    { label: t('analytics.quality.ambiguous'), value: String(s.ambiguous_severity), warn: s.ambiguous_severity > 0 },
    { label: t('analytics.quality.followup'), value: String(s.severity_from_followup) },
  ]

  return (
    <Panel title={t('analytics.quality.title')} note={t('analytics.quality.note')} className={className}>
      <div className="flex h-full flex-col justify-center">
        <dl>
          {rows.map((r) => (
            <div
              key={r.label}
              className="flex items-baseline justify-between gap-3 border-b border-hair py-[7px] last:border-b-0"
            >
              <dt className="text-[12px] text-ink-dim">{r.label}</dt>
              <dd className={`num text-[13px] ${r.warn ? 'text-sev-mid' : 'text-ink'}`}>{r.value}</dd>
            </div>
          ))}
        </dl>
        {s.ambiguous_severity > 0 && ambiguousEvents ? (
          <p className="mt-3 text-[11px] leading-snug text-ink-faint">
            {t('analytics.quality.ambiguousNote', { events: ambiguousEvents })}
          </p>
        ) : null}
      </div>
    </Panel>
  )
}

/* ────────────────────────── 5. 检测置信度分布 ────────────────────────── */

const BIN_LO = 0.25
const BIN_HI = 0.9
const BIN_STEP = 0.05

function ConfidencePanel({ className }: { className?: string }) {
  const { t } = useTranslation()
  const data = useDemoStore((s) => s.data)!

  const { labels, counts, total } = useMemo(() => {
    const bins = Math.round((BIN_HI - BIN_LO) / BIN_STEP)
    const c = new Array<number>(bins).fill(0)
    for (const p of data.timeline) {
      let i = Math.floor((p.confidence - BIN_LO) / BIN_STEP)
      if (i < 0) i = 0
      if (i >= bins) i = bins - 1
      c[i] += 1
    }
    return {
      labels: c.map((_, i) => (BIN_LO + i * BIN_STEP).toFixed(2)),
      counts: c,
      total: data.timeline.length,
    }
  }, [data.timeline])

  const option = useMemo(
    () => ({
      grid: { left: 8, right: 12, top: 12, bottom: 26, containLabel: true },
      tooltip: {
        ...TOOLTIP_STYLE,
        trigger: 'axis' as const,
        axisPointer: { type: 'shadow' as const, shadowStyle: { color: 'rgba(255,255,255,0.045)' } },
        formatter: asFormatter<{ name: string; value: number }[]>((ps) => {
          const p = ps[0]
          const lo = Number(p.name)
          return [
            `${t('common.confidence')} ${lo.toFixed(2)} – ${(lo + BIN_STEP).toFixed(2)}`,
            `<b>${p.value}</b> ${t('common.frame')} · ${((p.value / total) * 100).toFixed(1)}%`,
          ].join('<br/>')
        }),
      },
      xAxis: {
        type: 'category' as const,
        data: labels,
        axisLabel: { ...AXIS_LABEL, interval: 0, fontSize: 10 },
        axisLine: AXIS_LINE,
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value' as const,
        name: t('common.frame'),
        nameTextStyle: { ...AXIS_LABEL, align: 'right' as const, padding: [0, 4, 0, 0] },
        axisLabel: AXIS_LABEL,
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: SPLIT_LINE,
      },
      series: [
        {
          type: 'bar' as const,
          barCategoryGap: '18%',
          data: counts,
          itemStyle: {
            borderRadius: [3, 3, 0, 0],
            color: {
              type: 'linear' as const,
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: theme.accent },
                { offset: 1, color: 'rgba(34, 211, 238, 0.22)' },
              ],
            },
          },
        },
      ],
    }),
    [labels, counts, total, t],
  )

  return (
    <Panel
      title={t('analytics.confidence.title')}
      note={t('analytics.confidence.note', { n: total })}
      className={className}
    >
      <Chart option={option} height={186} />
      <p className="mt-2 text-[11px] leading-snug text-ink-faint">
        {t('analytics.confidence.thresholdNote', { thr: data.source.conf_threshold })}
      </p>
    </Panel>
  )
}

/* ────────────────────────── 6. 统计口径 ────────────────────────── */

function CaliberPanel({ className }: { className?: string }) {
  const { t } = useTranslation()
  const data = useDemoStore((s) => s.data)!
  const frame = data.stats.severity_distribution
  const event = data.stats.event_severity_distribution

  const sum = (d: Record<string, number>) => SEVERITY_DESC.reduce((n, k) => n + (d[k] ?? 0), 0)

  return (
    <Panel title={t('analytics.caliber.title')} note={t('analytics.caliber.note')} className={className}>
      <div className="flex h-full flex-col justify-center">
        <table className="w-full text-[12px]">
          <thead>
            <tr className="text-[11px] text-ink-faint">
              <th className="pb-2 text-left font-normal">{t('severity.label')}</th>
              <th className="pb-2 text-right font-normal">{t('analytics.caliber.frameLevel')}</th>
              <th className="pb-2 text-right font-normal">{t('analytics.caliber.eventLevel')}</th>
            </tr>
          </thead>
          <tbody>
            {SEVERITY_DESC.map((k) => (
              <tr key={k} className="border-t border-hair">
                <td className="py-[7px]">
                  <span className="flex items-center gap-2">
                    <span className="h-2 w-2 rounded-sm" style={{ background: severityColor(k) }} />
                    <span className="text-ink-dim">{t(`severity.${severityI18nKey(k)}`)}</span>
                  </span>
                </td>
                <td className="num py-[7px] text-right text-ink-faint">{frame[k] ?? 0}</td>
                <td className="num py-[7px] text-right text-ink">{event[k] ?? 0}</td>
              </tr>
            ))}
            <tr className="border-t border-line">
              <td className="py-[7px] text-ink-dim">{t('analytics.caliber.total')}</td>
              <td className="num py-[7px] text-right text-ink-faint">{sum(frame)}</td>
              <td className="num py-[7px] text-right text-ink">{sum(event)}</td>
            </tr>
          </tbody>
        </table>

        <div className="mt-3">
          <p className="text-[11px] leading-snug text-ink-faint">{t('analytics.caliber.rule')}</p>
          <code className="num mt-2 inline-block rounded-sm border border-hair bg-raised px-2 py-1 text-[10px] text-accent">
            {data.stats.event_severity_rule}
          </code>
        </div>
      </div>
    </Panel>
  )
}

/* ────────────────────────── 6. 模型可信度 ────────────────────────── */

const CRED_LEVELS = ['低', '中', '高'] as const

/** 指标数字。刻意不用"越好越绿"那套配色——准确率偏低时不该被染成中性色。 */
function Stat({ value, label, hint }: { value: string; label: string; hint?: string }) {
  return (
    <div>
      <div className="num text-[22px] leading-none text-ink">{value}</div>
      <div className="mt-1.5 text-[11px] text-ink-dim">{label}</div>
      {hint ? <div className="mt-1 text-[10px] leading-snug text-ink-faint">{hint}</div> : null}
    </div>
  )
}

/**
 * 模型可信度。
 *
 * 与这一页其他面板的关系，是这个面板最需要讲清楚的事：
 * 「推理质量」统计的是**模型有没有正常应答**（18/18 成功、0 退化），
 * 它**不说明模型答对了没有**。把前者当后者读，会以为精度已经验证过——
 * 而实际上在这个面板出现之前，一次都没验证过。
 *
 * 所以这里的三条规矩：
 *   ① 没有复核记录时**不给数字**（后端在分母为 0 时返回 null），
 *      因为"还没测过"和"准确率 0%"是完全不同的结论；
 *   ② 永远带上样本量（"基于已复核的 N 起"），
 *      只复核 2 起就说"精确率 100%"是会误导人的；
 *   ③ 明确写出**不含漏检**——漏检需要负样本标注集，不能拿这里的数字冒充。
 */
/**
 * 帧级检测指标（召回率 / 漏检率）。
 *
 * 它与上面的事件级指标**不是同一层东西**，所以单独一行、明确分开：
 *   事件级来自「人工复核」——判这起事故的等级对不对
 *   帧级来自「抽样标注」——判这一帧里到底有没有事故
 * 前者回答"报出来的准不准"，后者回答"漏了多少"。两个都有，才谈得上可信。
 *
 * 没有完成抽样标注时**不给数字**——只有召回率的分母（检出真阳 + 漏检）
 * 都估计出来了，这个比值才有意义。
 */
function RecallStrip() {
  const { t } = useTranslation()
  const metrics = useLabelingStore((s) => s.metrics)
  const load = useLabelingStore((s) => s.load)

  useEffect(() => {
    void load()
  }, [load])

  const recall = metrics?.ready ? metrics.recall : null
  const rate = (v: number) => `${(v * 100).toFixed(0)}%`

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
      <span className="text-[11px] text-ink-dim">
        {t('analytics.credibility.frameLevel')}
      </span>

      {recall ? (
        <>
          <span className="num text-[18px] leading-none text-accent">{rate(recall.value)}</span>
          <span className="text-[11px] text-ink-dim">{t('labeling.recall')}</span>
          {recall.ci ? (
            <span className="num text-[10px] text-ink-faint">
              {t('labeling.ci', {
                ci: `${(recall.ci[0] * 100).toFixed(0)}–${(recall.ci[1] * 100).toFixed(0)}%`,
                n: recall.n,
              })}
            </span>
          ) : null}
          {metrics?.miss_rate ? (
            <span className="num text-[11px] text-ink-dim">
              {t('labeling.missRate')} {rate(metrics.miss_rate.value)}
            </span>
          ) : null}
        </>
      ) : (
        <span className="text-[11px] text-ink-faint">
          {t('analytics.credibility.recallPending')}
        </span>
      )}

      <Link
        to="/labeling"
        className="ml-auto text-[11px] text-accent transition-opacity hover:opacity-80"
      >
        {t('analytics.credibility.toLabeling')}
      </Link>
    </div>
  )
}

function CredibilityPanel({ className }: { className?: string }) {
  const { t } = useTranslation()
  const data = useDemoStore((s) => s.data)!
  const liveJobId = useDemoStore((s) => s.liveJobId)
  const run = liveJobId ?? STATIC_RUN
  const load = useFeedbackStore((s) => s.load)
  const unavailable = useFeedbackStore((s) => s.unavailable)
  const verdicts = useFeedbackStore((s) => s.view?.verdicts)
  const metrics = useFeedbackStore((s) => s.view?.metrics ?? null)

  useEffect(() => {
    void load(run)
  }, [load, run])

  const title = t('analytics.credibility.title')

  if (unavailable) {
    return (
      <Panel title={title} note={t('analytics.credibility.note')} className={className}>
        <p className="text-[11px] leading-relaxed text-ink-faint">
          {t('feedback.needBackend')}
        </p>
      </Panel>
    )
  }

  if (!metrics || !metrics.evaluated) {
    // 空状态给几个直达入口。空白面板除了浪费空间，也把"我该去哪做复核"
    // 这个问题留给了用户——而这一栏的全部意义就是促成人来做第一次复核。
    const reviewed = new Set((verdicts ?? []).map((v) => v.event_id))
    const todo = data.events.filter((e) => !reviewed.has(e.event_id)).slice(0, 3)

    return (
      <Panel title={title} note={t('analytics.credibility.note')} className={className} grow>
        <div>
          <p className="text-[13px] text-ink">{t('analytics.credibility.none')}</p>
          <p className="mt-2 text-[11px] leading-relaxed text-ink-faint">
            {t('analytics.credibility.noneHint')}
          </p>
          {todo.length > 0 ? (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <span className="text-[11px] text-ink-dim">
                {t('analytics.credibility.startWith')}
              </span>
              {todo.map((e) => (
                <Link
                  key={e.event_id}
                  to={`/events/${e.event_id}`}
                  className="flex items-center gap-1.5 rounded-md border border-line px-2 py-1 text-[11px] text-ink-dim transition-colors hover:bg-raised"
                >
                  <span className="num">
                    {t('common.event')} {e.event_id}
                  </span>
                  <SeverityBadge severity={e.event_severity} size="sm" />
                </Link>
              ))}
            </div>
          ) : null}
        </div>
        {/* 帧级召回率与事件级复核是**两套独立的数据与进度**，不能被彼此挡住。
            它原先只画在"事件级有复核记录"的分支里，结果事件级一次都没复核时，
            明明已经算得出来的召回率被一起藏了起来——那正是这次改动要解决的问题。 */}
        <div className="mt-4 border-t border-hair pt-3">
          <RecallStrip />
        </div>
      </Panel>
    )
  }

  const pct = (v: number | null) => (v === null ? '—' : `${Math.round(v * 100)}%`)

  return (
    <Panel
      title={title}
      note={t('analytics.credibility.sample', { n: metrics.reviewed })}
      className={className}
    >
      <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-8">
        <div className="flex flex-col justify-between">
          <div className="grid grid-cols-3 gap-4">
            <Stat
              value={`${metrics.reviewed}/${metrics.total_events ?? '—'}`}
              label={t('analytics.credibility.reviewed')}
            />
            <Stat
              value={pct(metrics.precision)}
              label={t('analytics.credibility.precision')}
              hint={t('analytics.credibility.precisionHint')}
            />
            <Stat
              value={pct(metrics.severity_accuracy)}
              label={t('analytics.credibility.severityAccuracy')}
              hint={t('analytics.credibility.severityAccuracyHint')}
            />
          </div>
          <p className="mt-4 text-[11px] leading-relaxed text-ink-faint">
            {t('analytics.credibility.blindSpot')}
          </p>
        </div>

        <div>
          <div className="mb-1.5 text-[11px] text-ink-dim">
            {t('analytics.credibility.matrix')}
          </div>
          <table className="text-[12px]">
            <thead>
              {/* 表头两行：第一行是"模型判定"（跨两行）+ "人工结论"（跨三列），
                  第二行是三个等级。第一列被 rowSpan 占掉，所以第二行只放等级，
                  否则网格会错位。 */}
              <tr>
                <th
                  rowSpan={2}
                  className="pr-2.5 text-right align-bottom text-[10px] font-normal leading-tight text-ink-faint"
                >
                  {t('analytics.credibility.predicted')}
                </th>
                <th
                  colSpan={3}
                  className="pb-1 text-center text-[10px] font-normal text-ink-faint"
                >
                  {t('analytics.credibility.truth')}
                </th>
              </tr>
              <tr>
                {CRED_LEVELS.map((lv) => (
                  <th
                    key={lv}
                    className="w-9 pb-1.5 text-center text-[10px] font-normal text-ink-faint"
                  >
                    {t(`severity.${severityI18nKey(lv)}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {CRED_LEVELS.map((pred) => (
                <tr key={pred}>
                  <td className="num py-1 pr-2.5 text-right text-[11px] text-ink-dim">
                    {t(`severity.${severityI18nKey(pred)}`)}
                  </td>
                  {CRED_LEVELS.map((truth) => {
                    const n = metrics.confusion[pred]?.[truth] ?? 0
                    const diag = pred === truth
                    // 对角线＝判对（青），错位格＝判错（中性白）。
                    // 空格的边框压到最暗，让"有数"和"没数"一眼分开。
                    return (
                      <td key={truth} className="py-1 text-center">
                        <span
                          className="num inline-flex h-6 w-8 items-center justify-center rounded-sm text-[11px]"
                          style={{
                            background:
                              n === 0
                                ? 'transparent'
                                : diag
                                  ? 'rgba(34, 211, 238, 0.14)'
                                  : 'rgba(255, 255, 255, 0.07)',
                            border: `1px solid ${
                              n === 0
                                ? 'rgba(255, 255, 255, 0.08)'
                                : diag
                                  ? 'rgba(34, 211, 238, 0.45)'
                                  : 'rgba(255, 255, 255, 0.18)'
                            }`,
                            // 0 显示成 ·，避免和"有 0 次"混淆
                            color:
                              n === 0 ? theme.text.tertiary : diag ? theme.accent : theme.text.primary,
                          }}
                        >
                          {n === 0 ? '·' : n}
                        </span>
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-1.5 text-[10px] text-ink-faint">
            {t('analytics.credibility.matrixNote')}
          </p>
        </div>
      </div>

      <div className="mt-4 border-t border-hair pt-3">
        <RecallStrip />
      </div>
    </Panel>
  )
}

/* ────────────────────────── 页面 ────────────────────────── */

function Inner() {
  const { t } = useTranslation()
  const data = useDemoStore((s) => s.data)!

  return (
    <div className="space-y-4 p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h1 className="text-[15px] font-medium text-ink">{t('analytics.title')}</h1>
        <span className="text-[11px] text-ink-faint">
          {t('analytics.subtitle', {
            frames: data.video.total_frames,
            sec: data.video.duration_sec.toFixed(1),
          })}
        </span>
      </div>

      <div className="grid grid-cols-3 gap-4">
        <ComputePanel className="col-span-2" />
        <SeverityPanel />

        <TimelinePanel className="col-span-2" />
        <QualityPanel />

        {/* 可信度放在「推理质量」下面一行、且占 2 列：
            它要放下"指标 + 混淆矩阵"两部分，而且它是这一页最该被追问的东西。
            与 CaliberPanel 配对，后者回答"数字怎么算的"，正好承接。 */}
        <CredibilityPanel className="col-span-2" />
        <CaliberPanel />

        <ConfidencePanel className="col-span-3" />
      </div>
    </div>
  )
}

export default function Analytics() {
  return (
    <DataGate>
      <Inner />
    </DataGate>
  )
}
