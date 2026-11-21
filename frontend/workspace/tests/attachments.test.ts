import { describe, expect, it } from 'vitest';
import { attachmentFromContent, buildPromptBlocks, type PendingAttachment } from '../src/lib/attachments';

function pending(name: string, mimeType: string, size: number): PendingAttachment {
  return {
    id: 'id-' + name,
    name,
    mimeType,
    size,
    uri: 'file:///w/' + name,
    path: '/w/' + name,
    file: {} as File,
    state: 'ready',
    progress: 100,
    error: '',
    removed: false,
  };
}

const read = async () => 'data:image/png;base64,AAAA';

describe('prompt blocks', () => {
  it('puts the context first, then the text, then each file', async () => {
    const blocks = await buildPromptBlocks('Make it blue', [pending('a.pdf', 'application/pdf', 10)], {
      acceptsImages: true,
      context: { type: 'text', text: 'ctx' },
      read,
    });
    expect(blocks.map((b) => b.type)).toEqual(['text', 'text', 'resource_link']);
    expect(blocks[0].text).toBe('ctx');
    expect(blocks[1].text).toBe('Make it blue');
  });

  it('inlines images only when the agent accepts them, within the size budget', async () => {
    const small = pending('s.png', 'image/png', 1024);
    const huge = pending('h.png', 'image/png', 6 * 1024 * 1024);
    const accepted = await buildPromptBlocks('x', [small, huge], { acceptsImages: true, read });
    expect(accepted.map((b) => b.type)).toEqual(['text', 'resource_link', 'image', 'resource_link']);
    const refused = await buildPromptBlocks('x', [small], { acceptsImages: false, read });
    expect(refused.map((b) => b.type)).toEqual(['text', 'resource_link']);
  });

  it('falls back to the link when the file cannot be read', async () => {
    const blocks = await buildPromptBlocks('x', [pending('s.png', 'image/png', 1)], {
      acceptsImages: true,
      read: async () => {
        throw new Error('gone');
      },
    });
    expect(blocks.map((b) => b.type)).toEqual(['text', 'resource_link']);
  });

  it('recognizes attachments in replayed messages', () => {
    expect(
      attachmentFromContent({ type: 'resource_link', uri: 'file:///w/My%20File.pdf', mimeType: 'application/pdf' })
    ).toMatchObject({ name: 'My File.pdf', mimeType: 'application/pdf' });
    expect(attachmentFromContent({ type: 'text', text: 'hi' })).toBeNull();
  });
});
