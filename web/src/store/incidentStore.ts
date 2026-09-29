import { create } from 'zustand'
import { persist } from 'zustand/middleware'

/** 处置状态机：待处置 → 已派警 → 已处置（可撤回） */
export type IncidentStatus = 'pending' | 'dispatched' | 'resolved'

export const STATUS_FLOW: IncidentStatus[] = ['pending', 'dispatched', 'resolved']

export interface StatusChange {
  status: IncidentStatus
  at: number
}

interface IncidentState {
  current: Record<number, IncidentStatus>
  history: Record<number, StatusChange[]>
  setStatus: (eventId: number, status: IncidentStatus) => void
  resetAll: () => void
}

/**
 * 事件的处置状态。持久化到 localStorage —— 演示时关掉页面再打开，
 * 处置过的记录还在，现场讲"闭环"更有说服力。
 * 需要回到初始状态就用 /system 页的「重置演示数据」。
 */
export const useIncidentStore = create<IncidentState>()(
  persist(
    (set, get) => ({
      current: {},
      history: {},
      setStatus: (eventId, status) => {
        const prev = get().current[eventId] ?? 'pending'
        if (prev === status) return
        const at = Date.now()
        set((s) => ({
          current: { ...s.current, [eventId]: status },
          history: {
            ...s.history,
            [eventId]: [...(s.history[eventId] ?? []), { status, at }],
          },
        }))
      },
      resetAll: () => set({ current: {}, history: {} }),
    }),
    { name: 'sky-eyes-incidents', version: 1 },
  ),
)

export function useIncidentStatus(eventId: number): IncidentStatus {
  return useIncidentStore((s) => s.current[eventId] ?? 'pending')
}

export interface TrailEntry {
  /** null = 「系统自动告警」那一条，用视频内时间标注而不是墙钟时间 */
  at: number | null
  status: IncidentStatus
  /** 仅首条有值：告警发生在视频里的第几秒 */
  videoSec: number | null
}

/** 处置轨迹：以一条「系统自动告警」开头，后面跟实际的状态变更。 */
export function buildTrail(
  eventId: number,
  startSec: number,
  history: Record<number, StatusChange[]>,
): TrailEntry[] {
  return [
    { at: null, status: 'pending', videoSec: startSec },
    ...(history[eventId] ?? []).map((h) => ({ at: h.at, status: h.status, videoSec: null })),
  ]
}
