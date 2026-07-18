/**
 * ChatRoleContext — bridges `MessageBubbleForRole` (which knows the
 * current message's role) with `MarkdownText` (which renders inside
 * `MessagePrimitive.Parts` and therefore can't see the role directly).
 *
 * `MessageBubbleForRole` is the top-level per-message wrapper. It
 * sets the role via this context; the Text slot renderer reads it to
 * decide whether to add its own bubble chrome. We do this so:
 *   - user messages get exactly one bubble (the accent fill in
 *     `MessageBubbleForRole`); the inner Text renderer doesn't stack
 *     a second bubble on top.
 *   - assistant messages get a bubble around the LLM prose itself,
 *     so the text reads as a discrete unit instead of floating naked
 *     text alongside the tool-call / diff cards.
 *
 * Default is `assistant` (the most common path); `MessageBubbleForRole`
 * always sets the explicit value for both roles so the default never
 * actually fires in production.
 */
import { createContext } from 'react'

export type ChatRole = 'user' | 'assistant'

export const ChatRoleContext = createContext<ChatRole>('assistant')