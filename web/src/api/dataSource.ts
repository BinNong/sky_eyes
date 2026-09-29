import type { DemoData } from '../types/demo'

/** 数据源模式：静态（读打包好的 JSON） / 实时（调本地后端） */
export type DataMode = 'static' | 'live'

const BASE = import.meta.env.BASE_URL

/**
 * 资源路径前缀。
 *
 * 静态模式下是 Vite 的 BASE_URL（`/`）；切到实时模式后被指到
 * `/api/runs/<job_id>/web/`，这样 `assetUrl()` 一个字都不用改，
 * 页面里所有取图/取视频的地方自动跟着切。
 *
 * 用模块级可变变量而不是把 base 一路往下传：`assetUrl` 被十几个组件调用，
 * 改成 props 会污染一大片组件签名，而它的语义本来就是"当前这次演示的资源根"。
 */
let assetBase: string | null = null

export function setAssetBase(base: string | null): void {
  assetBase = base
}

/* ─────────────────────────── 静态数据源 ─────────────────────────── */

export async function loadDemo(): Promise<DemoData> {
  const url = `${BASE}data/demo.json`
  const res = await fetch(url, { cache: 'no-cache' })
  if (!res.ok) {
    throw new Error(
      `加载 demo.json 失败（HTTP ${res.status}）。请先运行：python scripts/export_demo_data.py`,
    )
  }
  return (await res.json()) as DemoData
}

/**
 * 把数据里的相对路径转成可直接塞进 <img src> / <video src> 的 URL。
 * 路径形如 `frames/roi/xxx.jpg`、`media/detections.mp4`。
 */
export function assetUrl(rel: string | null | undefined): string {
  if (!rel) return ''
  const clean = rel.replace(/^\/+/, '')
  return assetBase ? `${assetBase}${clean}` : `${BASE}${clean}`
}

/* ─────────────────────────── 实时数据源 ─────────────────────────── */

export interface JanusHealth {
  ok: boolean
  base: string
  device?: string
  cuda?: boolean
  model?: string
  error?: string
  latency_ms?: number
}

/**
 * 告警通道的配置状态。与后端 `alerting.channel_view()` 对齐。
 *
 * ⚠ `target` 是**后端脱敏过的**（`…?key=abcd…ef12`、飞书则是 `…/hook/abcd…ef12`）。
 *   真实凭证只存在于后端的环境变量或 `alert.config.json` 里，
 *   永远不要想着在前端把它显示出来——那等于把群发消息的权限挂到公网上。
 */
export interface AlertChannelView {
  /** wecom | dingtalk | feishu | smtp */
  channel: string
  /** 通道的中文名，直接显示 */
  label: string
  configured: boolean
  target: string | null
  min_severity: string
  with_image: boolean
  /**
   * 该通道能不能带现场图。钉钉/飞书的自定义机器人**发不了图**（没有图片消息类型），
   * 所以要显示成"该通道不支持附图"而不是"纯文字"——前者是平台限制，后者是用户的选择。
   */
  image_supported: boolean
  max_per_run: number
  /** 配置来源：arg / env / file / default，用来回答"我改了配置怎么没生效" */
  source: string
  /** 配置有问题时的说明（阈值非法、模板占位符没填、不能附图等） */
  notes: string[]
}

export interface LiveHealth {
  ok: boolean
  janus: JanusHealth
  pipeline: {
    ready: boolean
    python: string
    python_exists: boolean
    script_exists: boolean
  }
  default_video: string
  runs: number
  /** 产物总占用（MB），由后端带缓存统计 */
  runs_mb?: number
  /** 保留策略，用于在界面上说明"什么时候会自动清" */
  runs_keep?: { max_gb: number; min_keep: number }
  estimate_sec: number
  /** 企微告警通道。现场演示前先看这一栏，别等跑完 6 分钟才发现没配 */
  alerts?: AlertChannelView
}

/** interrupted = 上次没收尾（后端被停/崩溃），产物不完整，只能删除不能载入 */
export type JobStatus = 'queued' | 'running' | 'done' | 'error' | 'cancelled' | 'interrupted'

