/**
 * useFileDrop — global drag-and-drop receiver for external files.
 *
 * Listens on `document` so the user can drop a file anywhere on the
 * page. Ignores the palette's internal node drag (which uses
 * `application/agnobuilder-node-type`) so dropping a node type into the
 * canvas still works.
 *
 * The hook tracks three states:
 *   idle       — no drag in progress
 *   hovering   — a file is being dragged over the window (overlay time)
 *   rejected   — a drag is in progress but it's NOT a file (e.g. text)
 *
 * The caller is responsible for actually reading the file and acting on
 * it — the hook just hands you the `File` object.
 *
 * Usage:
 *   const { state, dropRef } = useFileDrop({
 *     accept: (f) => f.name.endsWith('.json'),
 *     onFile: (f) => importJson(f),
 *   })
 *   return <>{state === 'hovering' && <DropOverlay />}</>
 */
import { useEffect, useRef, useState } from 'react'

export type DropState = 'idle' | 'hovering' | 'rejected'

interface Options {
  /** Return true to accept this file (e.g. check extension). */
  accept?: (file: File) => boolean
  /** Called when a file is dropped and accepted. */
  onFile: (file: File) => void | Promise<void>
}

export function useFileDrop({ accept, onFile }: Options) {
  const [state, setState] = useState<DropState>('idle')
  // counter handles dragenter/dragleave on child elements correctly
  const depthRef = useRef(0)
  // keep latest callbacks in a ref so we don't re-bind listeners each render
  const cbRef = useRef({ accept, onFile })
  cbRef.current = { accept, onFile }

  useEffect(() => {
    // Guard: don't preventDefault on the whole document unless we know
    // the drag is a file. Otherwise the browser tries to navigate to
    // the file when dropped, which is worse than just letting the
    // event pass through.

    const isFileDrag = (e: DragEvent): boolean => {
      const types = e.dataTransfer?.types
      if (!types) return false
      // `Files` (plural) appears in `types` (not `files`) for file drags.
      for (let i = 0; i < types.length; i++) {
        if (types[i] === 'Files') return true
      }
      return false
    }

    const isInternalNodeDrag = (e: DragEvent): boolean => {
      const types = e.dataTransfer?.types
      if (!types) return false
      for (let i = 0; i < types.length; i++) {
        if (types[i] === 'application/agnobuilder-node-type') return true
      }
      return false
    }

    const onDragEnter = (e: DragEvent) => {
      if (isInternalNodeDrag(e)) return
      if (!isFileDrag(e)) return
      e.preventDefault()
      depthRef.current += 1
      setState('hovering')
    }

    const onDragOver = (e: DragEvent) => {
      if (isInternalNodeDrag(e)) return
      if (!isFileDrag(e)) return
      // required to allow the drop
      e.preventDefault()
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
    }

    const onDragLeave = (e: DragEvent) => {
      if (isInternalNodeDrag(e)) return
      if (!isFileDrag(e)) return
      e.preventDefault()
      depthRef.current = Math.max(0, depthRef.current - 1)
      if (depthRef.current === 0) setState('idle')
    }

    const onDrop = (e: DragEvent) => {
      if (isInternalNodeDrag(e)) {
        // let React Flow handle it; just reset state in case
        depthRef.current = 0
        setState('idle')
        return
      }
      e.preventDefault()
      depthRef.current = 0
      setState('idle')
      const files = e.dataTransfer?.files
      if (!files || files.length === 0) return
      const file = files[0]
      const { accept, onFile } = cbRef.current
      if (accept && !accept(file)) return
      void onFile(file)
    }

    document.addEventListener('dragenter', onDragEnter)
    document.addEventListener('dragover', onDragOver)
    document.addEventListener('dragleave', onDragLeave)
    document.addEventListener('drop', onDrop)
    return () => {
      document.removeEventListener('dragenter', onDragEnter)
      document.removeEventListener('dragover', onDragOver)
      document.removeEventListener('dragleave', onDragLeave)
      document.removeEventListener('drop', onDrop)
    }
  }, [])

  return { state, reset: () => setState('idle') }
}
