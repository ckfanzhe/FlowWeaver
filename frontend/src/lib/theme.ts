/**
 * Theme applier — translates the per-user theme preference into a
 * `dark` class on <html> and listens to OS-level scheme changes
 * when the choice is 'system'.
 *
 * Two callers:
 *   * `useThemeApplier` (mounted once at the app root) — initialises
 *     the listener and applies the current preference from the
 *     identity store on mount.
 *   * `identityStore.setTheme()` — applies synchronously on every
 *     user pick so the DOM flips immediately, without waiting for
 *     the React re-render to flow through the hook.
 *
 * The hook still owns the `'system'` MediaQueryList subscription
 * because that's a React effect, not a one-shot DOM write.
 */
export type Theme = 'light' | 'dark' | 'system'

export function applyTheme(theme: Theme): void {
  const resolved: 'light' | 'dark' =
    theme === 'system'
      ? (typeof window !== 'undefined' &&
          window.matchMedia('(prefers-color-scheme: dark)').matches)
        ? 'dark'
        : 'light'
      : theme
  if (typeof document !== 'undefined') {
    document.documentElement.classList.toggle('dark', resolved === 'dark')
    document.documentElement.style.colorScheme = resolved
  }
}