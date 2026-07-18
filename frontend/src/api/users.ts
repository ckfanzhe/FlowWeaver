/**
 * User-identity API client — .
 *
 * Email IS the user identifier. There is no auth ceremony: the
 * frontend prompts for an email on first visit, calls `identify`,
 * and uses the returned `userId` as the `X-User-Id` header for
 * every subsequent request.
 *
 * On a new device, typing the same email re-runs `identify` and
 * the backend returns the existing `users` row (`created=false`).
 * The workflow list re-hydrates from there.
 *
 * `me()` is called on app boot to validate whatever's in
 * localStorage. A successful response means the identity is good;
 * a 404 means the localStorage value is stale (e.g. backend was
 * reset) and the frontend should clear it + re-prompt.
 *
 * Internal-network constraint: no rate limiting, no captcha. The
 * only validation is the email-format check the backend performs
 * (an `@` plus a dot in the domain).
 */
import { api } from './client'

export interface IdentifyRequest {
  email: string
  /** Optional preference — re-asserted on every identify so a returning
   *  user on a new device picks up the right locale immediately. */
  language?: string
  /** Optional avatar picker id (e.g. `"fox"`, `"robot"`). */
  avatarId?: string
  /** Optional UI theme preference — `"light" | "dark" | "system"`.
   *  Persisted on the user row  so the choice travels
   *  with the user across browsers. */
  theme?: string
}

export interface IdentifyResult {
  userId: string
  email: string
  tenantId: string
  created: boolean
  createdAt: string
  language: string | null
  avatarId: string | null
  theme: string | null
}

export interface MeResult {
  userId: string
  email: string | null
  tenantId: string
  language: string | null
  avatarId: string | null
  theme: string | null
}

export const usersApi = {
  identify: (req: IdentifyRequest) => api.post<IdentifyResult>('/api/v1/users/identify', req),

  me: () => api.get<MeResult>('/api/v1/users/me'),
}