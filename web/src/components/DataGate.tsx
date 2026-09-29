import type { ReactNode } from 'react'
import { useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { useDemoStore } from '../store/demoStore'

/** 页面数据门：统一处理加载中 / 加载失败，避免每个页面各写一遍。 */
export default function DataGate({ children }: { children: ReactNode }) {
  const { t } = useTranslation()
  const { data, loading, error, load } = useDemoStore()

  useEffect(() => {
    void load()
  }, [load])

  if (error) {
    return (
      <div className="p-8">
        <div className="max-w-2xl rounded-lg border border-hair bg-panel p-6">
          <div className="text-[14px] text-sev-high">{t('common.loadFailed')}</div>
          <pre className="mt-3 overflow-auto whitespace-pre-wrap text-[12px] text-ink-dim">
            {error}
          </pre>
          <div className="mt-4 text-[12px] text-ink-faint">
            请先运行：<code className="num">python scripts/export_demo_data.py</code>
          </div>
        </div>
      </div>
    )
  }

  if (loading || !data) {
    return <div className="p-8 text-[13px] text-ink-dim">{t('common.loading')}…</div>
  }

  return <>{children}</>
}
