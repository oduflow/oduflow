import { describe, expect, it } from 'vitest';
import {
  CONTEXT_CLOSE,
  CONTEXT_OPEN,
  stripWorkspaceContext,
  workspaceContextBlock,
} from '../src/lib/workspaceContext';

describe('workspace context', () => {
  it('is fenced so it can be recognized', () => {
    const block = workspaceContextBlock();
    expect(block.type).toBe('text');
    expect(block.text.startsWith(CONTEXT_OPEN)).toBe(true);
    expect(block.text).toContain(CONTEXT_CLOSE);
  });

  it("is stripped from the user's message", () => {
    expect(stripWorkspaceContext(workspaceContextBlock().text + 'Add a field')).toBe('Add a field');
  });

  it('strips an unterminated block from a partial replay', () => {
    expect(stripWorkspaceContext(CONTEXT_OPEN + '\nhalf of the rules')).toBe('');
  });

  it('leaves ordinary text alone', () => {
    expect(stripWorkspaceContext('  keep  my spacing ')).toBe('  keep  my spacing ');
  });
});