/** 一次运行的推送台账条目。`status` 三态：sent / failed / skipped */
export interface AlertLedgerItem {
  kind: string
  status: string
  reason: string
  event_id: number | null
  severity: string | null
  text_sent: boolean
  image_sent: boolean | null
  at: string
}

/**
 * 一次运行的告警台账。与后端 `alerts.json` 对齐。
 *
 * ⚠ 后端用 `null` 表示「这次压根没开告警」，和「开了但一条都没推」（counts.sent = 0）
 *   是两件事：前者说明没推是对的，后者可能需要人去查配置或阈值。
 *   界面上必须分开显示，不能都写成"0 条"。
 */
export interface RunAlerts {
  version: number
  at: string
  enabled: boolean
  webhook: string | null
  min_severity: string
  with_image: boolean
  max_per_run: number
  counts: {
    sent: number
    failed: number
    skipped: number
    images_sent: number
    skip_reasons: Record<string, number>
  }
  items: AlertLedgerItem[]
}

export interface JobView {
  job_id: string
  video: string
  video_name?: string
  status: JobStatus
  stage: string
  stage_pct: number
  pct: number
  detail: Record<string, unknown>
  error: string | null
  created_at?: number
  finished_at?: number | null
  elapsed: number
  /** 产物体积（MB）。只有终态才算得出来，运行中为 null */
  size_mb?: number | null
  has_report: boolean
  log: string[]
  /** 这次任务实际用的参数（告警开关是按任务选的） */
  pipeline_args?: string[]
  alert_requested?: boolean
  /** 告警台账；`null` = 没开告警（不是"推了 0 条"） */
  alerts?: RunAlerts | null
}

/** 后端没起时 fetch 会抛网络错，统一收敛成 null，让调用方只看得到"不可用"。 */
async function safeJson<T>(url: string, init?: RequestInit, timeoutMs = 4000): Promise<T | null> {
  try {
    const res = await fetch(url, { ...init, signal: AbortSignal.timeout(timeoutMs) })
    if (!res.ok) return null
    return (await res.json()) as T
  } catch {
    return null
  }
}

/** 探活。前端据此决定能否切到实时模式；不可用一律返回 null，不抛错、不阻断演示。 */
export async function probeLive(): Promise<LiveHealth | null> {
  return safeJson<LiveHealth>('/api/health', undefined, 2000)
}

/**
 * 发起任务的结果。
 *
 * ⚠ 必须用 `ok` 做判别式，**不能用 `'error' in result`**——JobView 自己也有
 * `error: string | null` 字段，`in` 判断对两个分支都为真，TS 会把 else 分支
 * 收窄成 never（这个坑编译期就报了错，见 demoStore.ts）。
 */
export type StartJobResult = { ok: true; job: JobView } | { ok: false; error: string }

export async function startJob(video?: string, alert?: boolean): Promise<StartJobResult> {
  const body: Record<string, unknown> = {}
  if (video) body.video = video
  // 只在真的要推送时才带这个字段：少一个字段就少一种"以为关了其实开着"的可能
  if (alert) body.alert = true
  try {
    const res = await fetch('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(8000),
    })
    if (!res.ok) {
      const body = (await res.json().catch(() => null)) as { detail?: string } | null
      return { ok: false, error: body?.detail || `HTTP ${res.status}` }
    }
    return { ok: true, job: (await res.json()) as JobView }
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) }
  }
}

/**
 * 往企微群里发一条通道自检消息。
 *
 * 演示前必点：现场才发现 webhook key 填错，代价是一轮 6 分钟的检测加一次尴尬。
 * 后端没起时返回 null（不抛错，跟探活的处理一致）。
 */
export async function sendAlertTest(): Promise<AlertTestResult | null> {
  return safeJson<AlertTestResult>('/api/alerts/test', { method: 'POST' }, 12000)
}

