import { useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import type { TimelinePoint } from '../types/demo'

export interface PlaybackState {
  /** 当前帧号（由 currentTime × fps 推出） */
  frame: number
  /** 当前时间（秒），只保留视频时间轴精度 */
  time: number
  /** 当前帧命中的检测点；当前帧没有检测时向前回看两帧，避免框逐帧闪烁 */
  point: TimelinePoint | null
}

const EMPTY: PlaybackState = { frame: 0, time: 0, point: null }

/**
 * 把视频播放进度同步成帧号 + 命中的检测点。
 *
 * 用 requestAnimationFrame 而不是 timeupdate 事件——timeupdate 每秒只触发约 4 次，
 * 对 25fps 的视频来说太粗，检测框会一跳一跳。
 *
 * onEventChange 只在"进入另一个事件"时回调一次，用来往告警流里追加卡片，
 * 避免每帧都通知父组件。
 */
export function usePlaybackSync(
  videoRef: RefObject<HTMLVideoElement>,
  fps: number,
  timeline: TimelinePoint[],
  onEventChange?: (eventId: number | null) => void,
): PlaybackState {
  const [state, setState] = useState<PlaybackState>(EMPTY)
  const mapRef = useRef<Map<number, TimelinePoint>>(new Map())
  const cbRef = useRef(onEventChange)
  const lastEventRef = useRef<number | null>(null)

  useEffect(() => {
    cbRef.current = onEventChange
  }, [onEventChange])

  useEffect(() => {
    const map = new Map<number, TimelinePoint>()
    for (const p of timeline) map.set(p.frame_index, p)
    mapRef.current = map
  }, [timeline])

  useEffect(() => {
    const v = videoRef.current
    if (!v) return

    let raf = 0
    let running = false
    let prevFrame = -1

    const sample = () => {
      if (Number.isNaN(v.duration)) return
      // +1：元数据里的 frame_index 是 **1-based**（首帧=1），与 ROI 文件名
      // `accident_frame_0049_roi.jpg` 对齐；而 currentTime×fps 得到的是 0-based。
      // 不加这个 1，检测框会晚一帧出现（40ms），且 HUD 显示的帧号与 ROI 编号差 1，
      // 排查问题时会被误导。
      const frame = Math.round(v.currentTime * fps) + 1
      if (frame === prevFrame) return
      prevFrame = frame
      const point =
        mapRef.current.get(frame) ??
        mapRef.current.get(frame - 1) ??
        mapRef.current.get(frame - 2) ??
        null
      setState({ frame, time: v.currentTime, point })

      const eventId = point?.event_id ?? null
      if (eventId !== lastEventRef.current) {
        lastEventRef.current = eventId
        cbRef.current?.(eventId)
      }
    }

    const loop = () => {
      sample()
      raf = requestAnimationFrame(loop)
    }

    // 只在播放期间跑 rAF。原先是无条件常驻 60fps 空转——虽然 setState 有帧号去重
    // 不至于重渲染，但视频暂停时（也就是大部分演示时间）依然在白白烧 CPU 和电量。
    const start = () => {
      if (running) return
      running = true
      raf = requestAnimationFrame(loop)
    }
    const stop = () => {
      if (!running) return
      running = false
      cancelAnimationFrame(raf)
    }

    sample() // 首帧/切换数据源后先把状态摆正

    v.addEventListener('play', start)
    v.addEventListener('playing', start)
    v.addEventListener('pause', stop)
    v.addEventListener('ended', stop)
    v.addEventListener('seeked', sample) // 暂停状态下拖动进度条也要跟上
    v.addEventListener('loadedmetadata', sample)
    if (!v.paused) start()

    return () => {
      stop()
      v.removeEventListener('play', start)
      v.removeEventListener('playing', start)
      v.removeEventListener('pause', stop)
      v.removeEventListener('ended', stop)
      v.removeEventListener('seeked', sample)
      v.removeEventListener('loadedmetadata', sample)
    }
  }, [fps, timeline, videoRef])

  return state
}
