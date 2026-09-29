import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useDemoStore } from '../store/demoStore'
import { STATIC_RUN, useFeedbackStore, useVerdictOf } from '../store/feedbackStore'
import { severityI18nKey, VERDICT_STYLE } from '../theme/tokens'
import type { AccidentEvent } from '../types/demo'
import type { VerdictKind } from '../api/dataSource'

const LEVELS = ['低', '中', '高'] as const

const OPT =
  'rounded-md border border-line px-2.5 py-1 text-[11px] text-ink-dim transition-colors hover:bg-raised disabled:cursor-not-allowed disabled:opacity-40'

/**
 * 事件详情页的人工复核条。
 *
 * 这是整个"模型可信度"的**唯一数据来源**——界面上其他所有"质量"数字
 * （18/18 成功、0 退化）都只说明模型有应答，不说明它答对了。
 * 没有这一条，`/analytics` 上的精确率与等级准确率就永远只能显示"尚未复核"。
 *
 * 三档判定是刻意设计的：把「等级判错」和「根本不是事故」分开，
 * 因为它们是两种完全不同的失效——前者检测没错，后者检测就是错的。
 * 混成一档会让看板无法指出该去修哪一环。
 */
export default function FeedbackBar({ event }: { event: AccidentEvent }) {
  const { t } = useTranslation()
  const liveJobId = useDemoStore((s) => s.liveJobId)
  // 复核记录按数据集隔离：静态 demo 与每次实时分析的 event_id 会重复，
  // 混在一起会张冠李戴（后端也按 run_id 分开存）。
  const run = liveJobId ?? STATIC_RUN

  const load = useFeedbackStore((s) => s.load)
  const submit = useFeedbackStore((s) => s.submit)
  const withdraw = useFeedbackStore((s) => s.withdraw)
  const unavailable = useFeedbackStore((s) => s.unavailable)
  const pending = useFeedbackStore((s) => s.pending)
  const error = useFeedbackStore((s) => s.error)
  const mine = useVerdictOf(event.event_id)

  const [picking, setPicking] = useState(false)

  useEffect(() => {
    void load(run)
  }, [load, run])

  // 切事件或切数据集时收起等级选择，否则展开态会跟着跑到下一个事件上
  useEffect(() => {
    setPicking(false)
  }, [event.event_id, run])

  const predicted = event.event_severity

  // 后端不可用时明确说清"记录无处保存"，而不是给个点了没反应的按钮。
  // 静态演示本身不依赖后端，所以这里不能报错、不能挡路。
  if (unavailable) {
    return (
      <p className="text-[11px] leading-snug text-ink-faint">{t('feedback.needBackend')}</p>
    )
  }

  const decide = (verdict: VerdictKind, trueSeverity?: string | null) => {
    void submit({
      eventId: event.event_id,
      verdict,
      predictedSeverity: predicted,
      trueSeverity,
    })
  }

  if (mine) {
    const st = VERDICT_STYLE[mine.verdict]
    return (
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="text-[12px] text-ink-dim">{t('feedback.title')}</span>
        <span
          className="flex items-center gap-2 rounded-md border px-2 py-0.5 text-[11px]"
          style={{ color: st.fg, background: st.bg, borderColor: st.border }}
        >
          <span className="h-3 w-[2px] rounded-full" style={{ background: st.bar }} />
          {mine.verdict === 'wrong_severity'
            ? t('feedback.wrongSeverity', {
                level: t(`severity.${severityI18nKey(mine.true_severity)}`),
              })
            : t(`feedback.kind.${mine.verdict}`)}
        </span>
        <span className="num text-[11px] text-ink-faint">
          {new Date(mine.at * 1000).toLocaleTimeString()}
        </span>
        <button
          type="button"
          onClick={() => void withdraw(event.event_id)}
          className="ml-auto rounded-md border border-line px-2.5 py-1 text-[11px] text-ink-dim transition-colors hover:bg-raised"
        >
          {t('feedback.withdraw')}
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-2">
        <span className="mr-1 text-[12px] text-ink-dim">{t('feedback.title')}</span>
        {/* 模型未判出等级时禁用「判定正确」——后端也会拒绝。
            "判定正确"却在说一个不存在的等级，那句话没有内容。 */}
        <button
          type="button"
          className={OPT}
          disabled={pending || !predicted}
          onClick={() => decide('correct')}
        >
          {t('feedback.kind.correct')}
        </button>
        <button
          type="button"
          className={OPT}
          disabled={pending}
          onClick={() => setPicking((v) => !v)}
        >
          {t('feedback.kind.wrong_severity')}
        </button>
        <button
          type="button"
          className={OPT}
          disabled={pending}
          onClick={() => decide('false_positive')}
        >
          {t('feedback.kind.false_positive')}
        </button>
      </div>

      {picking ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] text-ink-faint">{t('feedback.pickLevel')}</span>
          {LEVELS.filter((lv) => lv !== predicted).map((lv) => (
            <button
              key={lv}
              type="button"
              className={OPT}
              disabled={pending}
              onClick={() => decide('wrong_severity', lv)}
            >
              {t(`severity.${severityI18nKey(lv)}`)}
            </button>
          ))}
        </div>
      ) : null}

      {error ? <p className="text-[11px] text-sev-high">{error}</p> : null}
    </div>
  )
}
