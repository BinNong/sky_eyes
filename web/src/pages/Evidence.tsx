import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import DataGate from '../components/DataGate'
import { useDemoStore } from '../store/demoStore'
import {
  fetchSearchStatus,
  searchFrames,
  type SearchAnswer,
  type SearchHit,
  type SearchStatus,
} from '../api/dataSource'

/** 秒 → mm:ss.s。带一位小数是因为检索粒度是 0.2 秒，只用秒会看不出差别。 */
function fmtTime(t: number): string {
  const m = Math.floor(t / 60)
  const s = t - m * 60
  return `${m}:${s.toFixed(1).padStart(4, '0')}`
}

function fmtFrame(i: number): string {
  return `#${i}`
}

/**
 * 「用一句话找画面」。
 *
 * 三处刻意的设计，都是为了不让使用者误读结果：
 *
 * ① **不显示"相似度 87%"这种数字。** 实测这个模型的余弦分数全都挤在 0.34~0.41
 *    的窄带里，同一查询前四名常常只差 0.002 —— 窄不代表没用（排序是准的），
 *    但绝对分数**不可解释**：0.35 既可能是最好的结果，也可能是最差的。
 *    界面上因此只给名次和相对差距，并把这句话原样写给使用者看。
 *
 * ② **把「编码服务不可用」和「没搜到」分开显示。** 两者在界面上都表现为
 *    "没有结果"，但一个要去查隧道与远端服务，一个要去改查询词。
 *    后端已经用 503 区分了，这里不能把它压回同一句话。
 *
 * ③ **核对索引与当前视频是不是同一段。** 索引记着自己的 fps / 时长 / 帧数，
 *    与当前加载的视频对不上时给出警告——否则点击结果会跳到错误的位置，
 *    而使用者只会觉得"这个检索不准"。
 */
