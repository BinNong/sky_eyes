import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useLabelingStore } from '../store/labelingStore'
import { theme } from '../theme/tokens'
import type { LabelItem, LabelStratum } from '../api/dataSource'

/**
 * 真值标注台 —— 逐帧判断「画面里有没有交通事故」。
 *
 * 这一页存在的唯一理由：**没有它就算不出召回率**。
 * `/analytics` 上的精确率只说明"模型报出来的有多少是真的"，
 * 而"真出了事故却没报"的那部分，必须有人真的看过那些帧。
 *
 * 抽样是分层做的（见 scripts/make_label_set.py），四层按**漏检概率**排：
 *   inside   事故区间内却没报 —— 漏检概率最高
 *   boundary 区间外侧几帧未报 —— 事故起止容易被截断
 *   far      离事故区都很远的未报帧 —— 真正的"没事故"时段
 *   detected 检出帧 —— 用来验证"报出来的都是真的"
 *
 * ⚠ 图中的 `assetUrl()` 这里**不能用**：切到实时模式后它会被指到
 *   `/api/runs/<job>/web/`，而标注图永远是静态资源，会全部 404。
 */

const STRATA: LabelStratum[] = ['inside', 'boundary', 'far', 'detected']

/** 标注图始终从站点根取，不受实时模式的资源根切换影响。 */
function frameSrc(rel: string): string {
  return `${import.meta.env.BASE_URL}${rel.replace(/^\/+/, '')}`
}

function pct(v: number | null | undefined, digits = 0): string {
  return v === null || v === undefined ? '—' : `${(v * 100).toFixed(digits)}%`
}

function ciText(ci: [number, number] | null | undefined): string {
  if (!ci) return ''
  return `${(ci[0] * 100).toFixed(0)}–${(ci[1] * 100).toFixed(0)}%`
}

