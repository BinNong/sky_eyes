import { memo } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import SeverityBadge from './SeverityBadge'
import StatusBadge from './StatusBadge'
import { assetUrl } from '../api/dataSource'
import { useIncidentStore } from '../store/incidentStore'
import { firstSentence } from '../utils/format'
import type { AccidentEvent } from '../types/demo'

/** 实时告警流。memo 掉，避免父组件因播放同步每帧重渲染时白跑一遍。 */
const AlertFeed = memo(function AlertFeed({ items }: { items: AccidentEvent[] }) {
  const { t } = useTranslation()
  const current = useIncidentStore((s) => s.current)

  return (
    <div className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-hair bg-panel">
      <div className="flex shrink-0 items-center gap-2 border-b border-hair px-3.5 py-2.5">
        <span className="text-[13px] text-ink-dim">{t('dashboard.alertFeed')}</span>
        <span className="num ml-auto text-[11px] text-ink-faint">{items.length}</span>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-2.5">
        {items.length === 0 ? (
          <div className="px-2 py-8 text-center text-[12px] text-ink-faint">
            {t('dashboard.feedEmpty')}
          </div>
        ) : (
          <div className="space-y-2">
            {items.map((e) => (
              <Link
                key={e.event_id}
                to={`/events/${e.event_id}`}
                className="flex gap-2.5 rounded-md border border-hair bg-raised p-2 transition-colors hover:border-line"
              >
                {e.lead?.roi ? (
                  <img
                    src={assetUrl(e.lead.roi)}
                    alt=""
                    loading="lazy"
                    className="h-11 w-14 shrink-0 rounded object-cover"
                  />
                ) : (
                  <div className="h-11 w-14 shrink-0 rounded bg-base" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <SeverityBadge severity={e.event_severity} size="sm" />
                    <StatusBadge status={current[e.event_id] ?? 'pending'} size="sm" />
                    <span className="num ml-auto text-[11px] text-ink-faint">
                      {e.start_sec.toFixed(2)}s
                    </span>
                  </div>
                  <div className="mt-1 line-clamp-2 text-[12px] leading-snug text-ink-dim">
                    {firstSentence(e.lead?.description) || '—'}
                  </div>
                </div>
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  )
})

export default AlertFeed
