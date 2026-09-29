import { useEffect } from 'react'
import { Link, NavLink, Outlet } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import ErrorBoundary from './ErrorBoundary'
import { setLang, type Lang } from '../i18n'
import { useDemoStore } from '../store/demoStore'
import { theme } from '../theme/tokens'

const NAV = [
  { to: '/', key: 'dashboard', end: true },
  { to: '/events', key: 'events', end: false },
  { to: '/evidence', key: 'evidence', end: false },
  { to: '/analytics', key: 'analytics', end: false },
  { to: '/labeling', key: 'labeling', end: false },
  { to: '/system', key: 'system', end: false },
] as const

/**
 * 右上角的推理服务指示灯（DEMO_PLAN §4 的设计要求）。
 *
 * 三态而不是两态：后端通了但多模态模型没起来，是**最容易被误判**的一种情况——
 * 只显示"离线"会让人以为整个服务都没开，只显示"在线"又会在点「开始实时分析」时打脸。
 * 分开报，现场一眼就能看出该去启哪个。
 */
function StatusLight() {
  const { t } = useTranslation()
  const health = useDemoStore((s) => s.health)
  const probing = useDemoStore((s) => s.probing)

  const state = probing
    ? { color: theme.text.tertiary, label: t('system.probing') }
    : health === null
      ? { color: theme.text.tertiary, label: t('system.statusOffline') }
      : health.janus.ok
        ? { color: theme.accent, label: t('system.statusOnline') }
        : { color: theme.severity.中, label: t('system.statusModelDown') }

  return (
    <Link
      to="/system"
      title={state.label}
      className="flex items-center gap-2 rounded-md border border-hair px-2.5 py-1 text-[11px] text-ink-dim transition-colors hover:bg-raised hover:text-ink"
    >
      <span
        className="h-2 w-2 shrink-0 rounded-full"
        style={{ background: state.color, boxShadow: `0 0 6px ${state.color}` }}
      />
      <span className="max-w-[168px] truncate">{state.label}</span>
    </Link>
  )
}

export default function Layout() {
  const { t, i18n } = useTranslation()
  const lang: Lang = i18n.language === 'en-US' ? 'en-US' : 'zh-CN'
  const probe = useDemoStore((s) => s.probe)
  const restoreLast = useDemoStore((s) => s.restoreLast)

  // 应用一进来就探一次，这样指示灯在任何页面都能反映真实状态，
  // 而不用先跑一趟系统页。失败也没关系——探活本身不抛错。
  // 顺带恢复上次载入的实时产物：刷新一下就被踢回静态数据，代价太大。
  useEffect(() => {
    void probe()
    void restoreLast()
  }, [probe, restoreLast])

  return (
    <div className="relative flex h-full flex-col">
      {/* 氛围层：绝对定位在内容之下，纯装饰，不接收鼠标事件 */}
      <div aria-hidden className="atmosphere absolute inset-0 overflow-hidden" />

      {/* 页头间距对窄屏收窄。
          ⚠ 这不是"顺手美化"：导航加到第 6 项（证据检索）之后，1024px 下这一行
          会挤到折行——导航从 32px 变成 51px、标题从 25px 变成 45px，虽然刚好没被
          56px 的页头裁掉（只剩 3px 余量），但看起来已经散了。
          用 `xl:` 前缀收口，保证演示用的 1600/1920 下**观感一字不变**。 */}
      <header className="relative z-10 flex h-14 shrink-0 items-center gap-3 border-b border-hair px-5 xl:gap-7">
        <div className="flex items-baseline gap-2.5">
          <span className="text-[15px] font-medium text-ink">{t('app.title')}</span>
          <span className="rounded border border-hair px-1.5 py-0.5 text-[11px] text-ink-faint">
            {t('app.demoBadge')}
          </span>
        </div>

        <nav className="flex items-center gap-1">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                [
                  // whitespace-nowrap：不加的话窄屏下会从词中间断开（「监测总 / 览」）
                  'whitespace-nowrap rounded-md px-2 py-1.5 text-[13px] transition-colors xl:px-3',
                  isActive
                    ? 'bg-accent-dim text-accent'
                    : 'text-ink-dim hover:bg-raised hover:text-ink',
                ].join(' ')
              }
            >
              {t(`nav.${item.key}`)}
            </NavLink>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-3">
          <StatusLight />
          <div className="flex items-center overflow-hidden rounded-md border border-hair">
            {(['zh-CN', 'en-US'] as const).map((code) => (
              <button
                key={code}
                type="button"
                onClick={() => setLang(code)}
                className={[
                  'px-2.5 py-1 text-[12px] transition-colors',
                  lang === code ? 'bg-accent-dim text-accent' : 'text-ink-dim hover:text-ink',
                ].join(' ')}
              >
                {code === 'zh-CN' ? '中' : 'EN'}
              </button>
            ))}
          </div>
        </div>
      </header>

      <main className="relative z-10 min-h-0 flex-1 overflow-auto">
        <ErrorBoundary>
          <Outlet />
        </ErrorBoundary>
      </main>
    </div>
  )
}
