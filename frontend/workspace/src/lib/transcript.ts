// The conversation as data: ACP session/update notifications folded into a
// list of items the components render. Plain functions over a plain object,
// so they work the same on Svelte state (the app) and in unit tests.
//
// Shape, matching the dashboard chat (static/chat.js):
//  - a user item per message the user sent (or replayed from history);
//  - an agent turn per reply. A turn collects everything the agent did in
//    order: messages, reasoning, tool calls, the plan. Only the reply that
//    answers the user is shown in full; the rest is folded into the turn's
//    activity (see splitTurn).

import { attachmentFromContent, attachmentKey, contentText, type Attachment } from './attachments';
import { toolImages, type ToolState } from './toolOutput';
import { hasWorkspaceContext } from './workspaceContext';

export interface PlanEntry {
  content: string;
  status: string;
}

export type Entry =
  | { type: 'message'; id: number; text: string }
  | { type: 'thought'; id: number; text: string }
  | { type: 'tool'; id: number; toolId: string }
  | { type: 'plan'; id: number; entries: PlanEntry[] };

export interface UserItem {
  kind: 'user';
  id: number;
  text: string;
  attachments: Attachment[];
  /** Still receiving replayed chunks. */
  open: boolean;
}

export interface TurnItem {
  kind: 'turn';
  id: number;
  entries: Entry[];
  done: boolean;
}

export interface SystemItem {
  kind: 'system';
  id: number;
  text: string;
  tone: 'info' | 'error';
}

export type Item = UserItem | TurnItem | SystemItem;

export interface Transcript {
  items: Item[];
  tools: Record<string, ToolState>;
  nextId: number;
  /** A replayed user message carried the workspace context block. */
  sawContext: boolean;
}

export function newTranscript(): Transcript {
  return { items: [], tools: {}, nextId: 1, sawContext: false };
}

export function resetTranscript(t: Transcript): void {
  t.items = [];
  t.tools = {};
  t.sawContext = false;
}

function lastItem(t: Transcript): Item | undefined {
  return t.items[t.items.length - 1];
}

function openUser(t: Transcript): UserItem | null {
  const last = lastItem(t);
  return last && last.kind === 'user' && last.open ? last : null;
}

export function currentTurn(t: Transcript): TurnItem | null {
  const last = lastItem(t);
  return last && last.kind === 'turn' && !last.done ? last : null;
}

function closeUser(t: Transcript): void {
  const user = openUser(t);
  if (user) user.open = false;
}

export function finalizeTurn(t: Transcript): void {
  const turn = currentTurn(t);
  if (turn) turn.done = true;
  closeUser(t);
}

function ensureTurn(t: Transcript): TurnItem {
  closeUser(t);
  const turn = currentTurn(t);
  if (turn) return turn;
  t.items.push({ kind: 'turn', id: t.nextId++, entries: [], done: false });
  // Read back through the list: on Svelte state this is the reactive proxy.
  return t.items[t.items.length - 1] as TurnItem;
}

export function addUserMessage(t: Transcript, text: string, attachments: Attachment[]): void {
  finalizeTurn(t);
  t.items.push({ kind: 'user', id: t.nextId++, text, attachments, open: false });
}

function ensureUser(t: Transcript): UserItem {
  const open = openUser(t);
  if (open) return open;
  finalizeTurn(t);
  t.items.push({ kind: 'user', id: t.nextId++, text: '', attachments: [], open: true });
  return t.items[t.items.length - 1] as UserItem;
}

/** A replayed piece of a user message: text, or an attached file. */
export function appendUserContent(t: Transcript, content: unknown): void {
  const text = contentText(content);
  if (text) {
    const user = ensureUser(t);
    user.text += text;
    if (hasWorkspaceContext(user.text)) t.sawContext = true;
    return;
  }
  const attachment = attachmentFromContent(content);
  if (!attachment) return;
  const user = ensureUser(t);
  const key = attachmentKey(attachment);
  if (user.attachments.some((existing) => attachmentKey(existing) === key)) return;
  user.attachments.push(attachment);
}

function appendStream(t: Transcript, type: 'message' | 'thought', text: string): void {
  if (!text) return;
  const turn = ensureTurn(t);
  const last = turn.entries[turn.entries.length - 1];
  if (last && last.type === type) {
    last.text += text;
  } else {
    turn.entries.push({ type, id: t.nextId++, text });
  }
}

export function appendAgentText(t: Transcript, text: string): void {
  appendStream(t, 'message', text);
}

export function appendThought(t: Transcript, text: string): void {
  appendStream(t, 'thought', text);
}

function hasOwn(obj: object, name: string): boolean {
  return Object.prototype.hasOwnProperty.call(obj, name);
}

export interface ToolUpdate {
  toolCallId: string;
  title?: string | null;
  kind?: string | null;
  status?: string | null;
  rawInput?: unknown;
  rawOutput?: unknown;
  content?: unknown;
}

function patchTool(tool: ToolState, u: ToolUpdate): void {
  if (u.title != null) tool.title = u.title;
  if (u.kind != null) tool.kind = u.kind;
  if (u.status != null) tool.status = u.status;
  if (hasOwn(u, 'rawInput')) {
    tool.hasRawInput = true;
    tool.rawInput = u.rawInput;
  }
  if (hasOwn(u, 'rawOutput')) {
    tool.hasRawOutput = true;
    tool.rawOutput = u.rawOutput;
  }
  if (hasOwn(u, 'content')) {
    tool.hasContent = true;
    tool.content = u.content;
  }
}

