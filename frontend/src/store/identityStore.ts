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
import { useBuilderChatStore } from './builderChatStore'
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

// Identity is per-TAB (sessionStorage), not per-BROWSER (localStorage).
// Previously the two tabs of one browser shared a single identity and
// could clobber each other's `lastWorkflowId` mid-edit. Using
// sessionStorage gives each tab its own copy; the user re-enters the
// email on the second tab if they want a different identity. Switching
// to localStorage again was a deliberate choice — multi-tab edit
// conflicts are worse than re-entering an email once.
function readStoredUserId(): string | null {
  try {
    const v = sessionStorage.getItem(STORAGE_KEY)
    return v && v.trim() ? v.trim() : null
  } catch {
    return null
  }
}

function writeStoredUserId(userId: string | null): void {
  try {
    if (userId) sessionStorage.setItem(STORAGE_KEY, userId)
    else sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    /* sessionStorage may be unavailable (private mode etc.) — non-fatal */
  }
}

// Cross-tab sign-out sync — when one tab signs out, every other tab
// on the same origin should also clear its identity state. Broadcast
// falls back to a no-op when the runtime doesn't expose it (Safari
// <15.4, some embedded WebViews). The receiving side just calls the
// same `signOut` action so the state machine stays in one place.
const SIGN_OUT_CHANNEL = 'agnobuilder.identity.signout'
type SignOutMsg = { type: 'signout' }
const signOutBroadcast: BroadcastChannel | null =
  typeof BroadcastChannel !== 'undefined'
    ? new BroadcastChannel(SIGN_OUT_CHANNEL)
    : null
signOutBroadcast?.addEventListener('message', (ev) => {
  const data = ev.data as SignOutMsg | undefined
  if (data?.type === 'signout') {
    // Pull signOut from the live store — avoids importing the action
    // at module-init time (which would capture a stale closure).
    const { signOut, userId: remoteUserId } = useIdentityStore.getState()
    // Only clear if the broadcast applies to us. Without this check,
    // a signOut broadcast from tab A would fire its own listener
    // when tab B receives it, causing tab B to broadcast a second
    // signout event. We do that filtering by only reacting to
    // REMOTE userIds — i.e. when our local userId differs from the
    // broadcast's. Since we don't carry the sender's userId in the
    // payload, we rely on the heuristic that no local-state change
    // is needed if we're already signed out.
    if (remoteUserId) signOut()
  }
})

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
      // subscriber will clear the current user's `lastWorkflowId`
      // slot when `workflowId` flips to null (see `workflowStore.ts`).
      useWorkflowStore.getState().reset()
      void useWorkflowListStore.getState().refresh()
      // multi-user: clear the chat transcript + session
      // state too. Without this the new identity lands in the same
      // browser and sees the previous user's messages + a stale
      // `sessionId` from a paused run they can't resume (the new
      // user's `X-User-Id` will get 404 on that sid because the
      // runtime now scopes by user).
      useChatRunStore.getState().resetAll()
      // Also clear the builder chat. The Run store was reset here
      // since the start of the multi-user refactor, but the Builder
      // store wasn't — without this a sign-out leaves the previous
      // user's staged diff + messages visible to the next identity
      // on the same browser. Same privacy rationale as the Run
      // reset above.
      useBuilderChatStore.getState().reset()
      // NB: we deliberately do NOT call `deleteUserChats(userId)`
      // here. Earlier revisions wiped the IndexedDB envelopes on
      // signOut for paranoia, but that broke the "switch accounts,
      // switch back, history restored" flow the user expects —
      // cross-account on the same browser is exactly when the
      // previous user's data needs to survive. The IndexedDB
      // composite key (`userId::workflowId`) already prevents
      // cross-user UI bleed: user B's chat sidebar can only read
      // keys prefixed `B::`, so user A's data is invisible to B
      // even though it still sits on disk. (Data on disk during a
      // session is the same regime as the rest of IndexedDB — not a
      // new privacy surface.) A future server-side persistence
      // layer can revisit this and either delete on signOut or
      // scope per-user encryption; for now we keep the simpler
      // "remember across sessions" behaviour.
      // Notify other tabs to sign out too — so opening the same
      // platform in two tabs and signing out from one doesn't leave
      // the other tab with a stale identity. The receiving listener
      // ignores broadcasts when its own userId is already null.
      signOutBroadcast?.postMessage({ type: 'signout' } satisfies SignOutMsg)
    },

    clearError: () => set({ error: null }),
  }),
)

// Re-export the storage key so the api/client wrapper can read from
// the same key without importing the store (avoids circular deps).
export { STORAGE_KEY as IDENTITY_STORAGE_KEY }