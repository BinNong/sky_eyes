import { create } from 'zustand'
import {
  cancelJob,
  deleteJobArtifacts,
  fetchLiveReport,
  listJobs,
  loadDemo,
  probeLive,
  setAssetBase,
  startJob,
  subscribeJob,
  type DataMode,
  type JobView,
  type LiveHealth,
} from '../api/dataSource'
import type { DemoData } from '../types/demo'

/** SSE 取消订阅句柄。放在模块级：它不参与渲染，塞进 store 只会引起无谓的重渲染。 */
let unsubscribe: (() => void) | null = null

/**
 * 记住"当前正在展示哪次实时分析"。
 *
 * 一次分析要 6 分多钟，刷新一下就把人踢回静态数据，代价太高——而且现场
 * 演示时刷新是很常见的动作（切投影、误按 F5）。所以把 job id 存进本地存储，
 * 下次进来自动把那份产物换上来；后端没起或产物已删就静默留在静态模式。
 */
const LAST_LIVE_KEY = 'sky-eyes-last-live'

function rememberLive(jobId: string | null): void {
  try {
    if (jobId) localStorage.setItem(LAST_LIVE_KEY, jobId)
    else localStorage.removeItem(LAST_LIVE_KEY)
  } catch {
    /* 隐私模式等场景下不可用，忽略即可 */
  }
}

function readRememberedLive(): string | null {
  try {
    return localStorage.getItem(LAST_LIVE_KEY)
  } catch {
    return null
  }
}

interface DemoState {
  data: DemoData | null
  loading: boolean
  error: string | null

  /** 当前展示的是哪份数据 */
  mode: DataMode
  /** 实时后端探活结果；null = 不可用 */
  health: LiveHealth | null
  probing: boolean
  /** 正在跑 / 刚跑完的任务 */
  job: JobView | null
  jobError: string | null
  /** 一次性提示（删除成功之类），由界面自行清空 */
  notice: string | null
  /** 已经加载为当前数据的任务 id */
  liveJobId: string | null
  /** 后端最近的任务（用于"载入上一次结果"，避免为了展示重跑 7 分钟） */
  jobs: JobView[]
  /**
   * 下次启动分析时是否推送企微告警。
   *
   * **刻意不持久化**（不像 `sky-eyes-last-live`）：默认回到"关闭"是唯一安全的默认值。
   * 上一次演示勾过，这次忘了就变成"莫名其妙往群里发消息"——而群消息收不回来。
   */
  alertOnRun: boolean

  load: () => Promise<void>
  probe: () => Promise<LiveHealth | null>
  attachActive: () => Promise<void>
  loadJob: (jobId: string) => Promise<boolean>
  removeJob: (jobId: string) => Promise<boolean>
  restoreLast: () => Promise<void>
  clearNotice: () => void
  setAlertOnRun: (on: boolean) => void
  runLive: () => Promise<void>
  cancelLive: () => Promise<void>
  backToStatic: () => Promise<void>
}

