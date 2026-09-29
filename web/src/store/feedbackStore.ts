import { create } from 'zustand'
import {
  clearVerdict,
  fetchFeedback,
  putVerdict,
  type FeedbackView,
  type Verdict,
  type VerdictKind,
} from '../api/dataSource'

/** 静态 demo 数据集在后端的 run_id，与 `server/feedback.py` 的 STATIC_RUN 对齐。 */
export const STATIC_RUN = 'static'

interface FeedbackState {
  run: string
  view: FeedbackView | null
  /**
   * 后端不可用。
   *
   * 与「还没有复核记录」是两件事：前者要提示去启动后端，后者要显示"还没复核过"。
   * 两者 `view` 都是空，只看 view 分不出来，所以单独记一个标志。
   */
  unavailable: boolean
  pending: boolean
  /** 后端返回的校验错误（例如"真值等级与模型判定相同"），要显示给用户 */
  error: string | null

  load: (run: string) => Promise<void>
  submit: (args: {
    eventId: number
    verdict: VerdictKind
    predictedSeverity: string | null
    trueSeverity?: string | null
  }) => Promise<boolean>
  withdraw: (eventId: number) => Promise<void>
  clearError: () => void
}

/**
 * 人工复核记录。**存在后端**，不是 localStorage。
 *
 * 这是与 `incidentStore`（处置状态，存本地）不同的取舍：复核记录是模型可信度的
 * 依据，两份互相矛盾的复核数据会直接让指标不可信，所以它必须单一来源。
 */
export const useFeedbackStore = create<FeedbackState>()((set, get) => ({
  run: STATIC_RUN,
  view: null,
  unavailable: false,
  pending: false,
  error: null,

  load: async (run) => {
    set({ run })
    const view = await fetchFeedback(run)
    // 竞态保护：请求飞行期间用户又切了数据集（静态 ↔ 实时），
    // 这次的结果就作废——否则会把上一个数据集的指标显示在当前页面上。
    if (get().run !== run) return
    set({ view, unavailable: view === null })
  },

  submit: async ({ eventId, verdict, predictedSeverity, trueSeverity }) => {
    const run = get().run
    set({ pending: true, error: null })
    const res = await putVerdict({
      run_id: run,
      event_id: eventId,
      verdict,
      predicted_severity: predictedSeverity,
      true_severity: trueSeverity ?? null,
    })
    if (get().run !== run) return false
    if (!res.ok) {
      set({ pending: false, error: res.error })
      return false
    }
    set({ view: res.view, unavailable: false, pending: false, error: null })
    return true
  },

  withdraw: async (eventId) => {
    const run = get().run
    const view = await clearVerdict(run, eventId)
    if (get().run !== run) return
    set({ view, unavailable: view === null, error: null })
  },

  clearError: () => set({ error: null }),
}))

/** 某起事件当前的复核结论（本数据集内）。 */
export function useVerdictOf(eventId: number): Verdict | undefined {
  return useFeedbackStore((s) => s.view?.verdicts.find((v) => v.event_id === eventId))
}
