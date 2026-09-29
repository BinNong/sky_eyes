import { Suspense, lazy } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Events from './pages/Events'
import EventDetail from './pages/EventDetail'
import Evidence from './pages/Evidence'
import Labeling from './pages/Labeling'
import System from './pages/System'

/**
 * 数据看板单独做懒加载。
 *
 * ECharts 一个人就把包体从 ~85KB 顶到 ~256KB gzip。监测大屏才是这个 demo 的门面，
 * 首屏不该为"另一个页面的图表"买单——按需加载后主包回到原来的量级，
 * 点进看板时再拉图表那块（本地几十毫秒，路演无感）。
 */
const Analytics = lazy(() => import('./pages/Analytics'))

function PageLoading() {
  const { t } = useTranslation()
  return <div className="p-5 text-[12px] text-ink-faint">{t('common.loading')}</div>
}

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="events" element={<Events />} />
        <Route path="events/:id" element={<EventDetail />} />
        <Route path="evidence" element={<Evidence />} />
        <Route
          path="analytics"
          element={
            <Suspense fallback={<PageLoading />}>
              <Analytics />
            </Suspense>
          }
        />
        <Route path="system" element={<System />} />
        <Route path="labeling" element={<Labeling />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
