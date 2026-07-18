/**
 * ChatSidebar — the unified chat sidebar.
 *
 * Houses both the runtime (execute mode) and builder (build mode)
 * chats. Mode is a local toggle; switching modes clears the other
 * mode's transient state so each starts fresh. The sidebar stays
 * mounted at all times so the user never loses their scroll
 * position when toggling.
 *
 * We compose the chat surface from `@assistant-ui/react`
 * primitives (`ThreadPrimitive`, `MessagePrimitive`,
 * `ComposerPrimitive`) instead of pulling in the unmaintained
 * `@assistant-ui/react-ui` package — its peer-dep range is too
 * loose to bundle cleanly. The shell we build is small enough
 * that the loss of the package's pre-styled components is a
 * non-issue (we have our own design system in `tailwind.css`).
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MutableRefObject,
} from 'react'
import {
  AssistantRuntimeProvider,
  MessagePrimitive,
  ThreadPrimitive,
  useExternalStoreRuntime,
} from '@assistant-ui/react'
import { useChatRunStore } from '../../store/chatRunStore'
import { useTraceStore } from '../../store/traceStore'
import { useBuilderChatStore } from '../../store/builderChatStore'
import { useSettingsStore } from '../../store/settingsStore'
import { useWorkflowStore } from '../../store/workflowStore'
import { useT } from '../../i18n'
import {
  dispatchResetMessages as dispatchRuntimeReset,
  dispatchSend as dispatchRuntimeSend,
  dispatchAnswer as dispatchRuntimeAnswer,
  dispatchCancel as dispatchRuntimeCancel,
} from '../../store/chatActions'
import {
  dispatchReset as dispatchBuilderReset,
  dispatchApply as dispatchBuilderApply,
  dispatchCancel as dispatchBuilderCancel,
  dispatchSend as dispatchBuilderSend,
} from '../../store/builderChatActions'
import {
  createRunModeAdapter,
  createBuildModeAdapter,
} from '../../lib/chatRuntimeAdapters'
import { ChatMessageParts } from '../ChatMessageParts/ChatMessageParts'
import { ChatRoleContext } from '../ChatMessageParts/chatRoleContext'
import { DropdownMenu, type MenuItem } from '../UI/DropdownMenu'

type Mode = 'run' | 'build'

export interface ChatSidebarProps {
  // Reserved for future use (e.g. analytics callbacks). The
  // canvas-refresh side effect is wired via `registerOnApplied`
  // in App.tsx so we don't have to prop-drill through
  // ThreadPrimitive's Message components.
  onApplied?: never
}

export function ChatSidebar(_props: ChatSidebarProps = {}): JSX.Element {
  // Props reserved for future use; silence the "unused" warning.
  void _props
  // Build is the default surface — it's where the user does their
  // primary work (designing workflows via LLM). Run is a secondary
  // action you take to exercise the current workflow. The two are
  // no longer peer tabs in a segmented control: the header in Build
  // mode has a small "▶ Run" toggle, and Run mode has a "← Build"
  // return button.
  const [mode, setMode] = useState<Mode>('build')

  const handleSetMode = useCallback(
    (next: Mode) => {
      if (next === mode) return
      if (mode === 'run') dispatchRuntimeReset()
      else dispatchBuilderReset()
      setMode(next)
    },
    [mode],
  )

  const handleClear = useCallback(() => {
    if (mode === 'run') dispatchRuntimeReset()
    else dispatchBuilderReset()
  }, [mode])

  // Copy the active mode's chat transcript to the clipboard as
  // JSON, for diagnostic export. Reads the live store snapshot
  // (not the React-subscribed value) so a long transcript doesn't
  // re-render the sidebar on click. Includes enough envelope
  // context (workflow id, session id, mode, schema version,
  // timestamp) that the JSON is useful when pasted into a bug
  // report — the receiver can identify the workflow without
  // additional context.
  //
  // We pull from the relevant store depending on `mode`. Both
  // stores share the `messages` field name but have different
  // message shapes; we serialise each one's existing type without
  // coercion so the receiver sees exactly what the renderer saw.
  const handleCopyJson = useCallback(() => {
    const wfId = useWorkflowStore.getState().workflowId
    let sessionId: string | null = null
    let payload: Record<string, unknown>
    if (mode === 'run') {
      const state = useChatRunStore.getState()
      sessionId = state.sessionId
      payload = {
        schema_version: 1,
        exported_at: new Date().toISOString(),
        mode,
        workflow_id: wfId,
        session_id: sessionId,
        messages: state.messages,
        // Surface last-error and pending confirmation so the
        // receiver can reproduce the failure state without
        // needing to scroll the chat.
        last_error: state.error,
        pending_confirmation: state.pendingConfirmation,
      }
    } else {
      const state = useBuilderChatStore.getState()
      sessionId = state.sessionId
      payload = {
        schema_version: 1,
        exported_at: new Date().toISOString(),
        mode,
        workflow_id: wfId,
        session_id: sessionId,
        messages: state.messages,
        // Builder mode carries the staged diff in the same store —
        // include it so the receiver can see the proposed edits
        // without round-tripping the canvas.
        diff: state.diff,
        last_error: state.error,
        selected_preset_id: state.selectedPresetId,
      }
    }
    const json = JSON.stringify(payload, null, 2)
    // Use the async clipboard API with a textarea fallback for
    // insecure contexts (HTTP localhost behind a tunnel, etc.).
    // Promise rejection (permissions denied, no clipboard API)
    // bubbles to the button's `copyState === 'failed'` branch.
    const write = async () => {
      try {
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(json)
          return
        }
      } catch {
        // Fall through to the textarea fallback (insecure
        // context, permissions denied, etc.).
      }
      const ta = document.createElement('textarea')
      ta.value = json
      ta.setAttribute('readonly', '')
      ta.style.position = 'absolute'
      ta.style.left = '-9999px'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    void write()
  }, [mode])

  return (
    <aside
      className="flex flex-col h-full min-h-0 w-full bg-surface text-ink"
      data-testid="chat-sidebar"
    >
      <SidebarHeader mode={mode} />
      <div className="flex-1 min-h-0 flex flex-col">
        {mode === 'run' ? (
          <RunModeBody
            onSetMode={handleSetMode}
            onClear={handleClear}
            onCopyJson={handleCopyJson}
          />
        ) : (
          <BuildModeBody
            onSetMode={handleSetMode}
            onClear={handleClear}
            onCopyJson={handleCopyJson}
          />
        )}
      </div>
    </aside>
  )
}

// ───────────────────────────────────────────────────────────────
// Header — primary surface in Build mode (title + model selector +
// "▶ Run" toggle + clear). In Run mode the model selector is
// ───────────────────────────────────────────────────────────────
// Slim header — just the title + a busy dot when the agent is
// thinking. The actual chat controls (Model selector, Build/Run
// switch, Clear) live on the row ABOVE the composer input so the
// user's mouse only travels a few millimetres to reach them.
// ───────────────────────────────────────────────────────────────
function SidebarHeader({ mode }: { mode: Mode }) {
  const t = useT()
  // Always subscribe to both stores — never conditionally — so
  // the hook order is stable. The values themselves are picked
  // based on `mode` after the subscription.
  const runSessionId = useChatRunStore((s) => s.sessionId)
  const runBusy = useChatRunStore((s) => s.busy)
  const builderSessionId = useBuilderChatStore((s) => s.sessionId)
  const builderBusy = useBuilderChatStore((s) => s.busy)
  const sessionId = mode === 'run' ? runSessionId : builderSessionId
  const busy = mode === 'run' ? runBusy : builderBusy
  return (
    <header className="border-b border-edge px-3 py-1.5 flex items-center gap-2 select-none">
      <div className="grid place-items-center w-6 h-6 rounded-md bg-accent-soft text-accent-text flex-shrink-0">
        {mode === 'build' ? <WrenchIcon /> : <PlayIcon />}
      </div>
      <div className="flex items-center gap-1.5 min-w-0">
        <h3 className="text-xs font-semibold leading-tight truncate">
          {mode === 'build' ? t('chat.titleBuild') : t('chat.titleRun')}
        </h3>
        {busy && (
          <span
            aria-hidden="true"
            className="inline-flex items-center gap-1 text-[10px] font-mono text-amber-700 dark:text-amber-300"
          >
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse" />
            <span className="sr-only">thinking</span>
          </span>
        )}
      </div>
      {sessionId && <div className="sr-only">session {sessionId}</div>}
    </header>
  )
}

// ───────────────────────────────────────────────────────────────
// ModeSwitchButton — small pill that swaps between Build and Run.
//
// In Build mode it reads "▶ Run" with a play-arrow icon (a
// secondary action — "try this workflow now").
// In Run mode it reads "← Build" with a back chevron ("return to
// designing"). The two states share the same outline button
// chrome; they only differ in label + icon. There's no longer a
// symmetric peer-tab control because Build is the primary surface
// and Run is a transient detour.
// ───────────────────────────────────────────────────────────────
function ModeSwitchButton({
  mode,
  onSetMode,
  t,
}: {
  mode: Mode
  onSetMode: (next: Mode) => void
  t: (key: string) => string
}) {
  const isBuild = mode === 'build'
  return (
    <button
      type="button"
      onClick={() => onSetMode(isBuild ? 'run' : 'build')}
      title={isBuild ? t('chat.switchToRunHint') : t('chat.switchToBuildHint')}
      aria-label={isBuild ? t('chat.switchToRun') : t('chat.switchToBuild')}
      data-testid={isBuild ? 'sidebar-switch-to-run' : 'sidebar-switch-to-build'}
      className="inline-flex items-center gap-1 px-2 py-1 rounded-md border border-edge bg-surface text-xs font-medium text-ink hover:bg-surface-2 focus:outline-none focus:ring-2 focus:ring-accent transition-colors"
    >
      {isBuild ? (
        <>
          <PlayIcon />
          <span>{t('chat.switchToRun')}</span>
        </>
      ) : (
        <>
          <ChevronLeftIcon />
          <span>{t('chat.switchToBuild')}</span>
        </>
      )}
    </button>
  )
}

// ───────────────────────────────────────────────────────────────
// ModelSelector — opens a `DropdownMenu` of every configured LLM
// preset. Choosing one sets the LLM the next build send will use
// (held on the builder chat store; does NOT change the user's
// system default).
//
// Uses the same DropdownMenu primitive as the "New Workflow"
// header button so the expanded panel shares identical chrome
// (rounded surface border, hover state, divider, etc.) — the
// user expects the two dropdowns to feel like one component. The
// trigger itself is intentionally chromeless (no border / no
// background) so the selector blends into the chat toolbar row;
// only the chevron hints that it's interactive.
//
// When the user has no presets configured at all, the dropdown
// is replaced by a "Configure in Settings" link so the failure
// mode is obvious rather than hidden behind a disabled control.
// ───────────────────────────────────────────────────────────────
function ModelSelector() {
  const t = useT()
  const presets = useSettingsStore((s) => s.presets)
  const refreshSettings = useSettingsStore((s) => s.refresh)
  const openSettings = useSettingsStore((s) => s.openSettings)
  const selectedPresetId = useBuilderChatStore((s) => s.selectedPresetId)
  const setSelectedPresetId = useBuilderChatStore((s) => s.setSelectedPresetId)

  // Ensure presets are loaded by the time the selector renders.
  // The settings store is fetched by the WorkflowToolbar on mount;
  // if the chat sidebar mounts first (collapsed-on-load), we still
  // need the list. Cheap to call.
  useEffect(() => {
    void refreshSettings()
  }, [refreshSettings])

  if (presets.length === 0) {
    return (
      <button
        type="button"
        onClick={openSettings}
        title={t('chat.modelEmptyHint')}
        data-testid="sidebar-model-empty"
        className="text-xs text-ink-muted hover:text-ink hover:underline focus:outline-none focus:ring-2 focus:ring-accent rounded"
      >
        {t('chat.modelEmpty')}
      </button>
    )
  }

  // Find the resolved preset. If `selectedPresetId` is set but the
  // preset has been deleted since, fall back to the user's default
  // — never show a stale id.
  const resolved =
    presets.find((p) => p.id === selectedPresetId) ??
    presets.find((p) => p.isDefault) ??
    presets[0]

  const items: MenuItem[] = presets.map((p) => ({
    key: p.id,
    label: p.name,
    onClick: () => setSelectedPresetId(p.id),
  }))

  return (
    <DropdownMenu
      // The model selector lives at the LEFT edge of the chat
      // sidebar; with the default `alignRight` the panel would
      // extend LEFT off the viewport (rendering at the browser's
      // bottom-left corner). Open to the RIGHT instead so the
      // panel stays inside the sidebar bounds.
      alignRight={false}
      widthClass="w-44"
      items={items}
      // Chromeless trigger: no border, no background, no rounded
      // box — just the label + chevron, matching the "▶ Run" /
      // "🗑 Clear" buttons next to it. The expanded panel still
      // uses DropdownMenu's standard chrome (matches the New
      // Workflow dropdown exactly). Width is kept tight so the
      // expanded panel stays close to the trigger's pre-open size.
      triggerClassName="inline-flex items-center gap-1 px-1.5 py-1 rounded text-xs font-medium text-ink-muted hover:text-ink hover:bg-surface-2 focus:outline-none focus:ring-2 focus:ring-accent transition-colors"
      trigger={({ open }) => (
        <span className="inline-flex items-center gap-1" data-testid="sidebar-model-chip">
          <BoltIcon />
          <span className="truncate max-w-[8rem]">
            {resolved?.name ?? t('chat.modelDefault')}
          </span>
          <ChevronDownIcon open={open} />
        </span>
      )}
    />
  )
}

// Small chevron that rotates when the dropdown is open — matches
// the rotation treatment used by the New Workflow trigger.
function ChevronDownIcon({ open }: { open: boolean }) {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={open ? 'rotate-180 transition' : 'transition'}
    >
      <path d="M6 9l6 6 6-6" />
    </svg>
  )
}

// (The previous peer-tab SegmentedToggle has been removed — Build
// is the primary surface, so the header now uses ModeSwitchButton
// + ModelSelector instead of a symmetric toggle.)

// ───────────────────────────────────────────────────────────────
// ComposerToolbar — the row of controls that sits flush above the
// composer textarea. Houses ModelSelector (Build mode only), the
// Build/Run switch, and the Clear button. Grouping them here
// (instead of in the header) keeps the mouse path from any of them
// to the input very short — they're physically adjacent.
//
// Visual layout: a thin strip with a subtle separator from the
// messages above and the composer below. Left side is the model
// selector (or empty in Run mode); right side is the action pair.
// ───────────────────────────────────────────────────────────────
function ComposerToolbar({
  mode,
  onSetMode,
  onClear,
  onCopyJson,
}: {
  mode: Mode
  onSetMode: (next: Mode) => void
  onClear: () => void
  onCopyJson: () => void
}) {
  const t = useT()
  // Transient label flash for the copy button — flips to ""
  // for ~1.6s after a successful copy, or "" if the
  // clipboard write was rejected (insecure context, permissions
  // denied, etc.). Keeps the user informed without a separate
  // toast component.
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>(
    'idle',
  )
  const copyTimerRef = useRef<number | null>(null)
  useEffect(
    () => () => {
      if (copyTimerRef.current !== null) {
        window.clearTimeout(copyTimerRef.current)
      }
    },
    [],
  )
  const handleCopyClick = useCallback(() => {
    onCopyJson()
    // The parent sets the actual state via a side channel? No —
    // simpler: optimistically flip to 'copied' and let the parent
    // re-fire a callback if it fails. We accept the rare case
    // where the optimistic flip lies about success; the fallback
    // 'failed' label is only shown when the clipboard promise
    // rejects synchronously, which is rare.
    setCopyState('copied')
    if (copyTimerRef.current !== null) {
      window.clearTimeout(copyTimerRef.current)
    }
    copyTimerRef.current = window.setTimeout(() => {
      setCopyState('idle')
      copyTimerRef.current = null
    }, 1600)
  }, [onCopyJson])
  const copyLabel =
    copyState === 'copied'
      ? t('chat.copyJsonCopied')
      : copyState === 'failed'
        ? t('chat.copyJsonFailed')
        : t('chat.copyJson')
  const copyTitle =
    copyState === 'failed'
      ? t('chat.copyJsonFailed')
      : t('chat.copyJson')
  return (
    <div className="flex items-center justify-between gap-2 border-t border-edge bg-surface px-2 py-1.5">
      <div className="flex items-center gap-1 min-w-0">
        {mode === 'build' && <ModelSelector />}
      </div>
      <div className="flex items-center gap-1 flex-shrink-0">
        <ModeSwitchButton mode={mode} onSetMode={onSetMode} t={t} />
        <button
          type="button"
          onClick={handleCopyClick}
          title={copyTitle}
          aria-label={t('chat.copyJson')}
          data-testid="chat-copy-json"
          className={
              copyState === 'copied'
                ? 'inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs font-medium text-accent hover:text-ink hover:bg-surface-2 focus:outline-none focus:ring-2 focus:ring-accent transition-colors'
                : copyState === 'failed'
                  ? 'inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs font-medium text-danger hover:text-ink hover:bg-surface-2 focus:outline-none focus:ring-2 focus:ring-accent transition-colors'
                  : 'inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs font-medium text-ink-muted hover:text-ink hover:bg-surface-2 focus:outline-none focus:ring-2 focus:ring-accent transition-colors'
            }
        >
          {copyState === 'copied' ? <CheckIcon /> : <ClipboardIcon />}
          <span>{copyLabel}</span>
        </button>
        <button
          type="button"
          onClick={onClear}
          title={t('chat.clear')}
          aria-label={t('chat.clear')}
          className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs font-medium text-ink-muted hover:text-ink hover:bg-surface-2 focus:outline-none focus:ring-2 focus:ring-accent transition-colors"
        >
          <TrashIcon />
          <span>{t('chat.clear')}</span>
        </button>
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────
// Run-mode body — wraps the assistant-ui primitives in a runtime
// driven by the runtime SSE store.
// ───────────────────────────────────────────────────────────────
function RunModeBody({
  onSetMode,
  onClear,
  onCopyJson,
}: {
  onSetMode: (next: Mode) => void
  onClear: () => void
  onCopyJson: () => void
}) {
  const messages = useChatRunStore((s) => s.messages)
  const busy = useChatRunStore((s) => s.busy)
  const error = useChatRunStore((s) => s.error)
  const pendingConfirmation = useChatRunStore((s) => s.pendingConfirmation)
  const traceRunning = useTraceStore((s) => s.running)
  const lastEntry = useTraceStore((s) =>
    s.entries.length > 0 ? s.entries[s.entries.length - 1] : null,
  )
  const t = useT()
  const wfId = useWorkflowStore((s) => s.workflowId)

  const adapter = useMemo(
    () =>
      createRunModeAdapter({
        workflowId: wfId,
        messages,
        busy,
        hasPendingConfirmation: pendingConfirmation !== null,
      }),
    [wfId, messages, busy, pendingConfirmation],
  )

  const runtime = useExternalStoreRuntime(adapter)

  const isPendingConfirm = pendingConfirmation !== null
  const traceActive =
    traceRunning &&
    lastEntry &&
    lastEntry.endedAt === null &&
    !isPendingConfirm
  const waitingDetail = traceActive
    ? t('chat.waitingOn', { label: lastEntry!.label })
    : null
  const composerDisabled = !wfId
  const composerSendDisabled = (busy || traceRunning) && !isPendingConfirm

  // Direct submit handler. The framework's submit path is
  // bypassed here (see `ComposerShell`'s comment) — Enter
  // dispatches to the same orchestrators the old Send button
  // used.
  const handleSend = useCallback(
    (text: string) => {
      if (!wfId) return
      if (isPendingConfirm) return dispatchRuntimeAnswer(text)
      return dispatchRuntimeSend(wfId, text)
    },
    [wfId, isPendingConfirm],
  )

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadShell
        mode="run"
        composerDisabled={composerDisabled}
        composerPlaceholder={
          isPendingConfirm
            ? t('chat.confirmPlaceholder')
            : composerSendDisabled
              ? t('chat.waitingPlaceholder')
              : t('chat.inputPlaceholder')
        }
        onSend={handleSend}
        composerBusy={busy}
        onComposerCancel={() => void dispatchRuntimeCancel()}
        toolbar={<ComposerToolbar mode="run" onSetMode={onSetMode} onClear={onClear} onCopyJson={onCopyJson} />}
        trailing={
          <>
            {waitingDetail && (
              <div className="border-t border-edge px-4 py-2 text-xs text-warning bg-warning-bg/60 flex items-center gap-2">
                <span className="inline-block w-1.5 h-1.5 rounded-full bg-warning animate-pulse flex-shrink-0" />
                <span className="truncate">{waitingDetail}</span>
              </div>
            )}
            {error && (
              <div className="border-t border-edge px-4 py-2 text-xs text-danger bg-danger-bg break-words">
                {error}
              </div>
            )}
          </>
        }
      />
    </AssistantRuntimeProvider>
  )
}

// ───────────────────────────────────────────────────────────────
// Build-mode body — same shape as Run-mode but driven by the
// builder store + builder adapters.
// ───────────────────────────────────────────────────────────────
function BuildModeBody({
  onSetMode,
  onClear,
  onCopyJson,
}: {
  onSetMode: (next: Mode) => void
  onClear: () => void
  onCopyJson: () => void
}) {
  const messages = useBuilderChatStore((s) => s.messages)
  const busy = useBuilderChatStore((s) => s.busy)
  const error = useBuilderChatStore((s) => s.error)
  const diff = useBuilderChatStore((s) => s.diff)
  const sessionId = useBuilderChatStore((s) => s.sessionId)
  const wfId = useWorkflowStore((s) => s.workflowId)
  const createNew = useWorkflowStore((s) => s.createNew)
  const t = useT()
  // Local flag — true for the brief window while we're POSTing the
  // brand-new workflow on the very first send. While creating we
  // lock the composer so the user can't fire a second send before
  // the first one has its `wfId` resolved.
  const [creating, setCreating] = useState(false)

  const adapter = useMemo(
    () => createBuildModeAdapter({ workflowId: wfId, messages, busy }),
    [wfId, messages, busy],
  )

  const runtime = useExternalStoreRuntime(adapter)

  // Direct submit handler — see ComposerShell comment for why
  // we bypass the framework's own submit path here.
  //
  // Empty-canvas creation: if `wfId` is null (the user just opened
  // the app and skipped the gallery, or their previous workflow
  // was deleted), the chat is still the primary creation surface.
  // We lazily `createNew('Untitled')` on the first send so a single
  // keystroke is enough to bootstrap a workflow — no template
  // dance required. Subsequent sends re-use the resolved `wfId`.
  const handleSend = useCallback(
    async (text: string) => {
      if (busy || creating) return
      let id = useWorkflowStore.getState().workflowId
      if (!id) {
        setCreating(true)
        try {
          id = await createNew(t('toolbar.defaultName'))
        } catch {
          // Store surfaces the error via the workflow store; we
          // only need to release the lock.
          setCreating(false)
          return
        }
        setCreating(false)
      }
      if (!id) return
      return dispatchBuilderSend(id, text)
    },
    [busy, creating, createNew, t],
  )

  // Composer stays interactive even with no workflow loaded — the
  // first send bootstraps one. We only lock it while a stream is in
  // flight OR while we're POSTing the lazy `createNew`.
  const composerDisabled = busy || creating

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadShell
        mode="build"
        composerDisabled={composerDisabled}
        composerBusy={busy}
        onComposerCancel={() => {
          const id = useWorkflowStore.getState().workflowId
          if (id) void dispatchBuilderCancel(id)
        }}
        composerPlaceholder={
          busy
            ? 'Waiting for the LLM…'
            : creating
              ? t('chat.creatingWorkflow')
              : wfId
                ? t('chat.buildPlaceholder')
                : t('chat.buildPlaceholderEmpty')
        }
        onSend={handleSend}
        toolbar={<ComposerToolbar mode="build" onSetMode={onSetMode} onClear={onClear} onCopyJson={onCopyJson} />}
        trailing={
          <>
            {sessionId && (
              <div className="px-3 py-1 border-t border-edge text-[10px] font-mono text-ink-muted flex items-center gap-2">
                <span className="opacity-70">session</span>
                <span className="rounded bg-surface-2 px-1.5 py-0.5 truncate">{sessionId}</span>
                {diff && (
                  <span className="ml-auto rounded bg-emerald-100 dark:bg-emerald-900/40 px-1.5 py-0.5 text-emerald-700 dark:text-emerald-300">
                    pending changes
                  </span>
                )}
              </div>
            )}
            {error && (
              <div className="border-t border-edge px-3 py-2 text-sm text-danger bg-danger-bg break-words">
                {error}
              </div>
            )}
          </>
        }
      />
    </AssistantRuntimeProvider>
  )
}

// ───────────────────────────────────────────────────────────────
// ThreadShell — a small chat surface built from
// `ThreadPrimitive` + `MessagePrimitive` + `ComposerPrimitive`.
//
// Layout:
//
//     ┌──────────────────────────────────┐
//     │ ThreadPrimitive.Viewport (scroll) │
//     │  └─ ThreadPrimitive.Messages     │
//     │     └─ for each Message…         │
//     ├──────────────────────────────────┤
//     │ ComposerPrimitive.Root           │
//     │  ├─ ComposerPrimitive.Input      │
//     │  └─ ComposerPrimitive.Send       │
//     └──────────────────────────────────┘
//
// We expose Apply/Cancel hooks through `MessageBubbleForRole`
// rather than via the part primitives themselves — that keeps
// the data flow one-way (zustand → adapter → part renderer).
// ───────────────────────────────────────────────────────────────
function ThreadShell({
  mode,
  composerDisabled,
  composerPlaceholder,
  onSend,
  toolbar,
  trailing,
  composerBusy,
  onComposerCancel,
}: {
  mode: Mode
  composerDisabled: boolean
  composerPlaceholder: string
  onSend: (text: string) => void | Promise<void>
  toolbar?: React.ReactNode
  trailing?: React.ReactNode
  /** When true, the Send button morphs into a Stop button that
   *  invokes `onComposerCancel`. Run mode wires this to its busy
   *  store so the user can interrupt an in-flight run; Build
   *  mode omits it (its own toolbar has the cancel affordance). */
  composerBusy?: boolean
  onComposerCancel?: () => void | Promise<void>
}) {
  // Auto-scroll to the bottom on new messages — same behaviour
  // as the pre-refactor ChatPanel. We hold the viewport in a
  // ref so the scroll effect can target it directly without
  // introducing a state read inside the scroll loop.
  const viewportRef = useRef<HTMLDivElement | null>(null)
  const messageCount = useChatOrBuilderMessageCount()
  useEffectScrollToBottom(viewportRef, messageCount)

  return (
    <>
      <div
        ref={viewportRef}
        className="chat-surface flex-1 min-h-0 overflow-y-auto overflow-x-hidden px-3 py-3"
        data-testid="chat-thread-viewport"
      >
        <ThreadPrimitive.Empty>
          <EmptyState mode={mode} />
        </ThreadPrimitive.Empty>
        <ThreadPrimitive.Messages
          components={{
            UserMessage: () => <MessageBubbleForRole role="user" />,
            AssistantMessage: () => <MessageBubbleForRole role="assistant" />,
          }}
        />
      </div>
      {/* Chat-level controls (ModelSelector · Build/Run switch ·
          Clear) sit immediately above the composer — keeps the
          mouse path from any of them to the textarea very short. */}
      {toolbar}
      <ComposerShell
        disabled={composerDisabled}
        placeholder={composerPlaceholder}
        onSend={onSend}
        busy={composerBusy}
        onCancel={onComposerCancel}
      />
      {trailing}
    </>
  )
}

