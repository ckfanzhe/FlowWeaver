/**
 * UserMenu — toolbar top-right avatar + popover.
 *
 * Click the avatar to open. Popover sections:
 *   1. Identity header — email + user id (read-only).
 *   2. Avatar picker — 8 cartoon options. Click to pick; the new
 *      choice is persisted via `identityStore.setAvatar()`.
 *   3. Language picker — `en` / `zh-CN`. Picking one calls
 *      `identityStore.setLanguage()` which both updates the i18n
 *      locale AND persists the preference to the user record.
 *   4. Theme picker — light / dark / system. Stored on the user
 *      row via `identityStore.setTheme()` and applied to the
 *      `<html>` `dark` class via `lib/theme.ts`.
 *   5. Settings — opens the LLM presets / system settings drawer.
 *   6. Switch user — sign out and re-open the email gate.
 *
 * All section actions close the popover. Click outside / Esc also
 * close (handled inside `DropdownMenu`-like wiring — implemented
 * inline here because the body is too custom for the generic menu).
 */
import { useEffect, useRef, useState } from 'react'
import { useIdentityStore } from '../../store/identityStore'
import { useSettingsStore } from '../../store/settingsStore'
import { useT, LOCALES, type Locale } from '../../i18n'
import type { Theme } from '../../lib/theme'
import { AVATARS, getAvatar, pickAvatarForEmail } from './avatars'
import { SettingsIcon } from '../UI/Icons'

export function UserMenu() {
  const t = useT()
  const rootRef = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)

  const userId = useIdentityStore((s) => s.userId)
  const email = useIdentityStore((s) => s.email)
  const avatarId = useIdentityStore((s) => s.avatarId)
  const language = useIdentityStore((s) => s.language)
  const theme = useIdentityStore((s) => s.theme)
  const setAvatar = useIdentityStore((s) => s.setAvatar)
  const setLanguage = useIdentityStore((s) => s.setLanguage)
  const setTheme = useIdentityStore((s) => s.setTheme)
  const signOut = useIdentityStore((s) => s.signOut)
  const openSettings = useSettingsStore((s) => s.openSettings)

  // Click-outside / Esc to close.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  if (!userId) {
    // Not yet identified — App.tsx's EmailGateModal handles this, no
    // avatar shown in the toolbar.
    return null
  }

  // Display the stored pick if it exists, otherwise derive a
  // deterministic default from the email so the avatar doesn't
  // flicker between renders.
  const avatar =
    (avatarId && getAvatar(avatarId)) || pickAvatarForEmail(email)

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={email ?? userId}
        className={[
          'flex items-center justify-center rounded-full transition',
          avatar.bg,
          avatar.fg,
          'h-8 w-8 text-base ring-1 ring-edge hover:ring-accent focus:outline-none focus:ring-2 focus:ring-accent',
        ].join(' ')}
        data-testid="user-menu-avatar"
      >
        <span aria-hidden="true">{avatar.glyph}</span>
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 mt-2 w-72 rounded-lg border border-edge bg-surface shadow-xl z-30 py-2"
        >
          {/* Identity header */}
          <div className="px-3 pb-2 border-b border-edge">
            <div className="text-[10px] uppercase tracking-wider text-ink-faint">
              {t('identity.menu.signedInAs')}
            </div>
            <div className="mt-0.5 truncate text-sm text-ink" title={email ?? ''}>
              {email ?? userId}
            </div>
            {email && (
              <div className="mt-0.5 truncate text-[10px] text-ink-faint font-mono" title={userId}>
                {userId}
              </div>
            )}
          </div>

          {/* Avatar picker */}
          <div className="px-3 pt-3 pb-1">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-[10px] uppercase tracking-wider text-ink-faint">
                {t('identity.menu.avatar')}
              </span>
            </div>
            <div className="grid grid-cols-8 gap-1.5">
              {AVATARS.map((a) => {
                const isCurrent = a.id === avatar.id
                return (
                  <button
                    key={a.id}
                    type="button"
                    onClick={() => {
                      setAvatar(a.id)
                    }}
                    title={a.name}
                    aria-label={a.name}
                    className={[
                      'flex items-center justify-center rounded-full transition h-7 w-7 text-sm',
                      a.bg,
                      a.fg,
                      isCurrent
                        ? 'ring-2 ring-accent ring-offset-1 ring-offset-surface'
                        : 'hover:ring-1 hover:ring-edge',
                    ].join(' ')}
                  >
                    <span aria-hidden="true">{a.glyph}</span>
                  </button>
                )
              })}
            </div>
          </div>

          {/* Language picker */}
          <div className="px-3 pt-3 pb-1">
            <div className="text-[10px] uppercase tracking-wider text-ink-faint mb-1.5">
              {t('identity.menu.language')}
            </div>
            <div className="flex gap-1">
              {LOCALES.map((l) => {
                const isCurrent = language === l.value
                return (
                  <button
                    key={l.value}
                    type="button"
                    onClick={() => {
                      setLanguage(l.value as Locale)
                    }}
                    className={[
                      'flex-1 rounded-md border px-2 py-1 text-xs transition',
                      isCurrent
                        ? 'border-accent bg-accent-soft text-accent-text'
                        : 'border-edge bg-surface text-ink hover:bg-surface-2',
                    ].join(' ')}
                  >
                    {l.label}
                  </button>
                )
              })}
            </div>
          </div>

          {/* Theme picker */}
          <div className="px-3 pt-3 pb-1">
            <div className="text-[10px] uppercase tracking-wider text-ink-faint mb-1.5">
              {t('identity.menu.theme')}
            </div>
            <div className="grid grid-cols-3 gap-1">
              {(['light', 'dark', 'system'] as Theme[]).map((opt) => {
                const isCurrent = theme === opt
                return (
                  <button
                    key={opt}
                    type="button"
                    onClick={() => setTheme(opt)}
                    className={[
                      'rounded-md border px-2 py-1 text-xs transition',
                      isCurrent
                        ? 'border-accent bg-accent-soft text-accent-text'
                        : 'border-edge bg-surface text-ink hover:bg-surface-2',
                    ].join(' ')}
                  >
                    {t(`identity.menu.themes.${opt}`)}
                  </button>
                )
              })}
            </div>
          </div>

          {/* Settings */}
          <div className="mt-2 pt-2 border-t border-edge px-1">
            <button
              type="button"
              onClick={() => {
                setOpen(false)
                openSettings()
              }}
              className="flex w-full items-center gap-2 rounded px-3 py-1.5 text-sm text-ink hover:bg-surface-2"
            >
              <SettingsIcon />
              <span className="flex-1 text-left">{t('identity.menu.settings')}</span>
            </button>
          </div>

          {/* Switch user */}
          <div className="pt-1 px-1">
            <button
              type="button"
              onClick={() => {
                setOpen(false)
                signOut()
              }}
              className="flex w-full items-center gap-2 rounded px-3 py-1.5 text-sm text-ink hover:bg-surface-2"
            >
              <span aria-hidden="true">↩</span>
              <span className="flex-1 text-left">{t('identity.menu.switchUser')}</span>
            </button>
          </div>
        </div>
      )}
    </div>
  )
}