import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import DataGate from '../components/DataGate'
import SeverityBadge from '../components/SeverityBadge'
import StatusBadge from '../components/StatusBadge'
import { useDemoStore } from '../store/demoStore'
import { STATUS_STYLE, theme } from '../theme/tokens'
import { useIncidentStore } from '../store/incidentStore'
import type { IncidentStatus } from '../store/incidentStore'

const FILTERS = ['all', 'pending', 'dispatched', 'resolved'] as const
type Filter = (typeof FILTERS)[number]

function Inner() {
  const { t } = useTranslation()
  const data = useDemoStore((s) => s.data)!
  const current = useIncidentStore((s) => s.current)
  const [filter, setFilter] = useState<Filter>('all')

  const statusOf = (id: number): IncidentStatus => current[id] ?? 'pending'

  // 注意：依赖里必须放 current，不能放 statusOf —— 后者每次渲染都是新函数。
  // 把取值逻辑直接写进 memo 里，依赖就是完整的，也就不需要 eslint-disable。
  const counts = useMemo(() => {
    const c: Record<Filter, number> = {
      all: data.events.length,
      pending: 0,
      dispatched: 0,
      resolved: 0,
    }
    for (const e of data.events) c[current[e.event_id] ?? 'pending'] += 1
    return c
  }, [data.events, current])

  const rows = useMemo(
    () =>
      filter === 'all'
        ? data.events
        : data.events.filter((e) => (current[e.event_id] ?? 'pending') === filter),
    [data.events, current, filter],
  )

  const dist = data.stats.event_severity_distribution

  return (
    <div className="p-5">
      <div className="mb-4 flex flex-wrap items-baseline gap-x-4 gap-y-2">
        <h1 className="text-[15px] font-medium text-ink">{t('events.title')}</h1>
        <span className="text-[12px] text-ink-faint">
          {data.stats.events} {t('common.event')} · {t('severity.high')} {dist['高'] ?? 0} /{' '}
          {t('severity.medium')} {dist['中'] ?? 0} / {t('severity.low')} {dist['低'] ?? 0}
        </span>

        <div className="ml-auto flex items-center gap-1.5">
          <span className="mr-1 text-[12px] text-ink-faint">{t('events.filterStatus')}</span>
          {FILTERS.map((f) => {
            const active = filter === f
            const style = f === 'all' ? null : STATUS_STYLE[f]
            return (
              <button
                key={f}
                type="button"
                onClick={() => setFilter(f)}
                className={[
                  'rounded-md border px-2.5 py-1 text-[12px] transition-colors',
                  active ? '' : 'border-hair text-ink-dim hover:bg-raised hover:text-ink',
                ].join(' ')}
                style={
                  active
                    ? {
                        color: style?.fg ?? theme.accent,
                        background: style?.bg === 'transparent' ? 'transparent' : (style?.bg ?? theme.bg.raised),
                        borderColor: style?.border ?? 'rgba(34,211,238,0.5)',
                      }
                    : undefined
                }
              >
                {f === 'all' ? t('events.filterAll') : t(`status.${f}`)}
                <span className="num ml-1.5 text-ink-faint">{counts[f]}</span>
              </button>
            )
          })}
        </div>
      </div>

      <div className="overflow-hidden rounded-lg border border-hair">
        <table className="w-full border-collapse text-[13px]">
          <thead>
            <tr className="bg-panel text-left text-[12px] text-ink-dim">
              <th className="px-4 py-2.5 font-normal">{t('events.colEvent')}</th>
              <th className="px-4 py-2.5 font-normal">{t('events.colTime')}</th>
              <th className="px-4 py-2.5 font-normal">{t('events.colFrames')}</th>
              <th className="px-4 py-2.5 font-normal">{t('events.colPeak')}</th>
              <th className="px-4 py-2.5 font-normal">{t('events.colSeverity')}</th>
              <th className="px-4 py-2.5 font-normal">{t('events.colStatus')}</th>
              <th className="px-4 py-2.5 font-normal">{t('events.colSummary')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <tr key={e.event_id} className="border-t border-hair hover:bg-panel">
                <td className="num px-4 py-2.5">
                  <Link to={`/events/${e.event_id}`} className="text-accent hover:underline">
                    #{e.event_id}
                  </Link>
                </td>
                <td className="num px-4 py-2.5 text-ink-dim">{e.start_sec.toFixed(2)}s</td>
                <td className="num px-4 py-2.5 text-ink-dim">
                  {e.start_frame}–{e.end_frame}
                </td>
                <td className="num px-4 py-2.5 text-ink-dim">{e.peak_confidence.toFixed(3)}</td>
                <td className="px-4 py-2.5">
                  <SeverityBadge severity={e.event_severity} size="sm" />
                </td>
                <td className="px-4 py-2.5">
                  <StatusBadge status={statusOf(e.event_id)} size="sm" />
                </td>
                <td className="max-w-[380px] px-4 py-2.5">
                  <span className="line-clamp-1 text-ink-dim">{e.lead?.description ?? '—'}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {rows.length === 0 ? (
          <div className="border-t border-hair px-4 py-8 text-center text-[12px] text-ink-faint">
            —
          </div>
        ) : null}
      </div>

      <p className="mt-3 text-[12px] text-ink-faint">{t('detail.ruleNote')}</p>
    </div>
  )
}

export default function Events() {
  return (
    <DataGate>
      <Inner />
    </DataGate>
  )
}