export interface AlertTestResult {
  configured: boolean
  webhook: string | null
  kind: string
  /** sent / failed / skipped */
  status: string
  reason: string
}

export async function cancelJob(id: string): Promise<JobView | null> {
  return safeJson<JobView>(`/api/jobs/${id}`, { method: 'DELETE' })
}

export interface DeleteResult {
  deleted: string
  freed_mb: number
  runs: number
}

/**
 * 删除某次分析的全部产物。
 *
 * 与 `cancelJob` 是两件事：取消只是停进程、产物留着还能载入；这个是**真删**。
 * 后端会严格校验 job_id（只允许 RUNS_DIR 下的单层目录名），并拒绝删除运行中的任务。
 */
export async function deleteJobArtifacts(id: string): Promise<DeleteResult | null> {
  return safeJson<DeleteResult>(`/api/jobs/${encodeURIComponent(id)}/artifacts`, {
    method: 'DELETE',
  }, 15000)
}

/** 列出最近的任务。用于页面刷新后重新接管正在进行的任务。 */
export async function listJobs(): Promise<JobView[]> {
  const res = await safeJson<{ jobs: JobView[] }>('/api/jobs', undefined, 4000)
  return res?.jobs ?? []
}

/** 拉取某次实时分析的产物，并把资源根切到该任务目录。 */
export async function fetchLiveReport(jobId: string): Promise<DemoData | null> {
  const data = await safeJson<DemoData>(`/api/report?job=${encodeURIComponent(jobId)}`, undefined, 10000)
  if (data) setAssetBase(`/api/runs/${jobId}/web/`)
  return data
}

/**
 * 订阅任务进度（SSE）。返回取消订阅函数。
 *
 * 用 EventSource 而不是轮询：浏览器会自动重连，服务端断开也不会让
 * 进度条卡死在半路。结束时服务端会推 `event: end`，这里据此主动关闭。
 */
export function subscribeJob(jobId: string, onUpdate: (job: JobView) => void): () => void {
  const es = new EventSource(`/api/jobs/${jobId}/stream`)
  es.onmessage = (ev) => {
    try {
      onUpdate(JSON.parse(ev.data) as JobView)
    } catch {
      /* 坏帧忽略 */
    }
  }
  es.addEventListener('end', () => es.close())
  es.onerror = () => es.close()
  return () => es.close()
}

/* ─────────────────── 人工复核反馈（模型可信度） ─────────────────── */

/**
 * 与后端 `server/feedback.py` 对齐。
 *
 * 这一组接口回答的是「模型**答对了没有**」，区别于本文件上面那些
 * （它们回答「系统跑得通不通」）。前者需要人的判断，所以数据来自人工复核。
 */

/** correct = 判定正确（等级也对）；wrong_severity = 是事故但等级判错 */
export type VerdictKind = 'correct' | 'wrong_severity' | 'false_positive'

export interface Verdict {
  run_id: string
  event_id: number
  verdict: VerdictKind
  predicted_severity: string | null
  true_severity: string | null
  at: number
  note: string | null
}

export interface FeedbackMetrics {
  total_events: number | null
  reviewed: number
  correct: number
  wrong_severity: number
  false_positive: number
  /**
   * ⚠ `null` 表示**尚无法计算**（一次复核都没有），不是 0。
   *   界面上必须把这两种情况显示成不同的东西——"还没测过"和"准确率 0%"
   *   是完全不同的结论。后端在分母为 0 时一律给 null。
   */
  precision: number | null
  severity_accuracy: number | null
  /** 行 = 模型判定，列 = 人工真值。误报不进这个矩阵 */
  confusion: Record<string, Record<string, number>>
  confusion_diagonal: number
  evaluated: boolean
  /** 复核覆盖率。只复核 2 起就说"精确率 100%"是有误导性的 */
  coverage: number | null
}

export interface FeedbackView {
  run_id: string
  /** 数据集存在（能读到 demo.json）。后端没起或数据缺失时为 false */
  available: boolean
  verdicts: Verdict[]
  metrics: FeedbackMetrics
}

