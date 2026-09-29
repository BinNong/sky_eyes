import { memo } from 'react'
import { useTranslation } from 'react-i18next'
import { theme } from '../theme/tokens'

export interface SourceItem {
  id: string
  name: string
  /** true = 本视频的真实推理结果；false = 复用同一片段的占位通道 */
  real: boolean
}

/** 只有 1 路是真实数据，其余为演示通道（复用同一段视频），界面上明确标注，不伪装 */
export const SOURCES: SourceItem[] = [
  { id: 'ch-01', name: '样本视频 · burstling_street', real: true },
  { id: 'ch-02', name: '演示通道 02', real: false },
  { id: 'ch-03', name: '演示通道 03', real: false },
  { id: 'ch-04', name: '演示通道 04', real: false },
]

/** memo 掉：父组件会因视频播放同步每帧重渲染，这个列表不需要跟着重跑 */
const SourceList = memo(function SourceList({
  selected,
  onSelect,
}: {
  selected: string
  onSelect: (id: string) => void
}) {
  const { t } = useTranslation()

  return (
    <div className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-hair bg-panel">
      <div className="flex shrink-0 items-center gap-2 border-b border-hair px-3.5 py-2.5">
        <span className="text-[13px] text-ink-dim">{t('dashboard.videoList')}</span>
        <span className="num ml-auto text-[11px] text-ink-faint">{SOURCES.length}</span>
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-2">
        {SOURCES.map((s) => {
          const active = s.id === selected
          return (
            <button
              key={s.id}
              type="button"
              onClick={() => onSelect(s.id)}
              className={[
                'mb-1.5 flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left transition-colors last:mb-0',
                active ? 'bg-accent-dim' : 'hover:bg-raised',
              ].join(' ')}
            >
              <span
                className="h-1.5 w-1.5 shrink-0 rounded-full"
                style={{ background: s.real ? theme.accent : theme.text.tertiary }}
              />
              <span
                className={[
                  'min-w-0 flex-1 truncate text-[12px]',
                  active ? 'text-accent' : 'text-ink-dim',
                ].join(' ')}
              >
                {s.name}
              </span>
              <span className="shrink-0 rounded border border-hair px-1 py-0.5 text-[10px] text-ink-faint">
                {s.real ? t('dashboard.sourceRealShort') : t('dashboard.sourceDemoShort')}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
})

export default SourceList
