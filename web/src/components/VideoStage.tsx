import { useEffect, useState } from 'react'
import type { CSSProperties, RefObject } from 'react'
import { useTranslation } from 'react-i18next'
import { assetUrl } from '../api/dataSource'
import { severityColor, theme } from '../theme/tokens'
import { formatTime, pct } from '../utils/format'
import type { DemoData } from '../types/demo'
import type { PlaybackState } from '../hooks/usePlaybackSync'

const RATES = [0.5, 1, 2] as const

export default function VideoStage({
  data,
  state,
  videoRef,
  severityOf,
  sourceLabel,
  onDemo,
}: {
  data: DemoData
  state: PlaybackState
  videoRef: RefObject<HTMLVideoElement>
  severityOf: (eventId: number | null) => string | null
  sourceLabel: { name: string; real: boolean }
  onDemo: () => void
}) {
  const { t } = useTranslation()
  const [playing, setPlaying] = useState(false)
  const [rate, setRate] = useState(1)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    const v = videoRef.current
    if (!v) return
    const onPlay = () => setPlaying(true)
    const onPause = () => setPlaying(false)
    v.addEventListener('play', onPlay)
    v.addEventListener('pause', onPause)
    return () => {
      v.removeEventListener('play', onPlay)
      v.removeEventListener('pause', onPause)
    }
  }, [videoRef])

  const toggle = () => {
    const v = videoRef.current
    if (!v) return
    if (v.paused) void v.play()
    else v.pause()
  }

  const seek = (sec: number) => {
    const v = videoRef.current
    if (!v) return
    v.currentTime = sec
  }

  const changeRate = (r: number) => {
    setRate(r)
    if (videoRef.current) videoRef.current.playbackRate = r
  }

  const box = state.point?.bbox ?? null
  const sev = severityOf(state.point?.event_id ?? null)
  const boxColor = sev ? severityColor(sev) : theme.accent
  const duration = data.video.duration_sec || 0

  return (
    <div className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-hair bg-panel">
      <div className="flex shrink-0 items-center gap-2.5 border-b border-hair px-3.5 py-2.5">
        <span
          className="h-1.5 w-1.5 rounded-full"
          style={{ background: playing ? theme.accent : theme.text.tertiary }}
        />
        <span className="text-[13px] text-ink">{sourceLabel.name}</span>
        {sourceLabel.real ? (
          <span className="rounded border border-accent/40 px-1.5 py-0.5 text-[11px] text-accent">
            {t('dashboard.sourceReal')}
          </span>
        ) : (
          <span className="rounded border border-hair px-1.5 py-0.5 text-[11px] text-ink-faint">
            {t('dashboard.sourceDemo')}
          </span>
        )}
        <span className="num ml-auto text-[11px] text-ink-faint">
          {data.video.width}×{data.video.height} · {data.video.fps}fps
        </span>
      </div>

      {!sourceLabel.real ? (
        <div className="shrink-0 border-b border-hair px-3.5 py-1.5 text-[11px] text-ink-faint">
          演示数据：复用同一段视频片段，非独立摄像头接入
        </div>
      ) : null}

      {/* 视频区：占满剩余高度，并用容器查询单位让 16:9 的框在「按宽铺满」与
          「按高铺满」之间取小者（见 index.css 的 .video-fit / .video-box）。
          这样视频永远完整可见，下方控件条也永远不会被挤出视野——曾经在
          1600×900 下整个控件条被裁掉，「开始演示」按钮在界面上根本不存在。 */}
      <div className="video-fit relative flex min-h-0 flex-1 items-center justify-center bg-black">
        {failed ? (
          <div className="absolute inset-0 flex items-center justify-center px-6 text-center text-[13px] text-ink-dim">
            视频加载失败：{data.video.src}
            <br />
            请确认已运行 python scripts/export_demo_data.py
          </div>
        ) : null}

        <div
          className="video-box relative"
          style={
            {
              '--ar': `${data.video.width} / ${data.video.height}`,
              '--ar-num': data.video.width / data.video.height,
            } as CSSProperties
          }
        >
          <video
            ref={videoRef}
            src={assetUrl(data.video.src)}
            className="h-full w-full"
            playsInline
            muted
            preload="auto"
            onError={() => setFailed(true)}
          />

          {box ? (
            <div
              className="pointer-events-none absolute rounded-sm border-2"
              style={{
                left: pct(box[0], data.video.width),
                top: pct(box[1], data.video.height),
                width: pct(box[2] - box[0], data.video.width),
                height: pct(box[3] - box[1], data.video.height),
                borderColor: boxColor,
                boxShadow: `0 0 14px ${boxColor}88`,
              }}
            >
              <span
                className="num absolute -top-[22px] left-0 whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium"
                style={{ background: boxColor, color: '#0B0F18' }}
              >
                {state.point?.confidence.toFixed(3)}
              </span>
            </div>
          ) : null}

          <div className="pointer-events-none absolute bottom-2 left-2 flex items-center gap-3 rounded bg-black/60 px-2.5 py-1.5">
            <span className="num text-[11px] text-ink-dim">
              {t('common.frame')} {state.frame}
            </span>
            <span className="num text-[11px] text-ink-faint">{formatTime(state.time)}</span>
            {state.point ? (
              <>
                <span className="num text-[11px]" style={{ color: boxColor }}>
                  {t('common.confidence')} {state.point.confidence.toFixed(3)}
                </span>
                <span className="num text-[11px] text-ink-faint">
                  {t('dashboard.boxes')} {state.point.bbox ? 1 : 0}
                </span>
              </>
            ) : (
              <span className="text-[11px] text-ink-faint">{t('dashboard.noDetection')}</span>
            )}
          </div>
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-3 border-t border-hair px-3.5 py-2.5">
        <button
          type="button"
          onClick={toggle}
          className="rounded-md border border-line px-3 py-1.5 text-[12px] text-ink transition-colors hover:bg-raised"
        >
          {playing ? t('dashboard.pause') : t('dashboard.play')}
        </button>
        <button
          type="button"
          onClick={onDemo}
          className="rounded-md border border-accent/50 bg-accent-dim px-3 py-1.5 text-[12px] text-accent transition-colors hover:bg-accent/20"
        >
          {t('dashboard.startDemo')}
        </button>

        <input
          type="range"
          min={0}
          max={duration}
          step={0.02}
          value={state.time}
          onChange={(e) => seek(Number(e.target.value))}
          className="h-1 min-w-0 flex-1 accent-accent"
          aria-label={t('dashboard.timeline')}
        />

        <span className="num shrink-0 text-[11px] text-ink-faint">
          {formatTime(state.time)} / {formatTime(duration)}
        </span>

        <div className="flex shrink-0 items-center overflow-hidden rounded-md border border-hair">
          {RATES.map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => changeRate(r)}
              className={[
                'px-2 py-1 text-[11px] transition-colors',
                rate === r ? 'bg-accent-dim text-accent' : 'text-ink-faint hover:text-ink',
              ].join(' ')}
            >
              {r}×
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
