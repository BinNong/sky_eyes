import { useTranslation } from 'react-i18next'
import { severityColor, severityI18nKey } from '../theme/tokens'

/** 严重等级徽章。等级色是全局唯一语义色，样式统一在这里，别在页面里另写一套。 */
export default function SeverityBadge({
  severity,
  size = 'md',
}: {
  severity: string | null | undefined
  size?: 'sm' | 'md' | 'lg'
}) {
  const { t } = useTranslation()
  const color = severityColor(severity)
  const label = t(`severity.${severityI18nKey(severity)}`)

  const cls =
    size === 'lg'
      ? 'px-3 py-1 text-[15px]'
      : size === 'sm'
        ? 'px-1.5 py-0.5 text-[11px]'
        : 'px-2 py-0.5 text-[12px]'

  return (
    <span
      className={`inline-flex items-center rounded-md border font-medium ${cls}`}
      style={{ color, borderColor: `${color}66`, backgroundColor: `${color}1A` }}
    >
      {label}
    </span>
  )
}