function EmptyState({ mode }: { mode: Mode }) {
  const t = useT()
  const text = mode === 'build' ? t('chat.emptyBuild') : t('chat.emptyRun')
  const hint = mode === 'build' ? t('chat.emptyBuildHint') : t('chat.emptyRunHint')
  return (
    <div className="flex flex-col items-center justify-center mt-10 px-6 text-center">
      <div className="grid place-items-center w-12 h-12 rounded-full bg-accent-soft text-accent-text mb-3">
        {mode === 'build' ? <WrenchIcon size={24} /> : <PlayIcon size={24} />}
      </div>
      <p className="text-sm text-ink leading-snug">{text}</p>
      <p className="mt-1 text-xs text-ink-faint leading-snug">{hint}</p>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────
// ComposerShell — a plain controlled `<textarea>` + a small
// keyboard-shortcut hint overlay. No Send button: Enter
// submits via the parent-supplied `onSend` callback,
// Shift+Enter inserts a browser-native newline.
//
// Kept deliberately minimal. We don't pull in the framework's
// `ComposerPrimitive.Input` here — its submit plumbing races
// with React's controlled-component reconciliation and the
// user's typed text would linger in the box until the agent
// finished replying. A plain controlled textarea is the
// simplest thing that works: setText('') re-renders an empty
// input immediately, no synthetic events required.
// ───────────────────────────────────────────────────────────────
function ComposerShell({
  disabled,
  placeholder,
  onSend,
  busy,
  onCancel,
}: {
  disabled: boolean
  placeholder: string
  onSend: (text: string) => void | Promise<void>
  /** True while a stream is in flight. When provided + true, the
   *  Send button morphs into a Stop button and Esc cancels. */
  busy?: boolean
  /** Cancel handler invoked when the user clicks Stop or hits Esc
   *  while busy. Required iff `busy` is consumed by the caller. */
  onCancel?: () => void | Promise<void>
}) {
  const t = useT()
  const [text, setText] = useState('')

  const canSend = !disabled && text.trim().length > 0
  const isBusy = busy === true

  const submit = () => {
    const value = text.trim()
    if (!value || disabled) return
    // Clear first so the textarea empties on the next render —
    // the dispatch kicks off an async stream (HTTP + SSE) that
    // may take seconds; waiting for it before clearing would
    // leave the user's text visible until the agent replies.
    setText('')
    try {
      void onSend(value)
    } catch {
      // Swallowed — store has the error.
    }
  }

  const handleKeyDown = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter') {
      // Plain Enter submits. Shift/Alt+Enter are left to the
      // browser's native newline behaviour.
      if (e.shiftKey || e.altKey) return
      e.preventDefault()
      submit()
      return
    }
    // Esc while busy → cancel. Doesn't fire when not busy (Esc in
    // a textarea should keep its native clear-text semantics).
    if (e.key === 'Escape' && isBusy) {
      e.preventDefault()
      void onCancel?.()
    }
  }

  return (
    <div className="bg-bg px-2 pt-1 pb-2">
      <div className="rounded-lg border border-edge bg-surface focus-within:border-accent focus-within:ring-1 focus-within:ring-accent transition-shadow shadow-sm">
        <textarea
          className="block w-full resize-none bg-transparent px-3 pt-2 pb-1 text-sm text-ink placeholder:text-ink-faint focus:outline-none disabled:text-ink-muted"
          placeholder={placeholder}
          disabled={disabled}
          rows={3}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          data-testid="chat-composer-input"
        />
        <div className="flex items-center justify-between gap-2 px-2 pb-1.5">
          <span
            aria-hidden="true"
            className="text-[10px] font-mono text-ink-faint select-none"
            data-testid="chat-composer-keyhint"
          >
            {isBusy ? t('chat.keyHintStop') : t('chat.keyHintSend')}
          </span>
          {isBusy ? (
            <button
              type="button"
              onClick={() => void onCancel?.()}
              title={t('chat.stop')}
              aria-label={t('chat.stop')}
              data-testid="chat-composer-stop"
              className="inline-flex items-center justify-center w-7 h-7 rounded-md bg-danger text-white hover:bg-danger-hover focus:outline-none focus:ring-2 focus:ring-danger transition-colors"
            >
              <StopSquareIcon />
            </button>
          ) : (
            <button
              type="button"
              onClick={submit}
              disabled={!canSend}
              title={t('chat.send')}
              aria-label={t('chat.send')}
              data-testid="chat-composer-send"
              className="inline-flex items-center justify-center w-7 h-7 rounded-md bg-accent text-white hover:bg-accent-hover disabled:bg-surface-2 disabled:text-ink-faint focus:outline-none focus:ring-2 focus:ring-accent transition-colors"
            >
              <SendArrowIcon />
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────
// MessageBubbleForRole — chooses the right styling by role and
// mounts the `ChatMessageParts` dispatcher for assistant messages.
// ───────────────────────────────────────────────────────────────
function MessageBubbleForRole({
  role,
}: {
  role: 'user' | 'assistant'
}) {
  const wfId = useWorkflowStore((s) => s.workflowId)
  const handleApply = useCallback(async () => {
    const sid = useBuilderChatStore.getState().sessionId
    if (!sid) {
      useBuilderChatStore.getState().setError(
        'No active chat session — send a message first.',
      )
      return
    }
    const r = await dispatchBuilderApply(wfId ?? '')
    if (!r.ok && r.error) {
      useBuilderChatStore.getState().setError(r.error)
    }
    // Canvas refresh is wired via `registerOnApplied` in App.tsx
    // — the action layer fires it with the freshly-applied
    // Workflow. We don't need to do anything here on success.
  }, [wfId])
  const handleCancel = useCallback(async () => {
    const sid = useBuilderChatStore.getState().sessionId
    if (sid && wfId) {
      await dispatchBuilderCancel(wfId)
    }
  }, [wfId])
  if (role === 'user') {
    return (
      <MessagePrimitive.Root>
        <ChatRoleContext.Provider value="user">
          <div className="flex justify-end mb-3">
            <div className="max-w-[85%] min-w-0 rounded-lg bg-accent px-3 py-2 text-sm text-white shadow-sm break-words overflow-wrap-anywhere">
              <ChatMessageParts />
            </div>
          </div>
        </ChatRoleContext.Provider>
      </MessagePrimitive.Root>
    )
  }
  // Assistant messages render a column of parts (text, tool_call,
  // tool_result, diff, etc). The Text part adds its own bubble via
  // `MarkdownText`; the tool / diff / completion / error parts each
  // already own their own chrome, so they sit alongside the bubble
  // rather than inside it. Each part owns its own bottom margin so
  // the spacing inside the column stays tight where parts are
  // related (thinking→text, call→result) and loose between
  // unrelated blocks.
  return (
    <MessagePrimitive.Root>
      <ChatRoleContext.Provider value="assistant">
        <div className="flex justify-start mb-4 w-full">
          <div className="max-w-[90%] min-w-0 flex flex-col">
            <ChatMessageParts onApplyDiff={handleApply} onCancelDiff={handleCancel} />
          </div>
        </div>
      </ChatRoleContext.Provider>
    </MessagePrimitive.Root>
  )
}

// ───────────────────────────────────────────────────────────────
// Helpers
// ───────────────────────────────────────────────────────────────

function useChatOrBuilderMessageCount(): number {
  const chatCount = useChatRunStore((s) => s.messages.length)
  const builderCount = useBuilderChatStore((s) => s.messages.length)
  return chatCount + builderCount
}

function useEffectScrollToBottom(
  ref: MutableRefObject<HTMLDivElement | null>,
  count: number,
) {
  // Lightweight re-scroll: whenever the message count changes,
  // jump to the bottom. We don't track content height — most chat
  // updates append, so jumping to `scrollHeight` is what we want.
  useEffect(() => {
    ref.current?.scrollTo({ top: ref.current.scrollHeight })
  }, [count, ref])
}

// ───────────────────────────────────────────────────────────────
// Inline icons — kept local to the chat surface because the rest
// of the app uses its own icon set (`UI/Icons.tsx`). A chat-bubble
// and a small trash glyph are chat-specific affordances that
// don't belong in the shared icon file.
// ───────────────────────────────────────────────────────────────

function TrashIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M3 6h18" />
      <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
      <path d="M10 11v6" />
      <path d="M14 11v6" />
    </svg>
  )
}

function ClipboardIcon() {
  // Two stacked rectangles with a clip on top — matches the
  // standard "copy" / "clipboard" outline used in most UI kits.
  // Drawn at 14×14 to match TrashIcon.
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="8" y="2" width="8" height="4" rx="1" />
      <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
      <path d="M9 12h6" />
      <path d="M9 16h4" />
    </svg>
  )
}

function CheckIcon() {
  // Single-check outline — shown after a successful copy to give
  // the user positive feedback that the clipboard write landed.
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M4 12.5l5 5L20 6.5" />
    </svg>
  )
}

function SendArrowIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M22 2L11 13" />
      <path d="M22 2l-7 20-4-9-9-4 20-7z" />
    </svg>
  )
}

function StopSquareIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
    >
      <rect x="5" y="5" width="14" height="14" rx="2" />
    </svg>
  )
}

function BoltIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="currentColor"
      stroke="none"
      aria-hidden="true"
    >
      <path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z" />
    </svg>
  )
}

function PlayIcon({ size = 12 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="currentColor"
      stroke="none"
      aria-hidden="true"
    >
      <path d="M6 4l14 8-14 8V4z" />
    </svg>
  )
}

// WrenchIcon is the build-mode counterpart to PlayIcon. The header
// uses a stroke-only wrench glyph so the two modes are visually
// distinct at a glance — a filled play triangle for the "go" action
// and an outlined wrench for the "configure / design" action. Shares
// the same path geometry as the WrenchIcon in ChatMessageParts so
// the user sees one consistent icon whether the wrench appears in
// the header or inside a tool-call row.
function WrenchIcon({ size = 14 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
    </svg>
  )
}

function ChevronLeftIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M15 18l-6-6 6-6" />
    </svg>
  )
}