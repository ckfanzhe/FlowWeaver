/**
 * Tiny typed fetch wrapper. Throws Error with the server's detail message on non-2xx.
 *
 * Auto-injects the `X-User-Id` header on every request from
 * `localStorage.agnobuilder.userId`. The multi-user backend reads
 * that header on every endpoint and uses it as the caller identity.
 *
 * `readUserId()` is a thin localStorage read; doing the lookup at
 * call time (rather than caching it once at module load) means the
 * "Switch user" affordance — which writes a new value to localStorage
 * — takes effect on the next request without needing a refresh.
 */

// localStorage key the identityStore writes/reads. Kept here (not in
// the store) so the API client has zero import-time coupling on the
// React store — module-load order matters when one file imports the
// other in dev.
export const USER_ID_STORAGE_KEY = 'agnobuilder.userId'

function readUserId(): string | null {
  try {
    const v = localStorage.getItem(USER_ID_STORAGE_KEY)
    return v && v.trim() ? v.trim() : null
  } catch {
    return null
  }
}

// Lazy lookup: `import.meta.env` is Vite-only. Reading it at call time
// (instead of module load) keeps this file importable from Node-side
// tools (tests via tsx, etc.) without a `import.meta.env` polyfill —
// the default kicks in when Vite isn't present.
function readBase(): string {
  const env = (import.meta as { env?: Record<string, string> }).env
  return env?.VITE_API_BASE ?? 'http://localhost:8880'
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init?.headers as Record<string, string> | undefined ?? {}),
  }
  // Inject X-User-Id if we have one in localStorage. The backend's
  // identity layer falls back to `user-default` when the header is
  // missing, so anonymous calls still work — but every workflow CRUD
  // should carry the header so RBAC scopes to the right user.
  const userId = readUserId()
  if (userId) headers['X-User-Id'] = userId
  const res = await fetch(`${readBase()}${path}`, { ...init, headers })
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const body = await res.json()
      // FastAPI's `detail` is usually a plain string. But our
      // connection-rules 422 returns a structured
      // `{errors: [...], message: "..."}` object — assigning that
      // directly to `msg` would stringify to "[object Object]" in
      // the toolbar. Detect the structured shape and pull out the
      // pre-joined `message`. Falls back to the raw detail / message
      // for any other API (e.g. Pydantic's array-of-errors format).
      const detail = (body as { detail?: unknown }).detail
      const topMessage = (body as { message?: unknown }).message
      if (detail && typeof detail === 'object') {
        const structured = detail as { message?: unknown; errors?: unknown }
        if (typeof structured.message === 'string') {
          msg = structured.message
        } else if (Array.isArray(structured.errors) && structured.errors.length) {
          // Last-resort: serialize the first error's `message` if present.
          const first = structured.errors[0] as { message?: unknown }
          msg = typeof first?.message === 'string' ? first.message : JSON.stringify(detail)
        } else {
          msg = JSON.stringify(detail)
        }
      } else if (typeof detail === 'string') {
        msg = detail
      } else if (typeof topMessage === 'string') {
        msg = topMessage
      }
    } catch {
      // ignore — keep the default HTTP status message
    }
    throw new ApiError(res.status, msg)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body !== undefined ? JSON.stringify(body) : undefined }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PUT', body: body !== undefined ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PATCH', body: body !== undefined ? JSON.stringify(body) : undefined }),
  delete: <T = void>(path: string) => request<T>(path, { method: 'DELETE' }),
  base: readBase(),
  /**
   * Raw fetch with the standard `X-User-Id` header injection, returning
   * the `Response` object directly. Use this when the caller needs the
   * raw body / headers (e.g. the export endpoints, which stream a
   * `.py` / `.json` file and read `Content-Disposition`).
   *
   * Calling raw `fetch()` instead bypasses the interceptor and the
   * backend falls back to `user-default` for the caller identity —
   * which 403s any workflow the caller owns but didn't create as
   * `user-default`. Always go through this helper for non-JSON
   * responses that need identity.
   */
  fetchRaw: (path: string, init?: RequestInit): Promise<Response> => {
    const headers: Record<string, string> = {
      ...(init?.headers as Record<string, string> | undefined ?? {}),
    }
    const userId = readUserId()
    if (userId) headers['X-User-Id'] = userId
    return fetch(`${readBase()}${path}`, { ...init, headers })
  },
}