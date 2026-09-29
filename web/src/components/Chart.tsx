import { useEffect, useRef } from 'react'
import echarts, { type ECharts, type EChartsOption } from './charts/setup'

/**
 * ECharts 的极简 React 封装。
 *
 * 刻意不用 `echarts-for-react`：它依赖较旧、对 React 18 的类型也不够准，
 * 而这里需要的能力（init / setOption / resize / dispose）自己写不到 30 行。
 *
 * 两个关键点：
 * 1. **ResizeObserver 而不是 window.resize** —— 看板是网格布局，容器宽度变化
 *    未必伴随窗口尺寸变化（比如侧栏折叠），只听 window 会 resize 失效。
 * 2. `setOption(option, true)` 的 `true` 是 notMerge，避免切换数据源时
 *    旧 series 残留（ECharts 默认是合并模式，会留下上一条数据的柱子）。
 */
export default function Chart({
  option,
  height = 220,
  className,
}: {
  option: EChartsOption
  height?: number | string
  className?: string
}) {
  const boxRef = useRef<HTMLDivElement | null>(null)
  const instRef = useRef<ECharts | null>(null)

  useEffect(() => {
    const box = boxRef.current
    if (!box) return

    const inst = echarts.init(box, undefined, { renderer: 'canvas' })
    instRef.current = inst

    // 首帧容器可能宽度为 0（父级尚未布局完成），resize 一次兜底
    const raf = requestAnimationFrame(() => inst.resize())

    const ro = new ResizeObserver(() => inst.resize())
    ro.observe(box)

    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
      inst.dispose()
      instRef.current = null
    }
  }, [])

  useEffect(() => {
    instRef.current?.setOption(option, true)
  }, [option])

  return <div ref={boxRef} className={className} style={{ height, width: '100%' }} />
}
