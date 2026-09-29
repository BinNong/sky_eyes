import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'
import i18n from '../i18n'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
  componentStack: string
}

/**
 * 渲染错误兜底。
 *
 * 没有它的话，任何一个组件在渲染时抛异常（字段缺失、数组越界等），
 * React 18 会卸载**整棵树**——用户看到的是纯白页面，除控制台外没有任何提示。
 * 这是个要现场演示的东西，白屏是最难当场定位的故障。
 *
 * 之所以放在 Layout 的 <Outlet /> 外层、而不是包住整个应用：
 * 这样出错时顶部导航还在，演示者可以直接切到别的页面把流程讲完，不至于当场卡死。
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, componentStack: '' }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    this.setState({ componentStack: info.componentStack ?? '' })
    console.error('[sky_eyes] 渲染异常：', error, info)
  }

  private retry = () => this.setState({ error: null, componentStack: '' })

  render() {
    const { error, componentStack } = this.state
    if (!error) return this.props.children

    return (
      <div className="p-8">
        <div className="max-w-3xl rounded-lg border border-hair bg-panel p-6">
          <div className="text-[15px] font-medium text-sev-high">{i18n.t('error.title')}</div>
          <p className="mt-2 text-[12px] leading-relaxed text-ink-dim">{i18n.t('error.hint')}</p>

          <div className="mt-4 text-[11px] text-ink-faint">{i18n.t('error.detail')}</div>
          <pre className="mt-1.5 max-h-64 overflow-auto whitespace-pre-wrap rounded border border-hair bg-base p-3 text-[11px] leading-relaxed text-ink-dim">
            {String(error.stack || error.message || error)}
            {componentStack ? `\n\n组件栈：${componentStack}` : ''}
          </pre>

          <div className="mt-4 flex gap-2">
            <button
              type="button"
              onClick={this.retry}
              className="rounded-md border border-accent/50 bg-accent-dim px-3 py-1.5 text-[12px] text-accent transition-colors hover:bg-accent/20"
            >
              {i18n.t('error.retry')}
            </button>
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="rounded-md border border-line px-3 py-1.5 text-[12px] text-ink transition-colors hover:bg-raised"
            >
              {i18n.t('error.reload')}
            </button>
          </div>
        </div>
      </div>
    )
  }
}
