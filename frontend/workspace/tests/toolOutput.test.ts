import { describe, expect, it } from 'vitest';
import { displayParts, toolImageSource, toolImages, toolOutputValue, type ToolState } from '../src/lib/toolOutput';

const PNG = 'iVBORw0KGgo=';

function tool(patch: Partial<ToolState>): ToolState {
  return {
    id: 't',
    title: null,
    kind: null,
    status: null,
    hasRawInput: false,
    rawInput: undefined,
    hasRawOutput: false,
    rawOutput: undefined,
    hasContent: false,
    content: undefined,
    ...patch,
  };
}

describe('tool output', () => {
  it('reads MCP and Claude image blocks', () => {
    expect(toolImageSource({ type: 'image', mimeType: 'image/png', data: PNG })).toBe(
      'data:image/png;base64,' + PNG
    );
    expect(
      toolImageSource({ type: 'image', source: { type: 'base64', media_type: 'image/jpeg', data: PNG } })
    ).toBe('data:image/jpeg;base64,' + PNG);
  });

  it('rejects images it should not render', () => {
    expect(toolImageSource({ type: 'image', mimeType: 'image/svg+xml', data: PNG })).toBeNull();
    expect(toolImageSource({ type: 'image', mimeType: 'image/png', data: 'not base64!' })).toBeNull();
    expect(toolImageSource({ type: 'image', source: { type: 'url', media_type: 'image/png', data: PNG } })).toBeNull();
  });

  it('finds an image serialized into a text block', () => {
    const serialized = JSON.stringify([{ type: 'image', mimeType: 'image/png', data: PNG }]);
    const t = tool({ hasContent: true, content: [{ type: 'content', content: { type: 'text', text: serialized } }] });
    expect(toolImages(t)).toEqual(['data:image/png;base64,' + PNG]);
  });

  it('shows ordinary output exactly as supplied', () => {
    const output = [{ type: 'text', text: 'hello' }];
    expect(displayParts(output)).toEqual([{ value: output }]);
    const single = tool({ hasContent: true, content: [{ type: 'content', content: { type: 'text', text: 'hi' } }] });
    expect(toolOutputValue(single)).toBe('hi');
  });

  it('prefers raw output over content', () => {
    expect(toolOutputValue(tool({ hasRawOutput: true, rawOutput: 'raw', hasContent: true, content: [] }))).toBe('raw');
    expect(toolOutputValue(tool({}))).toBeUndefined();
  });
});
