import { useTranslation } from 'react-i18next'
import { STATUS_STYLE } from '../theme/tokens'
import type { IncidentStatus } from '../store/incidentStore'

/** 处置状态徽章。刻意不用严重等级的三色，理由见 theme/tokens.ts 的 STATUS_STYLE 注释。 */
export default function StatusBadge({
  status,
  size = 'md',
}: {
  status: IncidentStatus
  size?: 'sm' | 'md'
}) {
  const { t } = useTranslation()
  const s = STATUS_STYLE[status] ?? STATUS_STYLE.pending
  const cls =
    size === 'sm'
      ? 'gap-1 px-1.5 py-0.5 text-[11px]'
      : 'gap-1.5 px-2 py-0.5 text-[12px]'

  return (
    <span
      className={`inline-flex items-center rounded-md border font-medium ${cls}`}
      style={{ color: s.fg, background: s.bg, borderColor: s.border }}
    >
      <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: s.dot }} />
      {t(`status.${status}`)}
    </span>
  )
}