export const useDemoStore = create<DemoState>((set, get) => {
  /**
   * 订阅任务进度。runLive 和 attachActive 共用，避免两处各写一份回调、
   * 以后改一处忘一处（任务完成时"把产物换上"这段逻辑不能被漏掉）。
   */
  function follow(jobId: string): void {
    unsubscribe?.()
    unsubscribe = subscribeJob(jobId, async (job) => {
      set({ job })
      if (job.status === 'done') {
        // 任务完成：把这次的真实产物换上来，并切到实时模式
        const live = await fetchLiveReport(job.job_id)
        if (live) {
          set({ data: live, mode: 'live', liveJobId: job.job_id })
          rememberLive(job.job_id)
        } else {
          set({ jobError: '分析完成，但读取产物失败' })
        }
        void get().attachActive()
      } else if (job.status === 'error') {
        set({ jobError: job.error || '分析失败' })
      }
    })
  }

  return {
    data: null,
    loading: false,
    error: null,

    mode: 'static',
    health: null,
    probing: false,
    job: null,
    jobError: null,
    notice: null,
    liveJobId: null,
    jobs: [],
    alertOnRun: false,

    load: async () => {
      if (get().data || get().loading) return
      set({ loading: true, error: null })
      try {
        const data = await loadDemo()
        // 期间若 restoreLast 已经把实时产物换上来了，别用静态数据盖掉它。
        // （两个动作都在挂载时发起，谁先完成不确定，所以要在这里再判一次）
        if (get().mode === 'live') set({ loading: false })
        else set({ data, loading: false })
      } catch (e) {
        set({ error: e instanceof Error ? e.message : String(e), loading: false })
      }
    },

    probe: async () => {
      set({ probing: true })
      const health = await probeLive()
      set({ health, probing: false })
      return health
    },

    /**
     * 页面刷新后重新接管正在进行的任务。
     *
     * 没有这一步的话，刷新一次进度条就没了——而后端那 8 分钟的进程还在跑。
     * 路演时手滑刷新一下、或者中途切页面回来，都会以为任务没开始。
     */
    attachActive: async () => {
      const jobs = await listJobs()
      set({ jobs })
      const active = jobs.find((j) => j.status === 'running' || j.status === 'queued')
      if (active) {
        if (get().job?.job_id !== active.job_id) {
          set({ job: active })
          follow(active.job_id)
        }
        return
      }
      // 后端已无在跑任务，但本地还留着"正在跑"的旧状态（如后端重启过）→ 清掉僵尸进度
      if (get().job?.status === 'running' || get().job?.status === 'queued') {
        set({ job: null })
      }
    },

    /**
     * 载入某次已完成分析的产物。
     *
     * 存在的理由很实际：一次分析要 6 分多钟，跑完之后如果刷新了页面、
     * 或者演示到一半切回静态看过别的，就再也回不到那份实时结果了。
     * 有了这个入口，展示只需点一下，不用为了一次讲解再跑一遍。
     */
    loadJob: async (jobId: string) => {
      const live = await fetchLiveReport(jobId)
      if (!live) {
        set({ jobError: '读取该次分析的产物失败' })
        return false
      }
      set({ data: live, mode: 'live', liveJobId: jobId, jobError: null })
      rememberLive(jobId)
      return true
    },

    /**
     * 删除某次分析的产物（真删，不是取消）。
     *
     * 后端会拒绝删除运行中的任务，也会校验 job_id 路径。前端额外做一件事：
     * 如果删的正是当前载入的那份，立刻退回静态——否则页面上的图和视频
     * 会全部指向已经不存在的文件，一片 404。
     */
    removeJob: async (jobId: string) => {
      set({ jobError: null, notice: null })
      const res = await deleteJobArtifacts(jobId)
      if (!res) {
        set({ jobError: '删除失败：后端不可用，或该任务正在运行' })
        return false
      }
      set({ notice: `${jobId} 已删除，释放 ${res.freed_mb} MB` })
      if (get().liveJobId === jobId) await get().backToStatic()
      await get().attachActive()
      return true
    },

    /** 启动时恢复上次载入的实时产物。任何一步不成立都静默留在静态模式。 */
    restoreLast: async () => {
      const saved = readRememberedLive()
      if (!saved || get().liveJobId === saved) return
      const health = await get().probe()
      if (!health?.ok) return // 后端没起是常态，不该打扰演示
      const live = await fetchLiveReport(saved)
      if (!live) {
        rememberLive(null) // 产物已被删除或损坏，别再重试
        return
      }
      set({ data: live, mode: 'live', liveJobId: saved })
    },

    clearNotice: () => set({ notice: null }),

    setAlertOnRun: (on: boolean) => set({ alertOnRun: on }),

    /**
     * 启动一次真实分析。
     *
     * 全流程约 8 分钟（CPU 逐帧检测占九成）。这里**不阻塞界面**：
     * 任务跑在服务端子进程里，进度通过 SSE 回来，用户切到别的页面照样能跑完。
     *
     * 告警开关读的是 store 里的 `alertOnRun`（不是参数），这样页面上两个入口
     * ——「实时接入」和「开始分析」——用的是同一个设置，不会出现"勾了却被忽略"。
     */
    runLive: async () => {
      set({ jobError: null })

      let health = get().health
      if (!health?.ok) health = await get().probe()
      if (!health?.ok) {
        set({ jobError: '无法连接本地推理服务，请先启动后端（./scripts/serve_api.sh）' })
        return
      }
      if (!health.pipeline.ready) {
        set({ jobError: `推理环境不可用：${health.pipeline.python}` })
        return
      }

      const created = await startJob(undefined, get().alertOnRun)
      if (!created.ok) {
        set({ jobError: created.error })
        return
      }

      set({ job: created.job })
      follow(created.job.job_id)
    },

    cancelLive: async () => {
      const job = get().job
      if (!job || job.status !== 'running') return
      const updated = await cancelJob(job.job_id)
      if (updated) set({ job: updated })
    },

    backToStatic: async () => {
      unsubscribe?.()
      unsubscribe = null
      setAssetBase(null)
      rememberLive(null)
      set({ mode: 'static', liveJobId: null, job: null, jobError: null })
      const fresh = await loadDemo().catch(() => null)
      if (fresh) set({ data: fresh })
    },
  }
})
