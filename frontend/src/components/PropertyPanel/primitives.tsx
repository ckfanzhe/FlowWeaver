/**
 * Shared form primitives used by every per-node form.
 *
 *   - `Field` — labelled wrapper around any input.
 *   - `NodeDataField<T>` — controlled-input helper that reads/writes a
 *     single path inside `node.data.config` (e.g. `['model', 'provider']`).
 *     Performs an immutable `structuredClone` before mutating so sibling
 *     keys in the config aren't lost.
 *   - `JsonField` — free-form JSON textarea that parses on blur and shows
 *     a validation hint.
 *
 * Kept tiny on purpose. Anything bigger belongs in its own form file.
 */
import { useState } from 'react'
import { useWorkflowStore } from '../../store/workflowStore'

// ─────────────────────────────────────────────────────────────────
// Label + input wrapper
// ─────────────────────────────────────────────────────────────────
export function Field({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <label className="block">
      <div className="field-label">{label}</div>
      {children}
    </label>
  )
}

// ─────────────────────────────────────────────────────────────────
// Path-based controlled input
// ─────────────────────────────────────────────────────────────────
export function NodeDataField<T>({
  nodeId,
  path,
  children,
}: {
  nodeId: string
  path: string[]
  children: (value: T, set: (v: T) => void) => React.ReactNode
}) {
  const data = useWorkflowStore((s) => s.nodes.find((n) => n.id === nodeId)?.data)
  const update = useWorkflowStore((s) => s.updateNodeData)
  const config = (data?.config ?? {}) as Record<string, unknown>
  let cursor: unknown = config
  for (const p of path) cursor = (cursor as Record<string, unknown>)?.[p]
  const set = (v: unknown) => {
    const next = structuredClone(config) as Record<string, unknown>
    let c = next
    for (let i = 0; i < path.length - 1; i++) {
      const k = path[i]
      if (c[k] === undefined) c[k] = {}
      c = c[k] as Record<string, unknown>
    }
    c[path[path.length - 1]] = v
    update(nodeId, { config: next })
  }
  return <>{children(cursor as T, set as (v: T) => void)}</>
}

// ─────────────────────────────────────────────────────────────────
// JSON textarea
// ─────────────────────────────────────────────────────────────────
export function JsonField({
  value,
  onChange,
  placeholder,
  rows = 3,
}: {
  value: unknown
  onChange: (v: unknown) => void
  placeholder?: string
  rows?: number
}) {
  const [draft, setDraft] = useState(() =>
    value === undefined || value === null || value === ''
      ? ''
      : JSON.stringify(value, null, 2)
  )
  const [err, setErr] = useState<string | null>(null)
  const commit = (text: string) => {
    if (text.trim() === '') {
      onChange(undefined)
      setErr(null)
      return
    }
    try {
      onChange(JSON.parse(text))
      setErr(null)
    } catch (e) {
      setErr((e as Error).message)
    }
  }
  return (
    <>
      <textarea
        className="input font-mono text-[11px]"
        rows={rows}
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={(e) => commit(e.target.value)}
      />
      {err && <p className="mt-1 text-[10px] text-danger">JSON: {err}</p>}
    </>
  )
}