/** A tool_call or tool_call_update: create the call or patch it in place. */
export function upsertTool(t: Transcript, u: ToolUpdate): ToolState {
  const existing = t.tools[u.toolCallId];
  if (existing) {
    patchTool(existing, u);
    return existing;
  }
  const turn = ensureTurn(t);
  t.tools[u.toolCallId] = {
    id: u.toolCallId,
    title: null,
    kind: null,
    status: null,
    hasRawInput: false,
    rawInput: undefined,
    hasRawOutput: false,
    rawOutput: undefined,
    hasContent: false,
    content: undefined,
  };
  const tool = t.tools[u.toolCallId];
  patchTool(tool, u);
  turn.entries.push({ type: 'tool', id: t.nextId++, toolId: u.toolCallId });
  return tool;
}

interface RawPlanEntry {
  content?: unknown;
  status?: string;
}

/** The agent's plan: one per turn, replaced as it progresses. */
export function setPlan(t: Transcript, raw: RawPlanEntry[]): void {
  const entries = raw.map((entry) => ({
    content: contentText(entry.content) || (typeof entry.content === 'string' ? entry.content : ''),
    status: entry.status || 'pending',
  }));
  const turn = ensureTurn(t);
  const plan = turn.entries.find((entry) => entry.type === 'plan');
  if (plan && plan.type === 'plan') {
    plan.entries = entries;
  } else {
    turn.entries.push({ type: 'plan', id: t.nextId++, entries });
  }
}

export function addSystem(t: Transcript, text: string, tone: 'info' | 'error' = 'info'): void {
  if (!text) return;
  t.items.push({ kind: 'system', id: t.nextId++, text, tone });
}

export interface SessionUpdate {
  sessionUpdate?: string;
  content?: unknown;
  entries?: RawPlanEntry[];
  plan?: RawPlanEntry[];
  toolCallId?: string;
  [key: string]: unknown;
}

/** Fold one ACP session/update notification into the transcript. */
export function applyUpdate(t: Transcript, u: SessionUpdate | undefined): void {
  if (!u) return;
  switch (u.sessionUpdate) {
    case 'user_message_chunk':
      appendUserContent(t, u.content);
      break;
    case 'agent_message_chunk':
      appendAgentText(t, contentText(u.content));
      break;
    case 'agent_thought_chunk':
      appendThought(t, contentText(u.content));
      break;
    case 'tool_call':
    case 'tool_call_update':
      if (u.toolCallId) upsertTool(t, u as unknown as ToolUpdate);
      break;
    case 'plan':
      setPlan(t, u.entries || u.plan || []);
      break;
    default:
      break; // available_commands_update, current_mode_update, ...
  }
}

export interface TurnView {
  /** Everything folded away: tool calls, reasoning, the plan, interim notes. */
  activity: Entry[];
  /** The reply shown in full. */
  answer: Extract<Entry, { type: 'message' }> | null;
}

/**
 * While the agent works, the message it is writing right now is the answer
 * and everything before it is activity. Once the turn is over, the last
 * message it wrote is the answer, even if tool calls followed it.
 */
export function splitTurn(turn: TurnItem): TurnView {
  let index = -1;
  if (turn.done) {
    for (let i = turn.entries.length - 1; i >= 0; i--) {
      if (turn.entries[i].type === 'message') {
        index = i;
        break;
      }
    }
  } else if (turn.entries.length && turn.entries[turn.entries.length - 1].type === 'message') {
    index = turn.entries.length - 1;
  }
  if (index === -1) return { activity: turn.entries, answer: null };
  return {
    activity: turn.entries.filter((_, i) => i !== index),
    answer: turn.entries[index] as Extract<Entry, { type: 'message' }>,
  };
}

export interface ActivitySummary {
  steps: number;
  notes: number;
  running: boolean;
  failed: number;
}

export function summarizeActivity(t: Transcript, entries: Entry[]): ActivitySummary {
  const summary: ActivitySummary = { steps: 0, notes: 0, running: false, failed: 0 };
  for (const entry of entries) {
    if (entry.type === 'message') summary.notes += 1;
    if (entry.type !== 'tool') continue;
    summary.steps += 1;
    const status = t.tools[entry.toolId]?.status;
    if (status === 'failed') summary.failed += 1;
    if (!status || status === 'pending' || status === 'in_progress') summary.running = true;
  }
  return summary;
}

/** The last image any tool returned in this turn (the agent's screenshot). */
export function latestTurnImage(t: Transcript, turn: TurnItem): string | null {
  for (let i = turn.entries.length - 1; i >= 0; i--) {
    const entry = turn.entries[i];
    if (entry.type !== 'tool') continue;
    const tool = t.tools[entry.toolId];
    if (!tool) continue;
    const images = toolImages(tool);
    if (images.length) return images[images.length - 1];
  }
  return null;
}

/** Whether anything but system notes has been said yet. */
export function isEmpty(t: Transcript): boolean {
  return !t.items.some((item) => item.kind !== 'system');
}
