import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import IncidentBrief from './IncidentBrief'
import type { IncidentStatus, TrailEntry } from '../store/incidentStore'
import { severityI18nKey } from '../theme/tokens'
import { formatTime } from '../utils/format'
import type { AccidentEvent, DemoData } from '../types/demo'

/** 简报弹层：预览 + 打印/存 PDF + 复制纯文本。 */
export default function BriefModal({
  data,
  event,
  status,
  trail,
  onClose,
}: {
  data: DemoData
  event: AccidentEvent
  status: IncidentStatus
  trail: TrailEntry[]
  onClose: () => void
}) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const buildText = () => {
    const sevKey = severityI18nKey(event.event_severity)
    const advice = t(`advice.${sevKey}`, { returnObjects: true }) as string[]
    const sourceName = data.source.video_path.split('/').pop() ?? ''
    const L: string[] = []
    L.push(`${t('brief.title')}  #${String(event.event_id).padStart(3, '0')}`)
    L.push('')
    L.push(`${t('brief.occurredAt')}: ${formatTime(event.start_sec)} - ${formatTime(event.end_sec)}`)
    L.push(
      `${t('common.frameRange')}: ${event.start_frame} - ${event.end_frame} (${event.num_frames})`,
    )
    L.push(`${t('brief.videoSource')}: ${sourceName}`)
    L.push(`${t('severity.label')}: ${t(`severity.${sevKey}`)}`)
    L.push(`${t('common.peakConfidence')}: ${event.peak_confidence.toFixed(3)}`)
    L.push(`${t('status.label')}: ${t(`status.${status}`)}`)
    L.push('')
    L.push(`${t('brief.scene')}:`)
    L.push(event.lead?.description ?? '—')
    L.push('')
    L.push(`${t('advice.title')}:`)
    ;(Array.isArray(advice) ? advice : []).forEach((a, i) => L.push(`${i + 1}. ${a}`))
    L.push('')
    L.push(`${t('history.title')}:`)
    trail.forEach((h, i) => {
      const when =
        h.at === null ? `视频 ${formatTime(h.videoSec ?? 0)}` : new Date(h.at).toLocaleString()
      L.push(`  ${when}  ${i === 0 ? t('history.alerted') : t(`status.${h.status}`)}`)
    })
    L.push('')
    L.push(t('brief.disclaimer'))
    return L.join('\n')
  }

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(buildText())
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      // 剪贴板不可用（非安全上下文等）就静默忽略，不打断演示
    }
  }

  return (
    <div className="fixed inset-0 z-50 overflow-auto bg-black/70 p-8">
      <div className="mx-auto w-full max-w-3xl">
        <div className="no-print mb-3 flex items-center gap-2">
          <button
            type="button"
            onClick={() => window.print()}
            className="rounded-md border border-accent/50 bg-accent-dim px-3 py-1.5 text-[12px] text-accent transition-colors hover:bg-accent/20"
          >
            {t('brief.print')}
          </button>
          <button
            type="button"
            onClick={copy}
            className="rounded-md border border-line px-3 py-1.5 text-[12px] text-ink transition-colors hover:bg-raised"
          >
            {copied ? t('brief.copied') : t('brief.copy')}
          </button>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto rounded-md border border-hair px-3 py-1.5 text-[12px] text-ink-dim transition-colors hover:bg-raised hover:text-ink"
          >
            {t('brief.close')} (Esc)
          </button>
        </div>

        <IncidentBrief data={data} event={event} status={status} trail={trail} />
      </div>
    </div>
  )
}
