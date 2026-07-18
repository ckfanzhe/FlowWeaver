/**
 * Minimal i18n — hand-rolled, no third-party deps.
 *
 * Public API:
 *   t(key, vars?)                 → translate a dotted key (e.g. "panel.noConfig")
 *   useT()                        → React hook returning a reactive `t`
 *   setLocale(locale)             → switch locale, persist to localStorage
 *   getLocale()                   → current locale
 *   LOCALES                       → list of supported locales, for UI dropdowns
 *
 * Adding a new language:
 *   1. Drop a JSON file in locales/ (mirror en.json's keys)
 *   2. Add it to the `dictionaries` map below
 *   3. Add an entry to LOCALES
 *
 * Missing translations fall back to English, then to the key itself
 * (so the UI is never blank — the key is at least visible during dev).
 */
import { useSyncExternalStore } from 'react'
import en from './locales/en.json'
import zhCN from './locales/zh-CN.json'

export type Locale = 'en' | 'zh-CN'

export interface LocaleMeta {
  value: Locale
  label: string
}

export const LOCALES: ReadonlyArray<LocaleMeta> = [
  { value: 'en', label: 'English' },
  { value: 'zh-CN', label: '中文' },
]

export const DEFAULT_LOCALE: Locale = 'en'

const dictionaries: Record<Locale, unknown> = {
  en,
  'zh-CN': zhCN,
}

const STORAGE_KEY = 'agnobuilder.locale'

let currentLocale: Locale = DEFAULT_LOCALE
const listeners = new Set<() => void>()

// Hydrate from localStorage at module load (runs before any React mount).
try {
  const stored = localStorage.getItem(STORAGE_KEY)
  if (stored && stored in dictionaries) currentLocale = stored as Locale
} catch {
  /* localStorage may be unavailable (SSR / private mode) */
}

export function getLocale(): Locale {
  return currentLocale
}

export function setLocale(locale: Locale): void {
  if (!(locale in dictionaries)) return
  if (locale === currentLocale) return
  currentLocale = locale
  try {
    localStorage.setItem(STORAGE_KEY, locale)
  } catch {
    /* ignore */
  }
  document.documentElement.lang = locale
  listeners.forEach((fn) => fn())
}

function subscribeLocale(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/** Lookup a dotted key in a nested dict. */
function lookup(dict: unknown, key: string): unknown {
  return key.split('.').reduce<unknown>((acc, k) => {
    if (acc && typeof acc === 'object') {
      return (acc as Record<string, unknown>)[k]
    }
    return undefined
  }, dict)
}

/** Replace {var} placeholders. Missing vars stay as `{var}` so they're visible. */
function interpolate(template: string, vars?: Record<string, string | number>): string {
  if (!vars) return template
  return template.replace(/\{(\w+)\}/g, (_, k) => String(vars[k] ?? `{${k}}`))
}

/** Translate a key. Falls back to English, then to the key itself. */
export function t(key: string, vars?: Record<string, string | number>): string {
  let value = lookup(dictionaries[currentLocale], key)
  if (typeof value !== 'string' && currentLocale !== 'en') {
    value = lookup(dictionaries.en, key)
  }
  if (typeof value !== 'string') return key
  return interpolate(value, vars)
}

/** React hook: returns a reactive `t` that re-renders on locale change. */
export function useT() {
  useSyncExternalStore(subscribeLocale, getLocale, getLocale)
  return t
}

/** React hook: returns the current locale and re-renders on change.
 *  Used by `App.tsx` to mirror the active locale onto
 *  `document.documentElement.lang` and by any other consumer that
 *  needs the raw locale (rather than the translation helper). */
export function useLocale(): Locale {
  return useSyncExternalStore(subscribeLocale, getLocale, getLocale)
}