/** 分层进度：每层标了多少、其中判为"有事故"多少 */
function StratumProgress() {
  const { t } = useTranslation()
  const metrics = useLabelingStore((s) => s.metrics)
  const layers = metrics?.strata
  if (!layers) return null

  return (
    <div className="rounded-lg border border-hair bg-panel p-4">
      <div className="mb-2.5 text-[12px] text-ink-dim">{t('labeling.byStratum')}</div>
      <div className="space-y-2">
        {STRATA.map((st) => {
          const d = layers[st]
          if (!d) return null
          const done = d.labeled >= d.sampled && d.sampled > 0
          return (
            <div key={st} className="text-[11px]">
              <div className="flex items-baseline justify-between gap-2">
                <span className={done ? 'text-accent' : 'text-ink-dim'}>
                  {t(`labeling.stratum.${st}`)}
                </span>
                <span className="num text-ink-faint">
                  {d.labeled}/{d.sampled}
                </span>
              </div>
              <div className="mt-1 flex items-center gap-2">
                <span className="h-1 flex-1 overflow-hidden rounded-full" style={{ background: theme.bg.raised }}>
                  <span
                    className="block h-full rounded-full transition-[width] duration-300"
                    style={{
                      width: d.sampled ? `${(d.labeled / d.sampled) * 100}%` : '0%',
                      background: done ? theme.accent : theme.series[1],
                    }}
                  />
                </span>
                {d.labeled > 0 ? (
                  <span className="num shrink-0 text-ink-faint">
                    {t('labeling.accidentOf', { n: d.accident, total: d.labeled })}
                  </span>
                ) : null}
              </div>
              {/* 该层总体大小要写出来：抽样占该层多少，直接决定估计的可信度 */}
              <div className="num mt-0.5 text-[10px] text-ink-faint">
                {t('labeling.population', { n: d.population })}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

/** 指标。没有样本时明确说"算不出来"，不给 0 也不给 100 */
function MetricsPanel() {
  const { t } = useTranslation()
  const metrics = useLabelingStore((s) => s.metrics)

  if (!metrics || !metrics.available) return null

  const missing = metrics.missing ?? []
  const fp = metrics.frame_precision

  return (
    <div className="rounded-lg border border-hair bg-panel p-4">
      <div className="mb-2.5 text-[12px] text-ink-dim">{t('labeling.metrics')}</div>

      {fp ? (
        <div className="mb-3">
          <div className="num text-[20px] leading-none text-ink">{pct(fp.value)}</div>
          <div className="mt-1 text-[11px] text-ink-dim">{t('labeling.framePrecision')}</div>
          {/* 抽样指标必须带区间与样本量，否则会被人当成精确值 */}
          <div className="num mt-0.5 text-[10px] text-ink-faint">
            {t('labeling.ci', { ci: ciText(fp.ci), n: fp.n })}
          </div>
        </div>
      ) : null}

      {metrics.ready && metrics.recall && metrics.miss_rate ? (
        <>
          <div className="mb-3">
            <div className="num text-[20px] leading-none text-accent">
              {pct(metrics.recall.value)}
            </div>
            <div className="mt-1 text-[11px] text-ink-dim">{t('labeling.recall')}</div>
            <div className="num mt-0.5 text-[10px] text-ink-faint">
              {t('labeling.ci', { ci: ciText(metrics.recall.ci), n: metrics.recall.n })}
            </div>
          </div>
          <div className="mb-3">
            <div className="num text-[15px] leading-none text-ink">
              {pct(metrics.miss_rate.value, 1)}
            </div>
            <div className="mt-1 text-[11px] text-ink-dim">{t('labeling.missRate')}</div>
            <div className="num mt-0.5 text-[10px] text-ink-faint">
              {t('labeling.ci', {
                ci: ciText(metrics.miss_rate.ci),
                n: metrics.miss_rate.population,
              })}
            </div>
          </div>
          {metrics.estimate ? (
            <div className="border-t border-hair pt-2.5 text-[11px] leading-relaxed text-ink-dim">
              {t('labeling.estimate', {
                fn: Math.round(metrics.estimate.false_negative),
                total: metrics.estimate.total_frames,
              })}
            </div>
          ) : null}
        </>
      ) : (
        <div>
          <p className="text-[11px] leading-relaxed text-ink-faint">
            {t('labeling.needAllStrata')}
          </p>
          {missing.length > 0 ? (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {missing.map((m) => (
                <span key={m} className="rounded border border-line px-1.5 py-0.5 text-[10px] text-ink-faint">
                  {t(`labeling.stratum.${m}`)}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      )}
    </div>
  )
}

function CurrentCard({ item }: { item: LabelItem }) {
  const { t } = useTranslation()
  const pending = useLabelingStore((s) => s.pending)
  const submit = useLabelingStore((s) => s.submit)

  const btn =
    'w-full rounded-md border px-3 py-2 text-[12px] transition-colors disabled:cursor-not-allowed disabled:opacity-40'

  return (
    <>
      <div className="num text-[20px] leading-none text-ink">
        {t('common.frame')} {item.frame_index}
      </div>
      <div className="num mt-1 text-[11px] text-ink-faint">
        {item.time_sec.toFixed(2)}s
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px]">
        <span className="rounded border border-line px-1.5 py-0.5 text-ink-dim">
          {t(`labeling.stratum.${item.stratum}`)}
        </span>
        {item.detected ? (
          <span className="num text-ink-faint">
            {t('labeling.modelDetected')} {item.confidence?.toFixed(3)}
          </span>
        ) : (
          <span className="text-ink-faint">{t('labeling.modelSilent')}</span>
        )}
      </div>

      <div className="mt-4 space-y-2">
        <button
          type="button"
          disabled={pending}
          onClick={() => void submit(item.id, 'accident')}
          className={`${btn} border-accent/50 bg-accent-dim text-accent hover:bg-accent/20`}
        >
          {t('labeling.hasAccident')}
          <span className="num ml-2 opacity-60">1</span>
        </button>
        <button
          type="button"
          disabled={pending}
          onClick={() => void submit(item.id, 'none')}
          className={`${btn} border-line text-ink hover:bg-raised`}
        >
          {t('labeling.noAccident')}
          <span className="num ml-2 opacity-60">2</span>
        </button>
      </div>
    </>
  )
}

function Inner() {
  const { t } = useTranslation()
  const set = useLabelingStore((s) => s.set)
  const labels = useLabelingStore((s) => s.labels)
  const unavailable = useLabelingStore((s) => s.unavailable)
  const pending = useLabelingStore((s) => s.pending)
  const load = useLabelingStore((s) => s.load)
  const submit = useLabelingStore((s) => s.submit)
  const undo = useLabelingStore((s) => s.undo)

  const [skipped, setSkipped] = useState<Set<string>>(() => new Set())

  useEffect(() => {
    void load()
  }, [load])

  const items = set?.items ?? []
  // "当前待标"就是队首那条。点完按钮 labels 一变，队首自动前进——
  // 不需要自己维护游标，也就不会出现游标与数据不同步的 bug。
  const queue = useMemo(
    () => items.filter((i) => !labels[i.id] && !skipped.has(i.id)),
    [items, labels, skipped],
  )
  const current = queue[0] ?? null

  const lastLabeled = useMemo(() => {
    const e = Object.entries(labels).sort((a, b) => b[1].at - a[1].at)[0]
    return e ? e[0] : null
  }, [labels])

  // 键盘快捷键。标注是纯粹重复劳动，键盘能省掉一半时间。
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (pending) return
      if (current && ev.key === '1') void submit(current.id, 'accident')
      else if (current && ev.key === '2') void submit(current.id, 'none')
      else if (current && (ev.key === ' ' || ev.key === 'Enter')) {
        ev.preventDefault()
        setSkipped((s) => new Set(s).add(current.id))
      } else if (ev.key === 'Backspace' && lastLabeled) {
        ev.preventDefault()
        void undo(lastLabeled)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [current, pending, submit, undo, lastLabeled])

  if (unavailable) {
    return (
      <div className="p-5">
        <p className="text-[13px] text-ink">{t('labeling.needBackend')}</p>
        <code className="num mt-3 inline-block rounded-sm border border-hair bg-raised px-2 py-1 text-[11px] text-accent">
          ./scripts/serve_api.sh
        </code>
      </div>
    )
  }

  if (!set) {
    return (
      <div className="p-5">
        <p className="text-[13px] text-ink">{t('labeling.noSet')}</p>
        <code className="num mt-3 inline-block rounded-sm border border-hair bg-raised px-2 py-1 text-[11px] text-accent">
          python scripts/make_label_set.py
        </code>
      </div>
    )
  }

  const labeled = Object.keys(labels).length
  const total = items.length

  return (
    <div className="space-y-4 p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <h1 className="text-[15px] font-medium text-ink">{t('labeling.title')}</h1>
          <p className="mt-1 text-[11px] leading-snug text-ink-faint">
            {t('labeling.subtitle', { frames: set.video.total_frames, total })}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span
            className="h-1.5 w-40 overflow-hidden rounded-full"
            style={{ background: theme.bg.raised }}
          >
            <span
              className="block h-full rounded-full transition-[width] duration-300"
              style={{ width: total ? `${(labeled / total) * 100}%` : '0%', background: theme.accent }}
            />
          </span>
          <span className="num text-[12px] text-ink-dim">
            {labeled} / {total}
          </span>
          {lastLabeled ? (
            <button
              type="button"
              onClick={() => void undo(lastLabeled)}
              className="rounded-md border border-line px-2.5 py-1 text-[11px] text-ink-dim transition-colors hover:bg-raised"
            >
              {t('labeling.undo')}
            </button>
          ) : null}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="rounded-lg border border-hair bg-panel p-3">
          {current ? (
            <>
              <img
                src={frameSrc(current.image)}
                alt={`frame ${current.frame_index}`}
                className="block w-full rounded"
              />
              <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[11px]">
                <span className="text-ink-faint">{t('labeling.lookHint')}</span>
                <span className="num text-ink-faint">
                  {t('labeling.remaining', { n: queue.length })}
                </span>
              </div>
            </>
          ) : (
            <div className="flex aspect-video flex-col items-center justify-center gap-3">
              <p className="text-[13px] text-ink">{t('labeling.allDone')}</p>
              {skipped.size > 0 ? (
                <button
                  type="button"
                  onClick={() => setSkipped(new Set())}
                  className="rounded-md border border-line px-3 py-1.5 text-[12px] text-ink-dim transition-colors hover:bg-raised"
                >
                  {t('labeling.retrySkipped', { n: skipped.size })}
                </button>
              ) : null}
            </div>
          )}
        </div>

        <aside className="space-y-3">
          <div className="rounded-lg border border-hair bg-panel p-4">
            {current ? (
              <CurrentCard item={current} />
            ) : (
              <p className="text-[12px] leading-relaxed text-ink-dim">
                {t('labeling.allDoneHint')}
              </p>
            )}
          </div>
          <StratumProgress />
          <MetricsPanel />
        </aside>
      </div>
    </div>
  )
}

export default function Labeling() {
  // 刻意**不套 DataGate**：这一页的数据来自抽样清单（/api/labeling），
  // 与 demo.json 无关。把它挂在 demo 数据的加载上，会在后端或产物缺失时
  // 连带把标注台也变成空白页——而那时恰恰最需要它可用。
  return <Inner />
}