/** 拉取某个数据集的复核结论。后端不可用时返回 null（不抛错）。 */
export async function fetchFeedback(run: string): Promise<FeedbackView | null> {
  return safeJson<FeedbackView>(`/api/feedback?run=${encodeURIComponent(run)}`, undefined, 5000)
}

/**
 * 提交结果。
 *
 * ⚠ 必须用 `ok` 做判别式，**不能靠 `'error' in result`**——
 *   `Verdict` 天然带不出 error 字段，但 FeedbackView 里的 metrics 是个开放结构，
 *   依赖 `in` 判断在类型收窄上不可靠（这个坑在 job 接口上已经踩过一次）。
 */
export type VerdictResult = { ok: true; view: FeedbackView } | { ok: false; error: string }

export async function putVerdict(payload: {
  run_id: string
  event_id: number
  verdict: VerdictKind
  predicted_severity: string | null
  true_severity?: string | null
}): Promise<VerdictResult> {
  try {
    const res = await fetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(6000),
    })
    if (!res.ok) {
      // 400 是校验失败（例如"真值等级与模型判定相同"），这个原因要显示给用户，
      // 不能像探活那样悄悄吞掉——否则用户点了按钮没反应，不知道哪里错了。
      const body = (await res.json().catch(() => null)) as { detail?: string } | null
      return { ok: false, error: body?.detail || `HTTP ${res.status}` }
    }
    return { ok: true, view: (await res.json()) as FeedbackView }
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) }
  }
}

/** 撤销一条复核记录。 */
export async function clearVerdict(run: string, eventId: number): Promise<FeedbackView | null> {
  return safeJson<FeedbackView>(
    `/api/feedback?run=${encodeURIComponent(run)}&event_id=${eventId}`,
    { method: 'DELETE' },
    6000,
  )
}

/* ─────────────────── 检测召回率的抽样标注 ─────────────────── */

/**
 * 与后端 `server/labeling.py` 对齐。
 *
 * 与上面的「人工复核」互补：复核回答"报出来的有多少是真的"（精确率），
 * 标注回答"没报的帧里有多少其实有事故"（漏检 → 召回率）。
 * 后者需要人真的看过那些没被检出的帧，没有捷径。
 *
 * 四层按**漏检概率**分层，不是随机撒的：
 *   detected  检出帧        → 帧级精确率
 *   inside    事故区间内未报 → 漏检（事故还在持续，模型却断了）
 *   boundary  区间外侧未报   → 漏检（事故起止被截断）
 *   far       远离区未报     → 漏检（真正的"没事故"时段）
 */
export type LabelTruth = 'accident' | 'none'
export type LabelStratum = 'detected' | 'inside' | 'boundary' | 'far'

export interface LabelItem {
  id: string
  frame_index: number
  time_sec: number
  stratum: LabelStratum
  detected: boolean
  confidence: number | null
  image: string
}

export interface LabelSet {
  version: number
  seed: number
  video: {
    src: string
    total_frames: number
    fps: number
    width: number
    height: number
  }
  conf_threshold: number | null
  strata: Record<LabelStratum, { sampled: number; population: number }>
  detected_frames: number
  unchecked_frames: number
  boundary_span: number
  items: LabelItem[]
}

export interface StratumStat {
  stratum: LabelStratum
  sampled: number
  labeled: number
  accident: number
  population: number
  /** 该层抽样帧中判为"有事故"的比例。0 表示"还没标"，null 才是"算不出" */
  rate: number | null
  ci: [number, number] | null
}

export interface RateWithCi {
  value: number
  /** Wilson 95% 区间。抽样指标永远要带它，否则会被当成精确值 */
  ci: [number, number] | null
}

