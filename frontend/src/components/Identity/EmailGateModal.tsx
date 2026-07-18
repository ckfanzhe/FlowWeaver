/**
 * Email-gate modal — required to enter the app.
 *
 * Per the contract: the platform has no login, but every
 * workflow row is owned by a `users.id` (= email). The user MUST
 * declare their email before any workflow CRUD fires. After first
 * identification the email is persisted to localStorage so the
 * gate doesn't re-appear on subsequent visits.
 *
 * When is the modal visible?
 *   - First visit (no `agnobuilder.userId` in localStorage).
 *   - Recovery from a stale id (`/users/me` returned 404 — e.g.
 *     backend was reset) — the previous email pre-fills the
 *     input but the user has to re-submit to re-bind.
 *   - "Switch user" from the toolbar — `signOut()` clears
 *     localStorage and re-mounts the modal.
 *
 * The modal CANNOT be closed (no Esc, no overlay click, no
 * Cancel button) until the user submits a valid email — that
 * is the whole point of the gate.
 */
import { useEffect, useState } from 'react'
import { useT } from '../../i18n'
import { useIdentityStore } from '../../store/identityStore'

export function EmailGateModal() {
  const t = useT()
  const userId = useIdentityStore((s) => s.userId)
  const email = useIdentityStore((s) => s.email)
  const error = useIdentityStore((s) => s.error)
  const identify = useIdentityStore((s) => s.identify)
  const clearError = useIdentityStore((s) => s.clearError)

  // Gate is open whenever there's no resolved identity.
  const open = !userId
  // Pre-fill with whatever the user had before (recovery) or
  // whatever the server told us about (rebind path).
  const [value, setValue] = useState(email ?? '')
  const [busy, setBusy] = useState(false)

  // Reset the input whenever the modal transitions from closed→open
  // (e.g. after signOut). Without this the form keeps the last
  // submission's text and the user sees a stale value.
  useEffect(() => {
    if (open) setValue(email ?? '')
  }, [open, email])

  if (!open) return null

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    const trimmed = value.trim()
    if (!trimmed) return
    setBusy(true)
    try {
      await identify(trimmed)
      // success → store flips `userId` → `open` becomes false → modal unmounts
    } catch {
      // error message is already on the store; the modal re-renders
      // with the server's detail
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      // No onClick on the overlay — gate cannot be dismissed by
      // clicking outside.
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      data-testid="email-gate-modal"
    >
      <div
        className="w-[min(92vw,440px)] rounded-lg bg-surface text-ink shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <form onSubmit={handleSubmit}>
          <header className="border-b border-edge px-5 py-4">
            <h2 className="text-base font-semibold">{t('identity.gate.title')}</h2>
            <p className="mt-1 text-xs text-ink-muted">{t('identity.gate.subtitle')}</p>
          </header>

          <div className="px-5 py-4 space-y-3">
            <label className="block">
              <span className="text-xs uppercase tracking-wider text-ink-faint">
                {t('identity.gate.label')}
              </span>
              <input
                type="email"
                autoFocus
                autoComplete="email"
                value={value}
                onChange={(e) => {
                  setValue(e.target.value)
                  if (error) clearError()
                }}
                disabled={busy}
                placeholder={t('identity.gate.placeholder')}
                className="mt-1 w-full rounded border border-edge bg-bg px-3 py-2 text-sm focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent disabled:opacity-50"
                data-testid="email-gate-input"
              />
            </label>

            {error && (
              <div
                role="alert"
                className="rounded border border-danger bg-danger-bg px-3 py-2 text-xs text-danger"
              >
                {error}
              </div>
            )}

            <p className="text-[11px] text-ink-faint">
              {t('identity.gate.hint')}
            </p>
          </div>

          <footer className="flex items-center justify-end gap-2 border-t border-edge px-5 py-3">
            <button
              type="submit"
              className="btn-primary"
              disabled={busy || !value.trim()}
              data-testid="email-gate-submit"
            >
              {busy ? t('common.loading') : t('identity.gate.submit')}
            </button>
          </footer>
        </form>
      </div>
    </div>
  )
}