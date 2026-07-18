/**
 * Identity store — .
 *
 * Frontend's view of "who is the current browser". The platform has
 * no login; the user enters their email once, we POST it to
 * `/users/identify`, and the resulting `userId` (= email) becomes
 * the `X-User-Id` header for every subsequent request.
 *
 * The  follow-up layers per-user preferences onto the same
 * store: `language`, `avatarId`, and `theme`. All three are persisted
 * to the backend on every change so the next visit re-applies them
 * before the first render — no need to refresh after picking a new
 * language or theme on another device.
 *
 * Lifecycle:
 *
 *   1. App boot → `init()` reads `agnobuilder.userId` from
 *      localStorage and probes `/users/me` to confirm it's still
 *      valid. The response carries `language` / `theme`; if present,
 *      the store applies them so the next render lands in the
 *      user's preferred locale + theme.
 *
 *   2. User submits the gate → `identify(email)` POSTs to
 *      `/users/identify`, including the current locale + avatar +
 *      theme as preferences so a returning user on a new device
 *      picks them up immediately.
 *
 *   3. User picks a different avatar / language / theme from the
 *      UserMenu → `setAvatar(id)` / `setLanguage(locale)` /
 *      `setTheme(theme)`. All three update local state synchronously
 *      AND re-POST `/users/identify` so the backend stays in sync.
 *
 *   4. User picks "Switch user" → `signOut()` clears localStorage +
 *      state. App.tsx re-mounts the gate modal.
 *
 * `ready` is the gate App.tsx watches: nothing renders until it's
 * true, so the workflow list / canvas / templates never fire
 * requests before the caller has an identity.
 */
import { create } from 'zustand'
import { ApiError } from '../api/client'
import { usersApi } from '../api/users'
import { setLocale, type Locale } from '../i18n'
import { applyTheme, type Theme } from '../lib/theme'
import { useChatRunStore } from './chatRunStore'
import { useWorkflowListStore } from './workflowListStore'
import { useWorkflowStore } from './workflowStore'

interface IdentityState {
  userId: string | null
  email: string | null
  /** Picked avatar id; null means "derive a default from email". */
  avatarId: string | null
  /** Persisted language preference; null means "no preference stored". */
  language: string | null
  /** Persisted theme preference; null means "no preference stored". */
  theme: Theme | null
  /** True once we've resolved an identity on the backend (or know we
   *  need to prompt for one). False until `init()` settles. */
  ready: boolean
  /** Last server-side error from `identify()`; the modal surfaces it. */
  error: string | null
}

interface IdentityActions {
  /** Boot-time reconciliation with the backend. */
  init: () => Promise<void>
  /** Submit an email to the gate. Stores result + localStorage on success. */
  identify: (email: string) => Promise<void>
  /** Pick a new cartoon avatar; persists to the backend. */
  setAvatar: (avatarId: string) => Promise<void>
  /** Switch UI language AND persist the preference to the backend. */
  setLanguage: (locale: Locale) => Promise<void>
  /** Switch UI theme AND persist the preference to the backend. */
  setTheme: (theme: Theme) => Promise<void>
  /** Clear identity, force the gate modal to re-open. */
  signOut: () => void
  /** Clear just the modal's transient error (e.g. user types again). */
  clearError: () => void
}

const STORAGE_KEY = 'agnobuilder.userId'

function readStoredUserId(): string | null {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    return v && v.trim() ? v.trim() : null
  } catch {
    return null
  }
}

function writeStoredUserId(userId: string | null): void {
  try {
    if (userId) localStorage.setItem(STORAGE_KEY, userId)
    else localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* localStorage may be unavailable (private mode etc.) — non-fatal */
  }
}

