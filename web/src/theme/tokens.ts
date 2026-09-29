/**
 * 深色科技风配色 token —— 完整规范见 DEMO_PLAN.md §6.1。
 *
 * 规矩：**组件里不要硬编码色值**，一律从这里取。
 * 严重等级三色是全局唯一语义色，其他地方不得借用。
 */

export const theme = {
  bg: {
    base: '#070B14',
    panel: '#0F1626',
    raised: '#16203A',
  },
  border: {
    hair: 'rgba(255, 255, 255, 0.08)',
    line: 'rgba(255, 255, 255, 0.14)',
    strong: 'rgba(255, 255, 255, 0.24)',
  },
  text: {
    primary: '#E8EEF9',
    secondary: '#93A2BF',
    tertiary: '#5B6B8C',
  },
  accent: '#22D3EE',
  severity: {
    高: '#FF4D4F',
    中: '#FFA940',
    低: '#40A9FF',
  },
  /** ECharts 系列色板 */
  series: ['#22D3EE', '#3B82F6', '#8B5CF6', '#14B8A6', '#F59E0B'],
} as const

export type SeverityKey = keyof typeof theme.severity

/**
 * 严重等级排序表。
 *
 * ⚠ 绝不能用 JS 的默认 sort 比中文字符串——按 Unicode 码点是
 * 中(U+4E2D) < 低(U+4F4E) < 高(U+9AD8)，`['低','中'].sort()` 会得到 `['中','低']`。
 * 后端 pipeline.py 第一版就栽在同一个坑上（见 DEMO_PLAN.md 附录 B）。
 */
export const SEVERITY_ORDER: Record<string, number> = { 低: 0, 中: 1, 高: 2 }

export function severityRank(s: string | null | undefined): number {
  if (!s) return -1
  return SEVERITY_ORDER[s] ?? -1
}

export function severityColor(s: string | null | undefined): string {
  if (!s) return theme.text.tertiary
  return theme.severity[s as SeverityKey] ?? theme.text.tertiary
}

/** 从高到低遍历用的等级列表 */
export const SEVERITY_DESC = ['高', '中', '低'] as const

/** 数据里的中文等级 -> i18n 词条键（severity.high / medium / low） */
export const SEVERITY_I18N_KEY: Record<string, string> = {
  高: 'high',
  中: 'medium',
  低: 'low',
}

export function severityI18nKey(s: string | null | undefined): string {
  if (!s) return 'unknown'
  return SEVERITY_I18N_KEY[s] ?? 'unknown'
}

/**
 * 处置状态的样式。
 *
 * 刻意**不复用严重等级的三色**——等级色是全局唯一语义色（§6.1 硬规矩），
 * 借用会让"红=高等级"和"红=待处置"混淆。这里用中性灰 → 强调青 → 亮白的递进表达。
 *
 * 写死成具体色值而不是 Tailwind 类名，避免 JIT 扫描不到动态拼接的类。
 */
export const STATUS_STYLE: Record<
  string,
  { fg: string; bg: string; border: string; dot: string }
> = {
  pending: {
    fg: '#93A2BF',
    bg: 'transparent',
    border: 'rgba(255, 255, 255, 0.14)',
    dot: '#5B6B8C',
  },
  dispatched: {
    fg: '#22D3EE',
    bg: 'rgba(34, 211, 238, 0.14)',
    border: 'rgba(34, 211, 238, 0.5)',
    dot: '#22D3EE',
  },
  resolved: {
    fg: '#E8EEF9',
    bg: '#16203A',
    border: 'rgba(255, 255, 255, 0.24)',
    dot: '#E8EEF9',
  },
}

/**
 * 推理任务状态的样式。
 *
 * 与处置状态同样的原则：**不借用严重等级的三色**（§6.1 硬规矩），
 * 用中性灰 → 强调青的递进来表达"有没有结果、能不能用"。
 *
 * 唯一例外是 `error` 用了红——它和界面里「失败帧数」是同一类语义
 * （"这次运行出错了"），而且失败必须一眼看见，不能压成灰色。
 * 「已中断」刻意用**虚线**边框：它是非正常结束，但产物/进程都已不在，
 * 需要的是"可删除"而不是"要救火"。
 */
export const RUN_STATUS_STYLE: Record<
  string,
  { fg: string; bg: string; border: string; dot: string; dashed?: boolean }
> = {
  queued: {
    fg: '#93A2BF',
    bg: 'transparent',
    border: 'rgba(255, 255, 255, 0.14)',
    dot: '#5B6B8C',
  },
  running: {
    fg: '#22D3EE',
    bg: 'rgba(34, 211, 238, 0.14)',
    border: 'rgba(34, 211, 238, 0.5)',
    dot: '#22D3EE',
  },
  done: {
    fg: '#22D3EE',
    bg: 'transparent',
    border: 'rgba(34, 211, 238, 0.35)',
    dot: '#22D3EE',
  },
  error: {
    fg: '#FF4D4F',
    bg: 'transparent',
    border: 'rgba(255, 77, 79, 0.45)',
    dot: '#FF4D4F',
  },
  cancelled: {
    fg: '#5B6B8C',
    bg: 'transparent',
    border: 'rgba(255, 255, 255, 0.14)',
    dot: '#5B6B8C',
  },
  interrupted: {
    fg: '#93A2BF',
    bg: 'transparent',
    border: 'rgba(255, 255, 255, 0.24)',
    dot: '#5B6B8C',
    dashed: true,
  },
}

/** 简报是"纸"，浅色；主应用是"屏"，深色。两套色不能混用。 */
export const paper = {
  bg: '#FFFFFF',
  ink: '#1A2233',
  sub: '#5A6782',
  faint: '#8A93A8',
  line: '#DCE1EA',
  headBg: '#F4F6FA',
} as const

/**
 * 人工复核结论的样式。
 *
 * 与处置/任务状态同样的原则：**不借用严重等级三色**（§6.1 硬规矩）。
 * 这里只有一处例外，理由和 `RUN_STATUS_STYLE.error` 相同：
 * `false_positive` 表达的是「**系统出错了**」，和界面里「失败帧数」是同一类语义，
 * 用红是一致的；而它和「这起事故是高等级」那种红分属两个语义层。
 * 为避免视觉混淆，它只染文字与边框、**不填充背景**——而等级徽章是填充的。
 *
 * `wrong_severity` 刻意不用橙：橙已经被「中等级」占用，借用会让
 * 「等级判成中」和「真实等级是中」看起来像同一个意思。改用亮白，靠文字说明真相。
 */
export const VERDICT_STYLE: Record<
  string,
  { fg: string; bg: string; border: string; bar: string }
> = {
  correct: {
    fg: '#22D3EE',
    bg: 'rgba(34, 211, 238, 0.12)',
    border: 'rgba(34, 211, 238, 0.5)',
    bar: '#22D3EE',
  },
  wrong_severity: {
    fg: '#E8EEF9',
    bg: '#16203A',
    border: 'rgba(255, 255, 255, 0.24)',
    bar: '#E8EEF9',
  },
  false_positive: {
    fg: '#FF4D4F',
    bg: 'transparent',
    border: 'rgba(255, 77, 79, 0.45)',
    bar: '#FF4D4F',
  },
}



