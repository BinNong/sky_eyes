/** 秒 -> mm:ss.s。38 秒的短视频用 mm:ss.s 比 mm:ss 直观。 */
export function formatTime(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return '00:00.0'
  const m = Math.floor(sec / 60)
  const s = sec - m * 60
  return `${String(m).padStart(2, '0')}:${s.toFixed(1).padStart(4, '0')}`
}

/** 百分比字符串，用于把 bbox 从原视频坐标映射到容器内的绝对定位 */
export function pct(value: number, total: number): string {
  if (!total) return '0%'
  return `${(value / total) * 100}%`
}

export function clamp(v: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, v))
}

/** 取描述的首句，用于列表摘要 */
export function firstSentence(text: string | null | undefined): string {
  if (!text) return ''
  return text.split(/[。\n]/)[0] ?? ''
}