export interface LabelMetrics {
  available: boolean
  reason?: string
  ready?: boolean
  progress?: {
    labeled: number
    total: number
    by_stratum: Record<string, { labeled: number; sampled: number }>
  }
  strata?: Record<string, StratumStat>
  frame_precision?: (RateWithCi & { n: number; population: number }) | null
  recall?: (RateWithCi & { n: number }) | null
  miss_rate?: (RateWithCi & { population: number }) | null
  estimate?: {
    true_positive: number
    false_negative: number
    detected_frames: number
    total_frames: number
    missed_by_stratum: Record<string, number>
  } | null
  /** 还缺哪一层的样本——缺任何一层都算不出召回率 */
  missing?: string[]
}

/** 标注后返回的结构。**不含清单**——清单是静态的，每点一下拖 12KB 没意义 */
export interface LabelingState {
  available: boolean
  reason?: string
  labels: Record<string, { truth: LabelTruth; at: number }>
  metrics: LabelMetrics
}

export interface LabelingView extends LabelingState {
  set: LabelSet | null
}

export async function fetchLabeling(): Promise<LabelingView | null> {
  return safeJson<LabelingView>('/api/labeling', undefined, 8000)
}

export async function putLabel(id: string, truth: LabelTruth): Promise<LabelingState | null> {
  return safeJson<LabelingState>(`/api/labeling/${encodeURIComponent(id)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ truth }),
  }, 8000)
}

export async function clearLabel(id: string): Promise<LabelingState | null> {
  return safeJson<LabelingState>(`/api/labeling/${encodeURIComponent(id)}`, { method: 'DELETE' }, 8000)
}

/* ───────────────────────── 视频检索（用文字找帧） ───────────────────────── */

export interface SearchHit {
  frame_index: number
  time_sec: number
  score: number
  rank: number
  /** 相对 thumb_base 的路径，如 thumbs/f00615.jpg */
  thumb: string | null
}

export interface SearchIndexInfo {
  frame_count: number
  stride: number
  stride_sec: number
  fps: number
}

export interface SearchAnswer {
  query: string
  hits: SearchHit[]
  /** cached_chip = 命中预置查询的本地缓存向量（编码服务挂了也能用）；encoder = 走了远端 */
  source: 'cached_chip' | 'encoder'
  elapsed_ms: number
  top_k: number
  thumb_base: string
  video: string | null
  index: SearchIndexInfo
  score_note: string
}

export interface EncoderStatus {
  available: boolean
  url: string
  model?: string
  device?: string
  dtype?: string
  weights_sha256?: string
  gpu?: { name?: string; mem_free_mb?: number; mem_total_mb?: number }
  uptime_sec?: number
  reason?: string
}

export interface SearchStatus {
  index_available: boolean
  index_dir: string
  reason?: string
  thumb_base: string
  /** 索引元数据：用来和当前播放的视频核对是不是同一段 */
  index?: {
    video: string | null
    fps: number
    stride: number
    stride_sec: number
    frame_count: number
    source_frames: number
    duration_sec: number | null
    dim: number
    chips: string[]
    chips_cached: number
    built_at: string
  }
  encoder: EncoderStatus
}

/**
 * 检索一帧。
 *
 * ⚠ 刻意**不用 `safeJson`**：它把所有失败都压成 null，而这里必须把
 *   「编码服务不可用」（503）和「这个词没搜到」分开——两者在界面上都表现为
 *   "没有结果"，但一个要去查隧道与服务，一个要去改查询词。混成一个响应，
 *   排查的人会在错误的方向上找很久。
 */
export async function searchFrames(
  q: string,
  topK = 12,
): Promise<{ ok: true; data: SearchAnswer } | { ok: false; error: string; unavailable: boolean }> {
  try {
    const res = await fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ q, top_k: topK }),
      signal: AbortSignal.timeout(20000),
    })
    if (!res.ok) {
      const body = (await res.json().catch(() => null)) as { detail?: string } | null
      return {
        ok: false,
        error: body?.detail || `HTTP ${res.status}`,
        unavailable: res.status === 503,
      }
    }
    return { ok: true, data: (await res.json()) as SearchAnswer }
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e), unavailable: true }
  }
}

export async function fetchSearchStatus(): Promise<SearchStatus | null> {
  return safeJson<SearchStatus>('/api/search/status', undefined, 12000)
}
