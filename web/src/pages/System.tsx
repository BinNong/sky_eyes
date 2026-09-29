import { useEffect, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import DataGate from '../components/DataGate'
import SeverityBadge from '../components/SeverityBadge'
import { useDemoStore } from '../store/demoStore'
import { useIncidentStore } from '../store/incidentStore'
import { sendAlertTest, fetchSearchStatus, type SearchStatus } from '../api/dataSource'
import { RUN_STATUS_STYLE, theme } from '../theme/tokens'
import type { JobStatus, JobView } from '../api/dataSource'

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex gap-4 border-t border-hair py-2.5 first:border-t-0">
      <span className="w-40 shrink-0 text-[12px] text-ink-dim">{label}</span>
      <span className="num min-w-0 break-all text-[12px] text-ink">{value}</span>
    </div>
  )
}

function Dot({ ok }: { ok: boolean }) {
  return (
    <span
      className="inline-block h-2 w-2 rounded-full"
      style={{ background: ok ? theme.accent : theme.severity.高 }}
    />
  )
}

/** 任务状态徽章。中断用虚线框，和"已取消"区分开。 */
function RunStatusPill({ status }: { status: JobStatus }) {
  const { t } = useTranslation()
  const s = RUN_STATUS_STYLE[status] ?? RUN_STATUS_STYLE.queued
  return (
    <span
      className="inline-flex shrink-0 items-center gap-1.5 rounded-md border px-1.5 py-0.5 text-[11px]"
      style={{
        color: s.fg,
        background: s.bg,
        borderColor: s.border,
        borderStyle: s.dashed ? 'dashed' : 'solid',
      }}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: s.dot }} />
      {t(`system.status${status.charAt(0).toUpperCase()}${status.slice(1)}`)}
    </span>
  )
}

