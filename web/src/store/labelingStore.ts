import { create } from 'zustand'
import {
  clearLabel,
  fetchLabeling,
  putLabel,
  type LabelMetrics,
  type LabelSet,
  type LabelTruth,
} from '../api/dataSource'

interface LabelingStore {
  /** 抽样清单（题目）。由 scripts/make_label_set.py 生成，静态不变 */
  set: LabelSet | null
  /** 人工答案。key = item id */
  labels: Record<string, { truth: LabelTruth; at: number }>
  metrics: LabelMetrics | null
  /** 后端不可用——标注无处保存，界面要说清楚而不是给个点了没反应的按钮 */
  unavailable: boolean
  pending: boolean
  load: () => Promise<void>
  submit: (id: string, truth: LabelTruth) => Promise<void>
  undo: (id: string) => Promise<void>
}

/**
 * 检测召回率的抽样标注。
 *
 * 与 `feedbackStore`（事件级人工复核）是两件事，不要合并：
 *   复核 判的是**事件等级对不对**，一条 = 一起事故
 *   标注 判的是**这一帧有没有事故**，一条 = 一帧
 * 数据来源、粒度、指标口径都不同，混在一起会两个都算不准。
 */
export const useLabelingStore = create<LabelingStore>()((set, get) => ({
  set: null,
  labels: {},
  metrics: null,
  unavailable: false,
  pending: false,

  load: async () => {
    const v = await fetchLabeling()
    if (!v) {
      set({ unavailable: true })
      return
    }
    set({
      unavailable: false,
      labels: v.labels,
      metrics: v.metrics,
      // 标注接口的返回里不带清单（它是静态的，每次拖 12KB 没意义），
      // 所以这里要保留已加载的那份。
      set: v.set ?? get().set,
    })
  },

  submit: async (id, truth) => {
    set({ pending: true })
    const v = await putLabel(id, truth)
    if (!v) {
      set({ pending: false, unavailable: true })
      return
    }
    set({ pending: false, labels: v.labels, metrics: v.metrics })
  },

  undo: async (id) => {
    const v = await clearLabel(id)
    if (!v) {
      set({ unavailable: true })
      return
    }
    set({ labels: v.labels, metrics: v.metrics })
  },
}))
