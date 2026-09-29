import { useCallback, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import AlertFeed from '../components/AlertFeed'
import DataGate from '../components/DataGate'
import EventTimeline from '../components/EventTimeline'
import SourceList, { SOURCES } from '../components/SourceList'
import StatCard from '../components/StatCard'
import VideoStage from '../components/VideoStage'
import { usePlaybackSync } from '../hooks/usePlaybackSync'
import { useDemoStore } from '../store/demoStore'
import { theme } from '../theme/tokens'
import type { AccidentEvent } from '../types/demo'

function Inner() {
  const { t } = useTranslation()
  const data = useDemoStore((s) => s.data)!
  const videoRef = useRef<HTMLVideoElement>(null)

  const [selected, setSelected] = useState(SOURCES[0].id)
  /** 已触发的告警（事件 id，最新在前）——随着视频播放逐条累积 */
  const [feedIds, setFeedIds] = useState<number[]>([])

  const eventById = useMemo(() => {
    const m = new Map<number, AccidentEvent>()
    for (const e of data.events) m.set(e.event_id, e)
    return m
  }, [data.events])

  const severityOf = useCallback(
    (id: number | null) => (id == null ? null : (eventById.get(id)?.event_severity ?? null)),
    [eventById],
  )

  const handleEventEnter = useCallback((id: number | null) => {
    if (id == null) return
    setFeedIds((prev) => (prev.includes(id) ? prev : [id, ...prev]))
  }, [])

  // 视频进度 -> 帧号 + 命中的检测点，并回调"进入新事件"
  const state = usePlaybackSync(videoRef, data.video.fps, data.timeline, handleEventEnter)

  const startDemo = useCallback(() => {
    setFeedIds([])
    const v = videoRef.current
    if (!v) return
    v.currentTime = 0
    void v.play()
  }, [])

  const seek = useCallback((sec: number) => {
    const v = videoRef.current
    if (!v) return
    v.currentTime = sec
    void v.play()
  }, [])

  const feed = useMemo(
    () => feedIds.map((id) => eventById.get(id)).filter((e): e is AccidentEvent => Boolean(e)),
    [feedIds, eventById],
  )

  const dist = data.stats.event_severity_distribution
  const source = SOURCES.find((s) => s.id === selected) ?? SOURCES[0]

  return (
    <div className="flex h-full flex-col gap-3 p-4">
      {/* 入场序列：指标条 → 主画面 → 时间轴，错峰 110ms（见 index.css 的 .reveal） */}
      <div className="reveal grid shrink-0 grid-cols-4 gap-3" style={{ animationDelay: '0ms' }}>
        <StatCard
          label={t('dashboard.onlineSources')}
          value={SOURCES.length}
          unit="路"
          hint={`${t('dashboard.sourceRealShort')} 1 / ${t('dashboard.sourceDemoShort')} 3`}
        />
        <StatCard
          label={t('dashboard.alerts')}
          value={feed.length}
          unit="起"
          accent={theme.accent}
          hint={`共 ${data.stats.events} 起 · ${t('severity.high')} ${dist['高'] ?? 0} / ${t('severity.medium')} ${dist['中'] ?? 0} / ${t('severity.low')} ${dist['低'] ?? 0}`}
        />
        <StatCard
          label={t('common.representatives')}
          value={data.stats.frames_sent}
          unit={t('common.frame')}
          hint={`${data.stats.raw_accident_frames} 帧 → ${data.stats.events} 起事件`}
        />
        <StatCard
          label={t('dashboard.efficiency')}
          value={(data.stats.frames_sent / data.stats.raw_accident_frames) * 100}
          decimals={1}
          unit="%"
          accent={theme.series[1]}
          hint={t('dashboard.efficiencyHint', {
            total: data.video.total_frames,
            sent: data.stats.frames_sent,
          })}
        />
      </div>

      <div
        className="reveal grid min-h-0 flex-1 grid-cols-[190px_minmax(0,1fr)_330px] gap-3"
        style={{ animationDelay: '110ms' }}
      >
        <SourceList selected={selected} onSelect={setSelected} />
        <VideoStage
          data={data}
          state={state}
          videoRef={videoRef}
          severityOf={severityOf}
          sourceLabel={source}
          onDemo={startDemo}
        />
        <AlertFeed items={feed} />
      </div>

      <div className="reveal shrink-0" style={{ animationDelay: '220ms' }}>
        <EventTimeline data={data} currentSec={state.time} onSeek={seek} />
      </div>
    </div>
  )
}

export default function Dashboard() {
  return (
    <DataGate>
      <Inner />
    </DataGate>
  )
}
