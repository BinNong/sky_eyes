import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import type { ReactNode } from 'react'
import BriefModal from '../components/BriefModal'
import DataGate from '../components/DataGate'
import FeedbackBar from '../components/FeedbackBar'
import SeverityBadge from '../components/SeverityBadge'
import StatusBadge from '../components/StatusBadge'
import { assetUrl } from '../api/dataSource'
import { useDemoStore } from '../store/demoStore'
import { buildTrail, useIncidentStore, useIncidentStatus } from '../store/incidentStore'
import { severityI18nKey, theme } from '../theme/tokens'
import { formatTime } from '../utils/format'
import type { Representative, VideoInfo } from '../types/demo'

/** 在完整视频帧上叠加检测框，坐标系是原始视频分辨率（1280×720）。 */
function BboxFrame({ rep, video }: { rep: Representative; video: VideoInfo }) {
  const box = rep.bbox
  return (
    <div className="relative overflow-hidden rounded-md border border-hair bg-base">
      <img
        src={assetUrl(rep.full)}
        alt={`frame ${rep.frame_index}`}
        className="block w-full"
        loading="lazy"
      />
      {box ? (
        <span
          className="pointer-events-none absolute rounded-sm border-2"
          style={{
            left: `${(box[0] / video.width) * 100}%`,
            top: `${(box[1] / video.height) * 100}%`,
            width: `${((box[2] - box[0]) / video.width) * 100}%`,
            height: `${((box[3] - box[1]) / video.height) * 100}%`,
            borderColor: theme.accent,
            boxShadow: `0 0 12px ${theme.accent}66`,
          }}
        />
      ) : null}
    </div>
  )
}

function SectionTitle({ children }: { children: ReactNode }) {
  return <h2 className="mb-2.5 text-[13px] font-medium text-ink-dim">{children}</h2>
}

