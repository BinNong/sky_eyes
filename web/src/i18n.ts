import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import zhCN from './locales/zh-CN.json'
import enUS from './locales/en-US.json'

const STORAGE_KEY = 'sky-eyes-lang'

export const SUPPORTED_LANGS = ['zh-CN', 'en-US'] as const
export type Lang = (typeof SUPPORTED_LANGS)[number]

/** 默认中文——主要受众是国内交管客户 */
const saved = localStorage.getItem(STORAGE_KEY)
const initial: Lang = saved === 'en-US' ? 'en-US' : 'zh-CN'

void i18n
  .use(initReactI18next)
  .init({
    resources: {
      'zh-CN': { translation: zhCN },
      'en-US': { translation: enUS },
    },
    lng: initial,
    fallbackLng: 'zh-CN',
    interpolation: { escapeValue: false },
  })
  // init 是异步的，必须等它 resolve 后再取译文，否则 t() 拿不到东西
  .then(applyLangToDocument)

export function setLang(lang: Lang) {
  localStorage.setItem(STORAGE_KEY, lang)
  void i18n.changeLanguage(lang)
}

/** 语言变了，`<html lang>` 和浏览器标签页标题都要跟着变。
 *  只改 lang 属性的话，切到英文后标签页标题仍是中文——截图和录屏里一眼能看出来。 */
function applyLangToDocument() {
  const lang = i18n.language === 'en-US' ? 'en-US' : 'zh-CN'
  document.documentElement.lang = lang
  const title = i18n.t('app.title')
  if (title) document.title = title
}

i18n.on('languageChanged', applyLangToDocument)

export default i18n
