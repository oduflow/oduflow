import { describe, expect, it } from 'vitest';
import {
  addUserMessage,
  applyUpdate,
  finalizeTurn,
  isEmpty,
  latestTurnImage,
  newTranscript,
  splitTurn,
  summarizeActivity,
  type TurnItem,
} from '../src/lib/transcript';
import { CONTEXT_CLOSE, CONTEXT_OPEN } from '../src/lib/workspaceContext';

const PNG = 'iVBORw0KGgo=';

function text(t: string) {
  return { type: 'text', text: t };
}

function lastTurn(items: { kind: string }[]): TurnItem {
  return items[items.length - 1] as TurnItem;
}

describe('transcript', () => {
  it('streams the reply being written as the answer', () => {
    const t = newTranscript();
    addUserMessage(t, 'Add a field', []);
    applyUpdate(t, { sessionUpdate: 'agent_message_chunk', content: text('Sure, ') });
    applyUpdate(t, { sessionUpdate: 'agent_message_chunk', content: text('on it.') });
    const view = splitTurn(lastTurn(t.items));
    expect(view.answer?.text).toBe('Sure, on it.');
    expect(view.activity).toEqual([]);
  });

  it('folds a message followed by tool calls into the activity until the turn ends', () => {
    const t = newTranscript();
    addUserMessage(t, 'Do it', []);
    applyUpdate(t, { sessionUpdate: 'agent_message_chunk', content: text('Done. Here it is.') });
    applyUpdate(t, { sessionUpdate: 'tool_call', toolCallId: 'a', title: 'screenshot', status: 'pending' });
    const turn = lastTurn(t.items);
    expect(splitTurn(turn).answer).toBeNull();
    expect(splitTurn(turn).activity).toHaveLength(2);

    finalizeTurn(t);
    const done = splitTurn(turn);
    expect(done.answer?.text).toBe('Done. Here it is.');
    expect(done.activity.map((e) => e.type)).toEqual(['tool']);
  });

  it('patches tool calls in place and summarizes their state', () => {
    const t = newTranscript();
    addUserMessage(t, 'Go', []);
    applyUpdate(t, { sessionUpdate: 'tool_call', toolCallId: 'a', title: 'upgrade', status: 'in_progress' });
    applyUpdate(t, { sessionUpdate: 'tool_call', toolCallId: 'b', title: 'tests', status: 'pending' });
    applyUpdate(t, { sessionUpdate: 'tool_call_update', toolCallId: 'a', status: 'completed', rawOutput: 'ok' });
    applyUpdate(t, { sessionUpdate: 'tool_call_update', toolCallId: 'b', status: 'failed' });
    const turn = lastTurn(t.items);
    expect(turn.entries).toHaveLength(2);
    expect(t.tools.a.status).toBe('completed');
    expect(t.tools.a.title).toBe('upgrade');
    expect(t.tools.a.rawOutput).toBe('ok');
    expect(summarizeActivity(t, turn.entries)).toEqual({ steps: 2, notes: 0, running: false, failed: 1 });
  });

  it('keeps one plan per turn, replaced as it progresses', () => {
    const t = newTranscript();
    applyUpdate(t, { sessionUpdate: 'plan', entries: [{ content: 'Model', status: 'in_progress' }] });
    applyUpdate(t, {
      sessionUpdate: 'plan',
      entries: [
        { content: 'Model', status: 'completed' },
        { content: 'View', status: 'pending' },
      ],
    });
    const turn = lastTurn(t.items);
    expect(turn.entries).toHaveLength(1);
    const plan = turn.entries[0];
    expect(plan.type === 'plan' && plan.entries.map((e) => e.status)).toEqual(['completed', 'pending']);
  });

  it('merges replayed user chunks into one message and notices the workspace context', () => {
    const t = newTranscript();
    applyUpdate(t, {
      sessionUpdate: 'user_message_chunk',
      content: text(CONTEXT_OPEN + '\nrules\n' + CONTEXT_CLOSE + '\n\n'),
    });
    applyUpdate(t, { sessionUpdate: 'user_message_chunk', content: text('Add a field') });
    const link = { type: 'resource_link', name: 'shot.png', uri: 'file:///w/shot.png', mimeType: 'image/png' };
    applyUpdate(t, { sessionUpdate: 'user_message_chunk', content: link });
    applyUpdate(t, { sessionUpdate: 'user_message_chunk', content: link });
    applyUpdate(t, { sessionUpdate: 'agent_message_chunk', content: text('Done') });
    applyUpdate(t, { sessionUpdate: 'user_message_chunk', content: text('Thanks') });

    expect(t.sawContext).toBe(true);
    expect(t.items.map((i) => i.kind)).toEqual(['user', 'turn', 'user']);
    const first = t.items[0];
    expect(first.kind === 'user' && first.attachments).toHaveLength(1);
    expect(first.kind === 'user' && first.open).toBe(false);
    expect((t.items[1] as TurnItem).done).toBe(true);
  });

  it('lifts the last screenshot of a turn', () => {
    const t = newTranscript();
    applyUpdate(t, {
      sessionUpdate: 'tool_call',
      toolCallId: 'shot',
      status: 'completed',
      content: [{ type: 'content', content: { type: 'image', mimeType: 'image/png', data: PNG } }],
    });
    applyUpdate(t, { sessionUpdate: 'tool_call', toolCallId: 'read', status: 'completed', rawOutput: 'text' });
    expect(latestTurnImage(t, lastTurn(t.items))).toBe('data:image/png;base64,' + PNG);
  });

  it('is empty until something other than a system note appears', () => {
    const t = newTranscript();
    expect(isEmpty(t)).toBe(true);
    t.items.push({ kind: 'system', id: 1, text: 'note', tone: 'info' });
    expect(isEmpty(t)).toBe(true);
    addUserMessage(t, 'hi', []);
    expect(isEmpty(t)).toBe(false);
  });
});