export const useIdentityStore = create<IdentityState & IdentityActions>(
  (set, get) => ({
    userId: readStoredUserId(),
    email: null,
    avatarId: null,
    language: null,
    theme: null,
    ready: false,
    error: null,

    init: async () => {
      const stored = readStoredUserId()
      if (!stored) {
        // Fresh visitor — no localStorage, no header to send.
        set({ ready: true, userId: null, email: null })
        return
      }
      // Probe the backend. We need a confirmed `email` so the gate
      // can pre-fill it on stale-id recovery; without a 200 from
      // `/me` we can't distinguish "anonymous lazy-created row"
      // from "known user with verified email".
      try {
        const me = await usersApi.me()
        if (me.userId !== stored) {
          // localStorage drifted from the server's view. Trust the
          // server and re-sync.
          writeStoredUserId(me.userId)
        }
        set({
          ready: true,
          userId: me.userId,
          email: me.email,
          avatarId: me.avatarId,
          language: me.language,
          theme: (me.theme as Theme | null) ?? null,
        })
        // Apply the stored locale + theme before the rest of the app
        // renders so the user lands in their preferred state.
        if (me.language) setLocale(me.language as Locale)
        if (me.theme) applyTheme(me.theme as Theme)
      } catch (e) {
        // 404 or network error — treat as "no identity". Clear so
        // the next call doesn't keep retrying a stale header.
        const status = (e as ApiError).status
        if (status === 404 || status === undefined) {
          writeStoredUserId(null)
          set({ ready: true, userId: null, email: null })
        } else {
          // 5xx / 403 — leave the stored id alone (might come back
          // online), but still surface the gate so the user can
          // choose to sign out + retry.
          set({ ready: true, error: (e as Error).message })
        }
      }
    },

    identify: async (email) => {
      set({ error: null })
      try {
        // Re-assert the current locale + avatar + theme on every
        // identify so a returning user on a new device picks them
        // up immediately. The backend only stores the new values
        // when they actually differ from what's already there, so
        // this is cheap and idempotent.
        const { language, avatarId, theme } = get()
        const result = await usersApi.identify({
          email,
          language: language ?? undefined,
          avatarId: avatarId ?? undefined,
          theme: theme ?? undefined,
        })
        writeStoredUserId(result.userId)
        set({
          userId: result.userId,
          email: result.email,
          avatarId: result.avatarId,
          language: result.language,
          theme: (result.theme as Theme | null) ?? null,
          ready: true,
          error: null,
        })
      } catch (e) {
        set({ error: (e as Error).message })
        throw e
      }
    },

    setAvatar: async (avatarId) => {
      const { userId } = get()
      // Optimistic local update so the avatar circle re-renders
      // before the network round-trip finishes.
      set({ avatarId })
      if (!userId) return
      try {
        await usersApi.identify({ email: userId, avatarId })
      } catch (e) {
        // Non-fatal — the local state still holds the pick; the
        // next identify() will re-assert it.
        console.warn('persist avatar failed', e)
      }
    },

    setLanguage: async (locale) => {
      const { userId } = get()
      // Apply the locale synchronously so all subscribers (including
      // the template gallery memo) re-render in the new language.
      setLocale(locale)
      set({ language: locale })
      if (!userId) return
      try {
        await usersApi.identify({ email: userId, language: locale })
      } catch (e) {
        console.warn('persist language failed', e)
      }
    },

    setTheme: async (theme) => {
      // Apply synchronously so the DOM flips immediately; the
      // `<html>` `dark` class is what Tailwind reads for the colour
      // scheme. The backend round-trip is fire-and-forget so the
      // user sees the change instantly even if the network is slow.
      applyTheme(theme)
      set({ theme })
      const { userId } = get()
      if (!userId) return
      try {
        await usersApi.identify({ email: userId, theme })
      } catch (e) {
        console.warn('persist theme failed', e)
      }
    },

    signOut: () => {
      writeStoredUserId(null)
      set({
        userId: null,
        email: null,
        avatarId: null,
        language: null,
        theme: null,
        ready: true,
        error: null,
      })
      // Reset the workflow state so the new identity doesn't see the
      // previous user's canvas / list. The workflow store's own
      // subscriber will clear `agnobuilder.lastWorkflowId` when
      // `workflowId` flips to null (see `workflowStore.ts`).
      useWorkflowStore.getState().reset()
      void useWorkflowListStore.getState().refresh()
      //  multi-user: clear the chat transcript + session
      // state too. Without this the new identity lands in the same
      // browser and sees the previous user's messages + a stale
      // `sessionId` from a paused run they can't resume (the new
      // user's `X-User-Id` will get 404 on that sid because the
      // runtime now scopes by user).
      useChatRunStore.getState().resetAll()
    },

    clearError: () => set({ error: null }),
  }),
)

// Re-export the storage key so the api/client wrapper can read from
// the same key without importing the store (avoids circular deps).
export { STORAGE_KEY as IDENTITY_STORAGE_KEY }