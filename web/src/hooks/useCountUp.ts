import { useEffect, useRef, useState } from 'react'

function prefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  )
}

/**
 * 数字滚动（DEMO_PLAN §5.1 顶部状态条要求）。
 *
 * 三条刻意的约束：
 *
 * 1. **只在首次挂载时滚一次**。监测大屏的"告警事件"数会随视频播放从 0 涨到 9，
 *    如果每次变化都滚一遍，观众根本看不清当前到底是多少——那是在炫技，不是传达信息。
 *    首屏滚一次是"装配感"，后续更新一律直接显示。
 * 2. **初值给 0 而不是目标值**，否则首帧先闪一下最终值再跳回 0，比不动还难看。
 * 3. **`prefers-reduced-motion: reduce` 时直接给终值**，不播任何动画。
 */
export function useCountUp(target: number, durationMs = 700): number {
  const animated = useRef(false)
  const [value, setValue] = useState(() => (prefersReducedMotion() ? target : 0))

  useEffect(() => {
    if (animated.current) {
      setValue(target) // 后续变化：直接显示，不滚动
      return
    }
    animated.current = true

    if (prefersReducedMotion() || durationMs <= 0) {
      setValue(target)
      return
    }

    let raf = 0
    const t0 = performance.now()
    const tick = (t: number) => {
      const p = Math.min(1, (t - t0) / durationMs)
      const eased = 1 - Math.pow(1 - p, 3) // easeOutCubic：先快后慢，收尾干脆
      setValue(target * eased)
      if (p < 1) raf = requestAnimationFrame(tick)
      else setValue(target) // 收敛到精确值，避免浮点残留（如 5.9999）
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [target, durationMs])

  return value
}
