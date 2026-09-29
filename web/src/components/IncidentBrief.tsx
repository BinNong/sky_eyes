import { useState } from 'react'
import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { assetUrl } from '../api/dataSource'
import type { IncidentStatus, TrailEntry } from '../store/incidentStore'
import { paper, severityColor, severityI18nKey, theme } from '../theme/tokens'
import { formatTime } from '../utils/format'
import type { AccidentEvent, DemoData, Representative } from '../types/demo'

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex gap-3 py-1.5">
      <span className="w-20 shrink-0 text-[12px]" style={{ color: paper.sub }}>
        {label}
      </span>
      <span className="min-w-0 flex-1 text-[12.5px]" style={{ color: paper.ink }}>
        {children}
      </span>
    </div>
  )
}

function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <div
      className="mt-5 mb-2 border-b pb-1 text-[13px] font-medium"
      style={{ color: paper.ink, borderColor: paper.line }}
    >
      {children}
    </div>
  )
}

/** 证据帧：浅色纸面上，检测框用深色描边更清楚（深色底那套青色在白纸上会发飘） */
function EvidenceFrame({ rep, video }: { rep: Representative; video: DemoData['video'] }) {
  const box = rep.bbox
  return (
    <div className="flex-1">
      <div
        className="relative overflow-hidden rounded border"
        style={{ borderColor: paper.line }}
      >
        <img src={assetUrl(rep.full)} alt="" className="block w-full" />
        {box ? (
          <span
            className="pointer-events-none absolute border-2"
            style={{
              left: `${(box[0] / video.width) * 100}%`,
              top: `${(box[1] / video.height) * 100}%`,
              width: `${((box[2] - box[0]) / video.width) * 100}%`,
              height: `${((box[3] - box[1]) / video.height) * 100}%`,
              borderColor: severityColor(rep.severity),
            }}
          />
        ) : null}
      </div>
      <div className="mt-1 flex items-center gap-2 text-[11px]" style={{ color: paper.faint }}>
        <span className="num">
          {rep.frame_index} 帧 · {rep.confidence.toFixed(3)}
        </span>
        <span
          className="num rounded px-1"
          style={{ background: paper.headBg, color: paper.sub }}
        >
          {rep.severity ?? '—'}
        </span>
      </div>
    </div>
  )
}

/**
 * 事故简报——一张"纸"。
 *
 * 整个应用是深色屏幕，但这份东西是要打印、要进卷宗的，所以**必须是白底黑字**。
 * 样式全部走 paper token，不复用主应用的深色变量。
 */
export default function IncidentBrief({
  data,
  event,
  status,
  trail,
}: {
  data: DemoData
  event: AccidentEvent
  status: IncidentStatus
  trail: TrailEntry[]
}) {
  const { t } = useTranslation()
  // 生成时间固化一次。直接写在 render 里的话，点「复制文本」触发 re-render 时它会跳变——
  // 一份公文的时间戳在眼皮底下变，观感很差。
  const [generatedAt] = useState(() => new Date().toLocaleString())
  const lead = event.lead
  const sevKey = severityI18nKey(event.event_severity)
  const advice = t(`advice.${sevKey}`, { returnObjects: true }) as string[]
  const sourceName = data.source.video_path.split('/').pop() ?? data.source.video_path

  return (
    <div className="print-area rounded-lg p-8" style={{ background: paper.bg, color: paper.ink }}>
      <div
        className="flex items-start justify-between border-b pb-3"
        style={{ borderColor: paper.line }}
      >
        <div>
          <div className="text-[18px] font-medium">{t('brief.title')}</div>
          <div className="mt-1 text-[11px]" style={{ color: paper.faint }}>
            Sky Eyes · 交通事故监测预警系统
          </div>
        </div>
        <div className="text-right">
          <div className="num text-[14px] font-medium">
            {t('brief.id')} #{String(event.event_id).padStart(3, '0')}
          </div>
          <div className="mt-1 text-[11px]" style={{ color: paper.faint }}>
            {t('brief.generatedAt')} {generatedAt}
          </div>
        </div>
      </div>

      <SectionTitle>{t('brief.eventInfo')}</SectionTitle>
      <div className="grid grid-cols-2 gap-x-8">
        <div>
          <Field label={t('brief.occurredAt')}>
            <span className="num">
              {formatTime(event.start_sec)} – {formatTime(event.end_sec)}
            </span>
          </Field>
          <Field label={t('common.frameRange')}>
            <span className="num">
              {event.start_frame} – {event.end_frame}（{event.num_frames}）
            </span>
          </Field>
          <Field label={t('brief.videoSource')}>{sourceName}</Field>
        </div>
        <div>
          <Field label={t('severity.label')}>
            <span
              className="inline-block rounded px-2 py-0.5 text-[12px] font-medium"
              style={{
                color: severityColor(event.event_severity),
                border: `1px solid ${severityColor(event.event_severity)}66`,
                background: `${severityColor(event.event_severity)}14`,
              }}
            >
              {t(`severity.${sevKey}`)}
            </span>
          </Field>
          <Field label={t('common.peakConfidence')}>
            <span className="num">{event.peak_confidence.toFixed(3)}</span>
          </Field>
          <Field label={t('status.label')}>{t(`status.${status}`)}</Field>
        </div>
      </div>

      <SectionTitle>{t('brief.scene')}</SectionTitle>
      <p className="whitespace-pre-wrap text-[12.5px] leading-relaxed">
        {lead?.description ?? '—'}
      </p>

      <SectionTitle>{t('brief.evidence')}</SectionTitle>
      <div className="flex gap-3">
        {event.representatives.map((rep) => (
          <EvidenceFrame key={rep.frame_index} rep={rep} video={data.video} />
        ))}
      </div>

      <SectionTitle>
        {t('advice.title')}
        <span className="ml-2 text-[11px] font-normal" style={{ color: paper.faint }}>
          （{t('advice.note')}）
        </span>
      </SectionTitle>
      <ol className="ml-4 list-decimal space-y-1 text-[12.5px]">
        {(Array.isArray(advice) ? advice : []).map((a) => (
          <li key={a}>{a}</li>
        ))}
      </ol>

      <SectionTitle>{t('history.title')}</SectionTitle>
      <div className="space-y-1">
        {trail.map((h, i) => (
          <div key={`${h.at}-${i}`} className="flex items-center gap-3 text-[12px]">
            <span className="num w-36 shrink-0" style={{ color: paper.faint }}>
              {h.at === null
                ? `视频 ${formatTime(h.videoSec ?? 0)}`
                : new Date(h.at).toLocaleTimeString()}
            </span>
            <span
              className="h-1.5 w-1.5 rounded-full"
              style={{ background: i === trail.length - 1 ? theme.accent : paper.line }}
            />
            <span>{i === 0 ? t('history.alerted') : t(`status.${h.status}`)}</span>
          </div>
        ))}
      </div>

      <div
        className="mt-6 border-t pt-3 text-[11px] leading-relaxed"
        style={{ borderColor: paper.line, color: paper.faint }}
      >
        {t('brief.disclaimer')}
      </div>
    </div>
  )
}
