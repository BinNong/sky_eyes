/**
 * ECharts 按需注册。
 *
 * 只用 `echarts/core` + 显式 `use()`，不用 `import * as echarts from 'echarts'`——
 * 后者会把全部图表类型和组件打进包里（约 1MB），而我们只要三种图。
 * 加新图之前先在这里补注册，否则运行时会静默不渲染（ECharts 不报错，只是空白）。
 */
import * as echarts from 'echarts/core'
import { BarChart, PieChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([BarChart, PieChart, GridComponent, TooltipComponent, CanvasRenderer])

export default echarts
export type { ECharts } from 'echarts/core'
export type { EChartsOption } from 'echarts'

/** 统一的深色 tooltip 外观，三张图共用，避免各写一套。 */
export const TOOLTIP_STYLE = {
  backgroundColor: 'rgba(22, 32, 58, 0.96)',
  borderColor: 'rgba(255, 255, 255, 0.14)',
  borderWidth: 1,
  padding: [8, 12] as [number, number],
  textStyle: { color: '#E8EEF9', fontSize: 12, lineHeight: 18 },
  extraCssText: 'border-radius:8px;box-shadow:0 8px 28px rgba(0,0,0,.45);',
}

/** 数字用等宽，符合 §6.1 的仪表盘规范。
 *  与 index.css 的 --font-mono 保持一致：只用平台自带栈，不引网络字体（离线演示要求）。 */
export const MONO_FONT =
  'ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace'

export const AXIS_LABEL = { color: '#5B6B8C', fontSize: 11, fontFamily: MONO_FONT }
export const AXIS_LINE = { lineStyle: { color: 'rgba(255, 255, 255, 0.14)' } }
export const SPLIT_LINE = { lineStyle: { color: 'rgba(255, 255, 255, 0.06)', type: 'dashed' as const } }

/**
 * tooltip / label 的 formatter 适配器。
 *
 * ECharts 的 formatter 参数类型是 `T | T[]` 的联合，直接写 `(p) => p.name` 会因为
 * 联合类型上不存在 `.name` 而报错；而 `any` 又不该出现在这个代码库里（见 REVIEW.md）。
 * 这里把调用方声明的具体形状断言成 `(params: unknown) => string` —— `unknown` 是任何
 * 参数类型的父类型，因此可以安全赋给 ECharts 声明的那几种 formatter 字段。
 *
 * 全项目**唯一**需要写 `as unknown as` 的地方，就集中在这里。
 */
export function asFormatter<P>(fn: (params: P) => string): (params: unknown) => string {
  return fn as unknown as (params: unknown) => string
}