function Btn({
  onClick,
  variant = 'ghost',
  children,
}: {
  onClick: () => void
  variant?: 'primary' | 'ghost'
  children: ReactNode
}) {
  const cls =
    variant === 'primary'
      ? 'border-accent/50 bg-accent-dim text-accent hover:bg-accent/20'
      : 'border-line text-ink hover:bg-raised'
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-md border px-3 py-1.5 text-[12px] transition-colors ${cls}`}
    >
      {children}
    </button>
  )
}

function Inner() {
  const { t } = useTranslation()
  const { id } = useParams()
  const data = useDemoStore((s) => s.data)!
  const eventId = Number(id)
  const event = data.events.find((e) => e.event_id === eventId)

  const status = useIncidentStatus(eventId)
  const setStatus = useIncidentStore((s) => s.setStatus)
  const history = useIncidentStore((s) => s.history)
  // ?brief=1 直接打开简报——演示时可以直接给一个打印链接
  const [searchParams] = useSearchParams()
  const [briefOpen, setBriefOpen] = useState(searchParams.get('brief') === '1')

  if (!event) {
    return (
      <div className="p-5">
        <Link to="/events" className="text-[13px] text-accent hover:underline">
          ← {t('detail.backToList')}
        </Link>
      </div>
    )
  }

  const lead = event.lead
  const summary = (lead?.description ?? '').split(/[。\n]/)[0]
  const levels = event.event_severity_levels
  const advice = t(`advice.${severityI18nKey(event.event_severity)}`, {
    returnObjects: true,
  }) as string[]
  const trail = buildTrail(event.event_id, event.start_sec, history)

  return (
    <div className="space-y-6 p-5">
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/events" className="text-[12px] text-ink-dim hover:text-accent">
          ← {t('detail.backToList')}
        </Link>
        <StatusBadge status={status} />
        <div className="ml-auto flex items-center gap-2">
          {status === 'pending' ? (
            <Btn variant="primary" onClick={() => setStatus(event.event_id, 'dispatched')}>
              {t('action.dispatch')}
            </Btn>
          ) : null}
          {status === 'dispatched' ? (
            <Btn variant="primary" onClick={() => setStatus(event.event_id, 'resolved')}>
              {t('action.resolve')}
            </Btn>
          ) : null}
          {status !== 'pending' ? (
            <Btn onClick={() => setStatus(event.event_id, 'pending')}>
              {t('action.revert')}
            </Btn>
          ) : null}
          <Btn onClick={() => setBriefOpen(true)}>{t('action.exportBrief')}</Btn>
        </div>
      </div>

      {/* ① 结论 */}
      <section className="rounded-lg border border-hair bg-panel p-5">
        <SectionTitle>{t('detail.conclusion')}</SectionTitle>
        <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
          <SeverityBadge severity={event.event_severity} size="lg" />
          <div className="num text-[13px] text-ink-dim">
            {event.start_sec.toFixed(2)}s – {event.end_sec.toFixed(2)}s
          </div>
          <div className="num text-[13px] text-ink-dim">
            {t('common.peakConfidence')} {event.peak_confidence.toFixed(3)}
          </div>
          <div className="num text-[13px] text-ink-dim">
            {t('common.frameRange')} {event.start_frame}–{event.end_frame}（{event.num_frames}）
          </div>
        </div>
        {summary ? <p className="mt-3 text-[14px] leading-relaxed text-ink">{summary}。</p> : null}
        <p className="mt-2.5 text-[11px] text-ink-faint">
          {t('detail.ruleNote')}
          {levels.length > 1 ? `（${levels.join(' → ')}）` : ''}
        </p>

        {/* 人工复核就放在结论下面——复核的对象正是上面这个等级判定，
            放到页面底部会让人找不到"我在复核什么"。 */}
        <div className="mt-4 border-t border-hair pt-3.5">
          <FeedbackBar event={event} />
        </div>
      </section>

      {/* ② 证据 */}
      <section>
        <SectionTitle>{t('detail.evidence')}</SectionTitle>
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {event.representatives.map((rep) => (
            <div key={rep.frame_index} className="rounded-lg border border-hair bg-panel p-3">
              <BboxFrame rep={rep} video={data.video} />
              <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[12px]">
                <span className="num text-ink-dim">
                  {t('common.frame')} {rep.frame_index}
                </span>
                <span className="num text-ink-faint">
                  {rep.time_sec != null ? `${rep.time_sec.toFixed(2)}s` : '—'}
                </span>
                <span className="num text-ink-faint">
                  {t('common.confidence')} {rep.confidence.toFixed(3)}
                </span>
                <SeverityBadge severity={rep.severity} size="sm" />
                {rep.is_lead ? (
                  <span className="rounded border border-hair px-1.5 py-0.5 text-[11px] text-accent">
                    {t('common.eventSeverity')}
                  </span>
                ) : null}
              </div>
              <div className="mt-2 h-1 overflow-hidden rounded-full bg-raised">
                <div
                  className="h-full rounded-full"
                  style={{
                    width: `${Math.min(100, rep.confidence * 100)}%`,
                    background: theme.accent,
                  }}
                />
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* ③ 解读 */}
      <section>
        <SectionTitle>{t('detail.insight')}</SectionTitle>
        <div className="space-y-2">
          {event.representatives.map((rep) => (
            <details
              key={rep.frame_index}
              open={rep.is_lead}
              className="rounded-lg border border-hair bg-panel px-4 py-3"
            >
              <summary className="cursor-pointer text-[13px] text-ink-dim">
                <span className="num">
                  {t('common.frame')} {rep.frame_index}
                </span>
                {rep.severity_source === 'followup' ? (
                  <span className="ml-2 text-[11px] text-ink-faint">（追问补判）</span>
                ) : null}
                {rep.ambiguous ? (
                  <span className="ml-2 text-[11px] text-sev-mid">等级不一致</span>
                ) : null}
              </summary>
              <p className="mt-2.5 whitespace-pre-wrap text-[13px] leading-relaxed text-ink">
                {rep.description ?? '—'}
              </p>
            </details>
          ))}
        </div>
        <p className="mt-3 text-[11px] text-ink-faint">{t('detail.aiOriginal')}</p>
      </section>

      {/* ④ 处置 */}
      <section>
        <SectionTitle>{t('detail.disposal')}</SectionTitle>
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <div className="rounded-lg border border-hair bg-panel p-4">
            <div className="mb-2 flex items-baseline gap-2">
              <span className="text-[13px] text-ink">{t('advice.title')}</span>
              <span className="text-[11px] text-ink-faint">（{t('advice.note')}）</span>
            </div>
            {Array.isArray(advice) ? (
              <ol className="ml-4 list-decimal space-y-1.5 text-[12.5px] leading-relaxed text-ink-dim">
                {advice.map((a) => (
                  <li key={a}>{a}</li>
                ))}
              </ol>
            ) : null}
          </div>

          <div className="rounded-lg border border-hair bg-panel p-4">
            <div className="mb-2.5 text-[13px] text-ink">{t('history.title')}</div>
            <div className="space-y-2">
              {trail.map((h, i) => (
                <div key={`${h.at}-${i}`} className="flex items-center gap-3 text-[12px]">
                  <span className="num w-32 shrink-0 text-ink-faint">
                    {h.at === null
                      ? formatTime(h.videoSec ?? 0)
                      : new Date(h.at).toLocaleTimeString()}
                  </span>
                  <span
                    className="h-1.5 w-1.5 shrink-0 rounded-full"
                    style={{
                      background: i === trail.length - 1 ? theme.accent : theme.text.tertiary,
                    }}
                  />
                  <span className={i === trail.length - 1 ? 'text-ink' : 'text-ink-dim'}>
                    {i === 0 ? t('history.alerted') : t(`status.${h.status}`)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      {briefOpen ? (
        <BriefModal
          data={data}
          event={event}
          status={status}
          trail={trail}
          onClose={() => setBriefOpen(false)}
        />
      ) : null}
    </div>
  )
}

export default function EventDetail() {
  return (
    <DataGate>
      <Inner />
    </DataGate>
  )
}
