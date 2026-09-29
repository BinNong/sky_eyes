import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { severityColor, theme } from '../theme/tokens'
import { formatTime } from '../utils/format'
import type { DemoData } from '../types/demo'

/** 相邻标记水平间距小于这个百分比时，时间标签换到第二行，避免文字叠在一起 */
const CROWD_PCT = 5

/** 底部事件时间轴：已播放进度 + 播放头 + 事件标记，点击跳转并播放。 */
export default function EventTimeline({
  data,
  currentSec,
  onSeek,
}: {
  data: DemoData
  currentSec: number
  onSeek: (sec: number) => void
}) {
  const { t } = useTranslation()
  const dur = data.video.duration_sec || 1
  const progress = Math.min(100, Math.max(0, (currentSec / dur) * 100))

  // 事件 17.6s / 18.4s 只差 0.8s，在 38 秒的轴上是 2%，标签必然重叠。
  // 检测到拥挤就把标签错行摆放。
  const markers = useMemo(() => {
    let prevPct = Number.NEGATIVE_INFINITY
    let row = 0
    return data.events.map((e) => {
      const p = (e.start_sec / dur) * 100
      row = p - prevPct < CROWD_PCT ? (row === 0 ? 1 : 0) : 0
      prevPct = p
      return { event: e, pct: p, row }
    })
  }, [data.events, dur])

  return (
    <div className="shrink-0 rounded-lg border border-hair bg-panel px-5 py-3">
      <div className="mb-1.5 flex items-baseline gap-3">
        <span className="text-[13px] text-ink-dim">{t('dashboard.timeline')}</span>
        <span className="num text-[11px] text-ink-faint">
          {formatTime(currentSec)} / {formatTime(dur)}
        </span>
      </div>

      <div className="relative h-[58px]">
        <div className="absolute inset-x-0 top-5 h-px bg-line" />
        <div
          className="absolute top-5 h-px"
          style={{ left: 0, width: `${progress}%`, background: theme.accent }}
        />
        <div
          className="absolute top-2 bottom-2 w-px"
          style={{ left: `${progress}%`, background: theme.accent }}
        />

        {markers.map(({ event: e, pct, row }) => (
          <button
            key={e.event_id}
            type="button"
            onClick={() => onSeek(e.start_sec)}
            className="group absolute top-5 -translate-x-1/2"
            style={{ left: `${pct}%` }}
            title={`${t('common.event')} #${e.event_id} · ${e.start_sec.toFixed(2)}s`}
          >
            <span
              className="block h-3 w-3 -translate-y-1/2 rounded-full transition-transform group-hover:scale-125"
              style={{
                background: severityColor(e.event_severity),
                boxShadow: `0 0 0 3px ${theme.bg.panel}`,
              }}
            />
            <span
              className="num absolute left-1/2 -translate-x-1/2 whitespace-nowrap text-[10px] text-ink-faint group-hover:text-accent"
              style={{ top: row === 0 ? 10 : 24 }}
            >
              {e.start_sec.toFixed(1)}s
            </span>
          </button>
        ))}
      </div>

      <div className="num mt-0.5 flex justify-between text-[10px] text-ink-faint">
        <span>0s</span>
        <span>{(dur / 2).toFixed(1)}s</span>
        <span>{dur.toFixed(1)}s</span>
      </div>
    </div>
  )
}
