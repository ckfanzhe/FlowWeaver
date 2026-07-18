/**
 * Theme applier — translates the identityStore theme preference
 * into a `dark` class on <html> and listens to OS-level scheme
 * changes when the choice is 'system'.
 *
 * Mount this once at the app root (App.tsx).
 *
 * `applyTheme` (in `./theme.ts`) handles the DOM side of the same
 * switch; the hook only owns the React-managed
 * `matchMedia('(prefers-color-scheme: dark)')` subscription.
 */
import { useEffect } from 'react'
import { useIdentityStore } from '../store/identityStore'
import { applyTheme, type Theme } from './theme'

export function useThemeApplier(): void {
  const theme = useIdentityStore((s) => s.theme)

  useEffect(() => {
    // Until the user's first `/users/me` settles, `theme` is null —
    // apply the OS default and let `setTheme` overwrite once the
    // user signs in.
    const effective: Theme = (theme ?? 'system') as Theme
    if (effective === 'system') {
      const mq = window.matchMedia('(prefers-color-scheme: dark)')
      const handle = () => applyTheme('system')
      handle()
      mq.addEventListener('change', handle)
      return () => mq.removeEventListener('change', handle)
    }
    applyTheme(effective)
  }, [theme])
}