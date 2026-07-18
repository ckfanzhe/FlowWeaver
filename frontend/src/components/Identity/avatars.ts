/**
 * Cartoon avatar library.
 *
 * Eight fixed options so the picker stays visually scannable (more
 * than that and the popover becomes a wall of icons). Each avatar is
 * just a `(color, glyph)` pair — the popover renders them as a
 * colored circle with the glyph centered. No image URLs, no SVGs to
 * keep the bundle slim and the picker fully offline (matches the
 * platform's internal-network-only posture).
 *
 * `pickAvatarForEmail(email)` derives a stable avatar from the email
 * so a brand-new user (who hasn't picked yet) gets a deterministic
 * starting point. The hash is a tiny FNV-1a — good enough for visual
 * distribution, no security claim.
 */
export interface AvatarDef {
  id: string
  /** Display name (English — shown in the picker for clarity). */
  name: string
  /** Tailwind background color class. */
  bg: string
  /** Tailwind text color class (kept light for contrast on the bg). */
  fg: string
  /** Single character / emoji glyph. */
  glyph: string
}

export const AVATARS: ReadonlyArray<AvatarDef> = [
  { id: 'fox',     name: 'Fox',      bg: 'bg-orange-500', fg: 'text-white', glyph: '🦊' },
  { id: 'cat',     name: 'Cat',      bg: 'bg-pink-400',   fg: 'text-white', glyph: '🐱' },
  { id: 'dog',     name: 'Dog',      bg: 'bg-amber-500',  fg: 'text-white', glyph: '🐶' },
  { id: 'bear',    name: 'Bear',     bg: 'bg-stone-500',  fg: 'text-white', glyph: '🐻' },
  { id: 'frog',    name: 'Frog',     bg: 'bg-green-500',  fg: 'text-white', glyph: '🐸' },
  { id: 'panda',   name: 'Panda',    bg: 'bg-zinc-700',   fg: 'text-white', glyph: '🐼' },
  { id: 'robot',   name: 'Robot',    bg: 'bg-sky-500',    fg: 'text-white', glyph: '🤖' },
  { id: 'ghost',   name: 'Ghost',    bg: 'bg-indigo-400', fg: 'text-white', glyph: '👻' },
]

const AVATAR_BY_ID: Record<string, AvatarDef> = Object.fromEntries(
  AVATARS.map((a) => [a.id, a]),
)

/** Default avatar — used when the user hasn't picked one yet. */
export const DEFAULT_AVATAR_ID = 'fox'

export function getAvatar(id: string | null | undefined): AvatarDef {
  if (id && AVATAR_BY_ID[id]) return AVATAR_BY_ID[id]
  return AVATAR_BY_ID[DEFAULT_AVATAR_ID]
}

/** Deterministic fallback derived from the email (so two browsers
 *  with the same email agree on the same starting avatar). */
export function pickAvatarForEmail(email: string | null | undefined): AvatarDef {
  const fallback = AVATAR_BY_ID[DEFAULT_AVATAR_ID]
  if (!email) return fallback
  let hash = 2166136261 >>> 0
  for (let i = 0; i < email.length; i++) {
    hash ^= email.charCodeAt(i)
    hash = Math.imul(hash, 16777619) >>> 0
  }
  return AVATARS[hash % AVATARS.length]
}