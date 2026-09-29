import { memo } from 'react'
import { useCountUp } from '../hooks/useCountUp'

/** 大屏顶部关键数字卡片。数值用等宽字体，避免刷新时抖动。
 *
 *  memo 掉：监测大屏播放时父组件会以 25fps 重渲染，这四张卡片的内容基本不变，
 *  没必要跟着每帧重跑。
 *
 *  传数字会自动播一次"数字滚动"（DEMO_PLAN §5.1）；传字符串则原样显示。
 */
const StatCard = memo(function StatCard({
  label,
  value,
  unit,
  accent,
  hint,
  decimals = 0,
}: {
  label: string
  value: string | number
  unit?: string
  accent?: string
  hint?: string
  /** 数值的小数位，用于 6.1% 这类指标 */
  decimals?: number
}) {
  const isNumeric = typeof value === 'number'
  const counted = useCountUp(isNumeric ? value : 0)
  const shown = isNumeric ? counted.toFixed(decimals) : value

  return (
    <div className="rounded-lg border border-hair bg-panel px-4 py-3">
      <div className="text-[12px] text-ink-dim">{label}</div>
      <div className="mt-1.5 flex items-baseline gap-1">
        <span
          className="num text-[26px] leading-none"
          style={accent ? { color: accent } : { color: 'var(--color-ink)' }}
        >
          {shown}
        </span>
        {unit ? <span className="text-[12px] text-ink-faint">{unit}</span> : null}
      </div>
      {hint ? <div className="mt-1.5 text-[11px] text-ink-faint">{hint}</div> : null}
    </div>
  )
})

export default StatCard