function EvidenceBody() {
  const { t } = useTranslation()
  const data = useDemoStore((s) => s.data)!

  const [status, setStatus] = useState<SearchStatus | null>(null)
  const [statusErr, setStatusErr] = useState(false)
  const [query, setQuery] = useState('')
  const [answer, setAnswer] = useState<SearchAnswer | null>(null)
  const [error, setError] = useState<{ msg: string; unavailable: boolean } | null>(null)
  const [busy, setBusy] = useState(false)
  const [selected, setSelected] = useState<number | null>(null)

  const videoRef = useRef<HTMLVideoElement>(null)

  useEffect(() => {
    let alive = true
    void fetchSearchStatus().then((s) => {
      if (!alive) return
      if (s) setStatus(s)
      else setStatusErr(true)
    })
    return () => {
      alive = false
    }
  }, [])

  const runSearch = useCallback(
    async (q: string) => {
      const text = q.trim()
      if (!text || busy) return
      setQuery(text)
      setBusy(true)
      setError(null)
      const res = await searchFrames(text, 12)
      if (res.ok) {
        setAnswer(res.data)
        setSelected(res.data.hits[0]?.frame_index ?? null)
      } else {
        setAnswer(null)
        setError({ msg: res.error, unavailable: res.unavailable })
      }
      setBusy(false)
    },
    [busy],
  )

  /** 点结果 = 把播放器跳到那一帧。这是"定位"这个动作的落点。 */
  const jumpTo = useCallback((hit: SearchHit) => {
    setSelected(hit.frame_index)
    const v = videoRef.current
    if (!v) return
    v.currentTime = hit.time_sec
    void v.play().catch(() => {
      /* 浏览器可能拒绝自动播放；画面已经跳过去了，不当作错误 */
    })
  }, [])

  // 索引与当前视频是否同一段。对不上就警告——否则点击结果会跳错位置。
  const mismatch = useMemo(() => {
    const idx = status?.index
    if (!idx) return null
    const v = data.video
    const diffs: string[] = []
    if (Math.abs((idx.duration_sec ?? 0) - v.duration_sec) > 0.5) {
      diffs.push(t('evidence.mismatchDuration', { a: idx.duration_sec, b: v.duration_sec }))
    }
    if (idx.fps && Math.abs(idx.fps - v.fps) > 0.01) {
      diffs.push(t('evidence.mismatchFps', { a: idx.fps, b: v.fps }))
    }
    return diffs.length ? diffs : null
  }, [status, data.video, t])

  const chips = status?.index?.chips ?? []
  const encoderDown = error?.unavailable || status?.encoder.available === false

  return (
    <div className="flex flex-col gap-4 p-5">
      {/* ── 标题 + 索引信息 ── */}
      <div className="reveal flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-[16px] font-medium text-ink">{t('evidence.title')}</h1>
          <p className="mt-1 max-w-[62ch] text-[12px] leading-relaxed text-ink-dim">
            {t('evidence.subtitle')}
          </p>
        </div>
        {status?.index ? (
          <div className="text-right text-[11px] leading-relaxed text-ink-faint">
            <div>
              {t('evidence.indexLine', {
                video: status.index.video,
                n: status.index.frame_count,
                step: status.index.stride_sec,
              })}
            </div>
            {/* built_at 与编码设备：回答"这份索引是什么时候、在哪算的" */}
            <div className="num">
              {status.index.built_at}
              {status.encoder.device ? ` · ${status.encoder.device}` : ''}
            </div>
          </div>
        ) : null}
      </div>

      {/* ── 检索栏 ── */}
      <div className="reveal rounded-lg border border-hair bg-panel p-3">
        <div className="flex gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void runSearch(query)
            }}
            placeholder={t('evidence.placeholder')}
            aria-label={t('evidence.title')}
            className="min-w-0 flex-1 rounded-md border border-line bg-base px-3 py-2 text-[13px] text-ink outline-none transition-colors placeholder:text-ink-faint focus:border-accent"
          />
          <button
            type="button"
            onClick={() => void runSearch(query)}
            disabled={busy || !query.trim()}
            className="shrink-0 rounded-md border border-accent bg-accent-dim px-4 py-2 text-[13px] text-accent transition-opacity disabled:opacity-40"
          >
            {busy ? t('evidence.searching') : t('evidence.search')}
          </button>
        </div>

        {/* 预置查询。它们在建索引时就把向量算好存进了索引，
            所以**编码服务不可达时这几条仍然能用**——演示现场断网的兜底。 */}
        {chips.length ? (
          <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
            <span className="text-[11px] text-ink-faint">{t('evidence.chipsLabel')}</span>
            {chips.map((c) => (
              <button
                key={c}
                type="button"
                onClick={() => void runSearch(c)}
                className="rounded border border-hair px-2 py-0.5 text-[11px] text-ink-dim transition-colors hover:border-line hover:bg-raised hover:text-ink"
              >
                {c}
              </button>
            ))}
            <span className="text-[11px] text-ink-faint">{t('evidence.chipsOffline')}</span>
          </div>
        ) : null}
      </div>

      {/* ── 前置条件不满足时的明确提示（不是"没有结果"） ── */}
      {statusErr ? (
        <Notice>{t('evidence.backendDown')}</Notice>
      ) : status && !status.index_available ? (
        <Notice>
          <span>{t('evidence.noIndex')}</span>
          <code className="num mx-1 rounded-sm border border-hair bg-raised px-1.5 py-0.5 text-accent">
            python scripts/build_search_index.py
          </code>
          <span className="block pt-1 text-ink-faint">{status.reason}</span>
        </Notice>
      ) : null}

      {encoderDown && status?.index_available ? (
        <Notice>
          <span>{t('evidence.encoderDown')}</span>
          <span className="block pt-1 text-ink-faint">
            {status.encoder.reason || t('evidence.encoderDownHint')}
          </span>
        </Notice>
      ) : null}

      {error && !error.unavailable ? <Notice>{error.msg}</Notice> : null}

      {mismatch ? (
        <Notice>
          <span>{t('evidence.mismatch')}</span>
          {mismatch.map((d) => (
            <span key={d} className="block pl-3 text-ink-faint">
              · {d}
            </span>
          ))}
        </Notice>
      ) : null}

      {/* ── 主区：左播放器 / 右结果 ── */}
      <div className="reveal grid min-h-0 gap-4 lg:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
        <div className="min-w-0 rounded-lg border border-hair bg-panel p-3">
          <div className="mb-2 flex items-baseline justify-between gap-2">
            <span className="text-[12px] text-ink-dim">{t('evidence.player')}</span>
            {selected !== null ? (
              <span className="num text-[11px] text-accent">
                {t('evidence.atFrame', { f: fmtFrame(selected) })}
              </span>
            ) : null}
          </div>
          <video
            ref={videoRef}
            src={`/${data.video.src}`}
            controls
            preload="metadata"
            className="w-full rounded-md border border-hair bg-black"
          />
          {answer ? (
            <p className="mt-2 text-[11px] leading-relaxed text-ink-faint">
              {t('evidence.scoreNote')}
            </p>
          ) : null}
        </div>

        <div className="min-w-0 rounded-lg border border-hair bg-panel p-3">
          <div className="mb-2 flex items-baseline justify-between gap-2">
            <span className="text-[12px] text-ink-dim">{t('evidence.results')}</span>
            {answer ? (
              <span className="num text-[11px] text-ink-faint">
                {t('evidence.meta', {
                  ms: answer.elapsed_ms,
                  source:
                    answer.source === 'cached_chip'
                      ? t('evidence.srcCached')
                      : t('evidence.srcEncoder'),
                })}
              </span>
            ) : null}
          </div>

          {!answer ? (
            <p className="py-10 text-center text-[12px] text-ink-faint">
              {t('evidence.empty')}
            </p>
          ) : (
            <div className="grid max-h-[52vh] grid-cols-2 gap-2 overflow-auto pr-1 sm:grid-cols-3 lg:grid-cols-2">
              {answer.hits.map((h) => {
                const on = selected === h.frame_index
                return (
                  <button
                    key={h.frame_index}
                    type="button"
                    onClick={() => jumpTo(h)}
                    title={`${fmtTime(h.time_sec)} · ${h.score.toFixed(4)}`}
                    className={[
                      'group relative overflow-hidden rounded border text-left transition-colors',
                      on ? 'border-accent' : 'border-hair hover:border-line',
                    ].join(' ')}
                  >
                    {h.thumb ? (
                      <img
                        src={`${answer.thumb_base}${h.thumb}`}
                        alt={t('evidence.hitAlt', { n: h.rank + 1 })}
                        loading="lazy"
                        className="block w-full"
                      />
                    ) : (
                      <div className="aspect-video w-full bg-raised" />
                    )}
                    <div className="flex items-baseline justify-between gap-1 px-1.5 py-1">
                      <span className="num text-[11px] text-ink">
                        {fmtTime(h.time_sec)}
                      </span>
                      <span className="num text-[10px] text-ink-faint">
                        {h.rank + 1} · {h.score.toFixed(3)}
                      </span>
                    </div>
                    {on ? (
                      <span className="absolute inset-y-0 left-0 w-[2px] bg-accent" />
                    ) : null}
                  </button>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/**
 * 前置条件不满足时的提示。
 *
 * ⚠ 刻意**不用橙色**：橙是「中等级」的语义色，等级三色是全局唯一语义色，
 *   借用会让「这里出问题了」和「这起事故是中等级」看起来是同一件事（§6.1 硬规矩）。
 *   这里用中性描边 + 强调青的措辞来表达"需要你处理"，与任务状态那一族保持一致。
 */
function Notice({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-line bg-panel px-3 py-2 text-[12px] leading-relaxed text-ink-dim">
      {children}
    </div>
  )
}

export default function Evidence() {
  return (
    <DataGate>
      <EvidenceBody />
    </DataGate>
  )
}