function fmtSize(mb: number): string {
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`
}

/** 三段式进度条。检测阶段耗时占九成，单独给它一个更细的刻度。 */
function StageBar({ job }: { job: JobView }) {
  const { t } = useTranslation()
  const stages = [
    { key: 'detect', label: t('system.stageDetect') },
    { key: 'cluster', label: t('system.stageCluster') },
    { key: 'understand', label: t('system.stageUnderstand') },
  ]
  const order = ['detect', 'cluster', 'understand']
  const curIdx = order.indexOf(job.stage)

  return (
    <div className="space-y-2">
      {/* 标签在上、进度条在下。若把标签放在条形右侧，每段会被拉得很宽，
          标签浮在段末、离自己的条形很远，宽屏下读不出分组关系。 */}
      <div className="flex items-start gap-3">
        {stages.map((s, i) => {
          const done = curIdx > i || job.status === 'done'
          const active = curIdx === i && job.status === 'running'
          return (
            <div key={s.key} className="min-w-0 flex-1 space-y-1.5">
              <div
                className={`truncate text-[11px] ${
                  active ? 'text-accent' : done ? 'text-ink-dim' : 'text-ink-faint'
                }`}
              >
                {s.label}
                {active ? <span className="num ml-1.5">{job.stage_pct.toFixed(0)}%</span> : null}
              </div>
              <span
                className="block h-1.5 overflow-hidden rounded-full"
                style={{ background: theme.bg.raised }}
              >
                <span
                  className="block h-full rounded-full transition-[width] duration-500"
                  style={{
                    width: done ? '100%' : active ? `${Math.max(3, job.stage_pct)}%` : '0%',
                    background: done ? theme.accent : theme.series[1],
                  }}
                />
              </span>
            </div>
          )
        })}
      </div>
      <div className="flex flex-wrap items-center gap-4 text-[11px] text-ink-faint">
        <span className="num text-[13px] text-ink">{job.pct.toFixed(1)}%</span>
        <span className="num">{t('system.elapsed')} {fmtElapsed(job.elapsed)}</span>
        {typeof job.detail.accident_frames === 'number' ? (
          <span className="num">
            {t('system.detectedFrames')} {job.detail.accident_frames}
          </span>
        ) : null}
        {job.stage === 'understand' && typeof job.detail.total === 'number' ? (
          <span className="num">
            {String(job.detail.done ?? 0)} / {String(job.detail.total)}
          </span>
        ) : null}
      </div>
    </div>
  )
}

function fmtElapsed(sec: number): string {
  const m = Math.floor(sec / 60)
  const s = Math.round(sec % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

function Inner() {
  const { t } = useTranslation()
  const data = useDemoStore((s) => s.data)!
  const s = data.stats

  const mode = useDemoStore((st) => st.mode)
  const health = useDemoStore((st) => st.health)
  const probing = useDemoStore((st) => st.probing)
  const job = useDemoStore((st) => st.job)
  const jobError = useDemoStore((st) => st.jobError)
  const liveJobId = useDemoStore((st) => st.liveJobId)
  const jobs = useDemoStore((st) => st.jobs)
  const probe = useDemoStore((st) => st.probe)
  const attachActive = useDemoStore((st) => st.attachActive)
  const loadJob = useDemoStore((st) => st.loadJob)
  const removeJob = useDemoStore((st) => st.removeJob)
  const runLive = useDemoStore((st) => st.runLive)
  const cancelLive = useDemoStore((st) => st.cancelLive)
  const backToStatic = useDemoStore((st) => st.backToStatic)
  const notice = useDemoStore((st) => st.notice)
  const clearNotice = useDemoStore((st) => st.clearNotice)
  const alertOnRun = useDemoStore((st) => st.alertOnRun)
  const setAlertOnRun = useDemoStore((st) => st.setAlertOnRun)

  const resetAll = useIncidentStore((st) => st.resetAll)
  const [resetDone, setResetDone] = useState(false)
  /** 正在等待二次确认删除的任务 id。删除不可恢复，不做一步式按钮 */
  const [confirming, setConfirming] = useState<string | null>(null)
  /** 企微通道自检：结果就地显示，不弹窗 */
  const [alertTesting, setAlertTesting] = useState(false)
  const [alertTestMsg, setAlertTestMsg] = useState<string | null>(null)

  // 检索编码服务状态。**不走 /api/health**：那里探一次要往 :8001 发 HTTP（超时 6 秒），
  // 塞进 health 会把整页的状态灯一起拖慢。所以这里单独取一次。
  const [searchSt, setSearchSt] = useState<SearchStatus | null>(null)

  // 进页面就探一次。不做这事的话，后端明明起着，页面却一直显示"未启动"，
  // 得先点一下「实时接入」才会刷新——第一次打开的人会以为服务没开。
  // 同时接管可能正在进行中的任务，刷新页面不会丢失进度。
  useEffect(() => {
    void probe()
    void attachActive()
    void fetchSearchStatus().then(setSearchSt)
  }, [probe, attachActive])

  // 一次性提示自动收起，避免"已删除 xx"一直挂在页面上
  useEffect(() => {
    if (!notice) return
    const t = window.setTimeout(() => clearNotice(), 5000)
    return () => window.clearTimeout(t)
  }, [notice, clearNotice])

  const handleReset = () => {
    resetAll()
    setResetDone(true)
    window.setTimeout(() => setResetDone(false), 2000)
  }

  const handleDelete = async (jobId: string) => {
    setConfirming(null)
    await removeJob(jobId)
  }

  const handleTestAlert = async () => {
    setAlertTesting(true)
    setAlertTestMsg(null)
    const result = await sendAlertTest()
    setAlertTesting(false)
    if (!result) {
      setAlertTestMsg(t('system.alertTestNoBackend'))
      return
    }
    setAlertTestMsg(
      result.status === 'sent'
        ? t('system.alertTestSent')
        : t('system.alertTestFailed', { reason: result.reason }),
    )
  }

  const liveReady = health?.ok === true && health.janus.ok
  const busy = job?.status === 'running' || job?.status === 'queued'
  // 老版本后端没有这一段，取可选值；没有就不渲染告警相关的行
  const alertCh = health?.alerts
  const tabCls = (active: boolean) =>
    `rounded-md border px-2.5 py-1 text-[12px] transition-colors ${
      active
        ? 'border-accent/40 bg-accent-dim text-accent'
        : 'border-hair text-ink-dim hover:bg-raised'
    }`

  return (
    <div className="space-y-5 p-5">
      <h1 className="text-[15px] font-medium text-ink">{t('system.title')}</h1>

      {/* ── 数据源模式 ── */}
      <section className="rounded-lg border border-hair bg-panel p-5">
        <h2 className="mb-3 text-[13px] font-medium text-ink-dim">{t('system.dataSource')}</h2>
        <div className="flex items-center gap-2">
          <button type="button" className={tabCls(mode === 'static')} onClick={() => void backToStatic()}>
            {t('system.staticMode')}
          </button>
          <button
            type="button"
            className={tabCls(mode === 'live')}
            disabled={probing || busy}
            onClick={() => {
              void probe().then((h) => {
                if (h?.ok) void runLive()
              })
            }}
          >
            {t('system.liveMode')}
          </button>
          {mode === 'live' && liveJobId ? (
            <span className="num text-[11px] text-ink-faint">
              {t('system.currentRun')} {liveJobId}
            </span>
          ) : null}
        </div>
        <p className="mt-2.5 text-[11px] leading-snug text-ink-faint">
          {mode === 'live' ? t('system.modeLiveHint') : t('system.modeStaticHint')}
        </p>
      </section>

      {/* ── 本地推理服务 ── */}
      <section className="rounded-lg border border-hair bg-panel p-5">
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 className="text-[13px] font-medium text-ink-dim">{t('system.backend')}</h2>
          <button
            type="button"
            onClick={() => void probe()}
            className="rounded-md border border-line px-2.5 py-1 text-[11px] text-ink-dim transition-colors hover:bg-raised"
          >
            {probing ? t('system.probing') : t('system.probeAgain')}
          </button>
        </div>

        {probing && health === null ? (
          <p className="text-[12px] text-ink-faint">{t('system.probing')}</p>
        ) : health === null ? (
          <p className="text-[12px] leading-relaxed text-ink-faint">
            {t('system.backendDown')}
            <br />
            <code className="num mt-2 inline-block rounded-sm border border-hair bg-raised px-2 py-1 text-[11px] text-accent">
              ./scripts/serve_api.sh
            </code>
          </p>
        ) : (
          <>
            <Row
              label={t('system.backend')}
              value={
                <span className="flex items-center gap-2">
                  <Dot ok />
                  {t('system.backendOk')}
                </span>
              }
            />
            <Row
              label={t('system.janus')}
              value={
                <span className="flex items-center gap-2">
                  <Dot ok={health.janus.ok} />
                  {health.janus.ok
                    ? `${health.janus.device ?? '—'} · ${health.janus.model ?? ''} (cuda=${
                        health.janus.cuda ? 'yes' : 'no'
                      })`
                    : health.janus.error || t('system.janusDown')}
                  {health.janus.latency_ms != null ? (
                    <span className="text-ink-faint">{health.janus.latency_ms} ms</span>
                  ) : null}
                </span>
              }
            />
            <Row
              label={t('system.inferEnv')}
              value={
                <span className="flex items-center gap-2">
                  <Dot ok={health.pipeline.ready} />
                  {health.pipeline.python}
                </span>
              }
            />
            {/* 检索编码服务。和告警通道同理：它是「证据检索」这个页面的前置条件，
                而它依赖一条 SSH 隧道 + 远端 GPU 服务，是最容易在现场才发现没起的东西。 */}
            {searchSt ? (
              <Row
                label={t('system.searchService')}
                value={
                  <span className="flex flex-wrap items-center gap-2">
                    <Dot ok={searchSt.index_available && searchSt.encoder.available} />
                    {searchSt.encoder.available ? (
                      <>
                        <span className="text-ink-faint">{searchSt.encoder.url}</span>
                        <span>{searchSt.encoder.device}</span>
                        {searchSt.encoder.gpu?.name ? (
                          <span className="text-ink-faint">
                            {t('system.searchGpuFree', { mb: searchSt.encoder.gpu.mem_free_mb })}
                          </span>
                        ) : null}
                      </>
                    ) : (
                      <span className="text-ink-faint">{t('system.searchEncoderDown')}</span>
                    )}
                    {searchSt.index_available && searchSt.index ? (
                      <span className="text-ink-faint">
                        {t('system.searchIndex', {
                          n: searchSt.index.frame_count,
                          step: searchSt.index.stride_sec,
                        })}
                      </span>
                    ) : (
                      <span className="text-ink-faint">{t('system.searchNoIndex')}</span>
                    )}
                  </span>
                }
              />
            ) : null}
            {/* 告警通道。放在服务状态里而不是藏在设置页：
                现场演示前"通道通不通"和"推理环境在不在"是同等重要的前置条件。 */}
            {alertCh ? (
              <Row
                label={t('system.alertChannel')}
                value={
                  <span className="flex flex-wrap items-center gap-2">
                    <Dot ok={alertCh.configured} />
                    {alertCh.configured ? (
                      <>
                        <span>{alertCh.label}</span>
                        <span className="text-ink-faint">{alertCh.target}</span>
                        <span className="text-ink-faint">
                          ·{' '}
                          {t('system.alertRule', {
                            sev: alertCh.min_severity,
                            max: alertCh.max_per_run,
                          })}
                        </span>
                        <span className="text-ink-faint">
                          {/* 三态：带图 / 用户关掉了 / 平台根本不支持。不能合并成一种说法 */}
                          {!alertCh.image_supported
                            ? t('system.alertImageUnsupported')
                            : alertCh.with_image
                              ? t('system.alertWithImage')
                              : t('system.alertNoImage')}
                        </span>
                      </>
                    ) : (
                      t('system.alertNotConfigured')
                    )}
                  </span>
                }
              />
            ) : null}
            {alertCh && !alertCh.configured ? (
              <p className="pt-2.5 text-[11px] leading-snug text-ink-faint">
                {t('system.alertHowTo')}
                <code className="num mx-1 rounded-sm border border-hair bg-raised px-1.5 py-0.5 text-accent">
                  alert.config.json
                </code>
                <span>{t('system.alertHowTo2')}</span>
              </p>
            ) : null}
            {alertCh && alertCh.notes.length ? (
              <p className="pt-2 text-[11px] leading-snug text-ink-faint">
                {alertCh.notes.join('；')}
              </p>
            ) : null}
          </>
        )}
      </section>

      {/* ── 实时分析 ── */}
      <section className="rounded-lg border border-hair bg-panel p-5">
        <h2 className="mb-2 text-[13px] font-medium text-ink-dim">{t('system.liveAnalyze')}</h2>
        <p className="mb-3 text-[11px] leading-snug text-ink-faint">
          {t('system.liveAnalyzeHint', {
            min: health ? Math.round(health.estimate_sec / 60) : 7,
          })}
        </p>

        {job ? <StageBar job={job} /> : null}

        {/* 告警开关放在「开始分析」正上方：它是这次分析的参数，不是全局设置。
            自检按钮也放这里——演示前点一下，比记一条命令行可靠。 */}
        {alertCh ? (
          <div className="mb-3 rounded-md border border-hair bg-raised px-3 py-2">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
              <label
                className={`flex items-center gap-2 text-[12px] ${
                  alertCh.configured
                    ? 'cursor-pointer text-ink-dim'
                    : 'cursor-not-allowed text-ink-faint'
                }`}
              >
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5"
                  style={{ accentColor: theme.accent }}
                  checked={alertOnRun && alertCh.configured}
                  disabled={!alertCh.configured || busy}
                  onChange={(e) => setAlertOnRun(e.target.checked)}
                />
                {t('system.alertOnRun')}
              </label>

              <button
                type="button"
                onClick={() => void handleTestAlert()}
                disabled={alertTesting || !alertCh.configured}
                className="rounded-md border border-line px-2.5 py-1 text-[11px] text-ink-dim transition-colors hover:bg-base disabled:cursor-not-allowed disabled:opacity-40"
              >
                {alertTesting ? t('system.alertTesting') : t('system.alertTest')}
              </button>

              {alertTestMsg ? (
                <span className="text-[11px] text-ink-faint">{alertTestMsg}</span>
              ) : null}
            </div>
            <p className="mt-2 text-[11px] leading-snug text-ink-faint">
              {alertCh.configured ? t('system.alertOnRunHint') : t('system.alertNeedWebhook')}
            </p>
          </div>
        ) : null}

        <div className="mt-3 flex items-center gap-2">
          <button
            type="button"
            disabled={!liveReady || busy}
            onClick={() => void runLive()}
            className="rounded-md border border-accent/40 bg-accent-dim px-3 py-1.5 text-[12px] text-accent transition-colors hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? t('system.running') : t('system.startAnalyze')}
          </button>
          {busy ? (
            <button
              type="button"
              onClick={() => void cancelLive()}
              className="rounded-md border border-line px-3 py-1.5 text-[12px] text-ink-dim transition-colors hover:bg-raised"
            >
              {t('system.cancelAnalyze')}
            </button>
          ) : null}
          {job?.status === 'done' ? (
            <span className="text-[12px] text-accent">{t('system.analyzeDone')}</span>
          ) : null}
          {!liveReady && health ? (
            <span className="text-[11px] text-ink-faint">{t('system.needJanus')}</span>
          ) : null}
        </div>

        {jobError ? (
          <p className="mt-3 rounded-md border border-sev-high/40 px-3 py-2 text-[11px] leading-snug text-sev-high">
            {jobError}
          </p>
        ) : null}

        {job && job.log.length ? (
          <details className="mt-3">
            <summary className="cursor-pointer text-[11px] text-ink-faint">
              {t('system.showLog')}
            </summary>
            <pre className="num mt-2 max-h-56 overflow-auto rounded-md border border-hair bg-base p-3 text-[10px] leading-relaxed text-ink-dim">
              {job.log.join('\n')}
            </pre>
          </details>
        ) : null}
      </section>

      {/* ── 任务与产物 ──
           不只是"已完成的任务"：中断的任务同样列出来（它们以前在磁盘上留着
           却不出现在界面里，谁也删不掉）。 */}
      {jobs.length > 0 ? (
        <section className="rounded-lg border border-hair bg-panel p-5">
          <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <h2 className="text-[13px] font-medium text-ink-dim">{t('system.recentRuns')}</h2>
            <span className="num text-[11px] text-ink-faint">
              {t('system.runsCount', { n: jobs.length })}
              {health?.runs_mb != null ? ` · ${t('system.runsStorage')} ${fmtSize(health.runs_mb)}` : ''}
            </span>
          </div>
          <p className="mb-3 text-[11px] leading-snug text-ink-faint">{t('system.recentRunsHint')}</p>

          {notice ? (
            <p className="mb-3 rounded-md border border-accent/40 bg-accent-dim px-3 py-2 text-[11px] text-accent">
              {notice}
            </p>
          ) : null}

          <div>
            {jobs.map((j) => {
              const terminal = j.status !== 'running' && j.status !== 'queued'
              const loading = liveJobId === j.job_id
              return (
                <div
                  key={j.job_id}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-t border-hair py-2.5 first:border-t-0"
                >
                  <RunStatusPill status={j.status} />
                  <span className="num text-[11px] text-ink-dim">{j.job_id}</span>
                  {j.video_name ? (
                    <span className="text-[11px] text-ink-faint">{j.video_name}</span>
                  ) : null}
                  {terminal && j.elapsed > 0 ? (
                    <span className="num text-[11px] text-ink-faint">
                      {t('system.elapsed')} {fmtElapsed(j.elapsed)}
                    </span>
                  ) : null}
                  {j.size_mb != null && j.size_mb > 0 ? (
                    <span className="num text-[11px] text-ink-faint">{fmtSize(j.size_mb)}</span>
                  ) : null}
                  {/* 告警结果按任务显示。`alerts` 为 null 与"推了 0 条"是两件事：
                      前者是这次没开告警，后者是开了但没达标事件——不能都写成 0 条。 */}
                  {j.alerts ? (
                    <span
                      className="num text-[11px]"
                      style={{
                        color: j.alerts.counts.failed
                          ? theme.severity.高
                          : theme.text.secondary,
                      }}
                      title={
                        Object.entries(j.alerts.counts.skip_reasons)
                          .map(([k, n]) => `${k} ${n}`)
                          .join('｜') || undefined
                      }
                    >
                      {t('system.runAlerts', { n: j.alerts.counts.sent })}
                      {j.alerts.counts.failed
                        ? ` · ${t('system.runAlertsFailed', { n: j.alerts.counts.failed })}`
                        : ''}
                    </span>
                  ) : j.alert_requested ? (
                    <span className="text-[11px] text-ink-faint">{t('system.runAlertsPending')}</span>
                  ) : null}
                  {terminal && !j.has_report ? (
                    <span className="text-[11px] text-ink-faint" title={j.error ?? ''}>
                      {t('system.noArtifacts')}
                    </span>
                  ) : null}

                  <div className="ml-auto flex items-center gap-2">
                    {loading ? (
                      <span className="text-[11px] text-accent">{t('system.loaded')}</span>
                    ) : j.has_report ? (
                      <button
                        type="button"
                        onClick={() => void loadJob(j.job_id)}
                        className="rounded-md border border-line px-2.5 py-1 text-[11px] text-ink-dim transition-colors hover:bg-raised"
                      >
                        {t('system.loadRun')}
                      </button>
                    ) : null}

                    {terminal ? (
                      confirming === j.job_id ? (
                        <>
                          <span className="text-[11px] text-ink-faint">
                            {t('system.deleteHint')}
                          </span>
                          <button
                            type="button"
                            onClick={() => void handleDelete(j.job_id)}
                            className="rounded-md border border-sev-high/50 px-2.5 py-1 text-[11px] text-sev-high transition-colors hover:bg-sev-high/10"
                          >
                            {t('system.deleteConfirm')}
                          </button>
                          <button
                            type="button"
                            onClick={() => setConfirming(null)}
                            className="rounded-md border border-line px-2.5 py-1 text-[11px] text-ink-dim transition-colors hover:bg-raised"
                          >
                            {t('system.deleteCancel')}
                          </button>
                        </>
                      ) : (
                        <button
                          type="button"
                          onClick={() => setConfirming(j.job_id)}
                          className="rounded-md border border-line px-2.5 py-1 text-[11px] text-ink-faint transition-colors hover:border-sev-high/50 hover:text-sev-high"
                        >
                          {t('system.deleteRun')}
                        </button>
                      )
                    ) : null}
                  </div>
                </div>
              )
            })}
          </div>

          {health?.runs_keep && health.runs_keep.max_gb > 0 ? (
            <p className="mt-3 text-[11px] leading-snug text-ink-faint">
              {t('system.runsKeepHint', {
                gb: health.runs_keep.max_gb,
                keep: health.runs_keep.min_keep,
              })}
            </p>
          ) : null}
        </section>
      ) : null}

      <section className="rounded-lg border border-hair bg-panel p-5">
        <h2 className="mb-2 text-[13px] font-medium text-ink-dim">{t('system.resetState')}</h2>
        <p className="mb-3 text-[11px] text-ink-faint">{t('system.resetHint')}</p>
        <button
          type="button"
          onClick={handleReset}
          className="rounded-md border border-line px-3 py-1.5 text-[12px] text-ink transition-colors hover:bg-raised"
        >
          {resetDone ? t('events.resetDone') : t('events.reset')}
        </button>
      </section>

      <section className="rounded-lg border border-hair bg-panel p-5">
        <h2 className="mb-2 text-[13px] font-medium text-ink-dim">{t('system.runtime')}</h2>
        <Row label={t('system.rowVideo')} value={data.source.video_path} />
        <Row label={t('system.rowWeights')} value={data.source.weights} />
        <Row label={t('system.rowConf')} value={data.source.conf_threshold} />
        <Row
          label={t('system.rowTemp')}
          value={`${data.source.temperature} ${t('system.rowTempNote')}`}
        />
        <Row label={t('system.rowApi')} value={data.source.api_base} />
        <Row
          label={t('system.rowSpec')}
          value={`${data.video.width}×${data.video.height} · ${data.video.fps} fps · ${
            data.video.total_frames
          } ${t('common.frame')} · ${data.video.duration_sec}s`}
        />
        <Row label={t('system.rowGenerated')} value={data.generated_at} />
      </section>

      <section className="rounded-lg border border-hair bg-panel p-5">
        <h2 className="mb-3 text-[13px] font-medium text-ink-dim">{t('system.pipeline')}</h2>
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-[12px] text-ink-dim">
          <span>
            {t('analytics.compute.accidentFrames')}{' '}
            <span className="num text-ink">{s.raw_accident_frames}</span>
          </span>
          <span>
            {t('common.event')} <span className="num text-ink">{s.events}</span>
          </span>
          <span>
            {t('analytics.quality.sent')} <span className="num text-ink">{s.frames_sent}</span>
          </span>
          <span>
            {t('analytics.quality.ok')} <span className="num text-accent">{s.understood}</span>
          </span>
          <span>
            {t('analytics.quality.failed')}{' '}
            <span
              className="num"
              style={{ color: s.failed ? theme.severity.高 : theme.text.primary }}
            >
              {s.failed}
            </span>
          </span>
          <span>
            {t('analytics.quality.followup')}{' '}
            <span className="num text-ink">{s.severity_from_followup}</span>
          </span>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-3 text-[12px] text-ink-faint">
          <span>{t('analytics.severity.note')}</span>
          <SeverityBadge severity="高" size="sm" />
          <span className="num text-ink">{s.event_severity_distribution['高'] ?? 0}</span>
          <SeverityBadge severity="中" size="sm" />
          <span className="num text-ink">{s.event_severity_distribution['中'] ?? 0}</span>
          <SeverityBadge severity="低" size="sm" />
          <span className="num text-ink">{s.event_severity_distribution['低'] ?? 0}</span>
          <span className="num text-ink-faint">rule: {s.event_severity_rule}</span>
        </div>
      </section>
    </div>
  )
}

export default function System() {
  return (
    <DataGate>
      <Inner />
    </DataGate>
  )
}
