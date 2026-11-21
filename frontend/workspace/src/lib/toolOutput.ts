// Tool-call payloads: what the agent sent and what came back, including
// screenshots. Ported from the dashboard chat (static/chat.js) so both chats
// read ACP tool results the same way.

export interface ToolState {
  id: string;
  title: string | null;
  kind: string | null;
  status: string | null;
  hasRawInput: boolean;
  rawInput: unknown;
  hasRawOutput: boolean;
  rawOutput: unknown;
  hasContent: boolean;
  content: unknown;
}

export interface OutputPart {
  value: unknown;
  /** A data: URL when this part is an image the browser can show. */
  src?: string;
}

export function formatToolValue(value: unknown): string {
  if (typeof value === 'string') return value;
  try {
    const json = JSON.stringify(value, null, 2);
    return json === undefined ? String(value) : json;
  } catch {
    return String(value);
  }
}

export function toolOutputValue(tool: ToolState): unknown {
  if (tool.hasRawOutput) return tool.rawOutput;
  if (!tool.hasContent) return undefined;
  const content = tool.content;
  if (Array.isArray(content) && content.length === 1) {
    const item = content[0];
    if (item && item.type === 'content' && item.content && item.content.type === 'text') {
      return item.content.text || '';
    }
  }
  return content;
}

type Block = Record<string, unknown>;

export function toolImageSource(value: unknown): string | null {
  if (!value || typeof value !== 'object') return null;
  const block = value as Block;
  if (block.type !== 'image') return null;
  // MCP/ACP image blocks and Claude's native base64 image blocks.
  const source = block.source as Block | undefined;
  const mime = source ? source.media_type : block.mimeType;
  const raw = source ? source.data : block.data;
  if (source && source.type !== 'base64') return null;
  if (typeof mime !== 'string' || !/^image\/(png|jpeg|gif|webp)$/.test(mime)) return null;
  if (typeof raw !== 'string') return null;
  const data = raw.replace(/\s/g, '');
  if (!data || data.length % 4 === 1 || !/^[A-Za-z0-9+/]*={0,2}$/.test(data)) return null;
  return 'data:' + mime + ';base64,' + data;
}

// An image can only hide inside a serialized block if the literal type shows
// up in the text, so big results that cannot hold one are never parsed.
function mayHoldImageBlock(text: string): boolean {
  return text.indexOf('"image"') !== -1;
}

function collect(parts: OutputPart[], value: unknown, depth: number): void {
  if (depth > 8) {
    parts.push({ value });
    return;
  }
  if (typeof value === 'string') {
    // Some adapters serialize the content array into a text block.
    if (/^\s*[[{]/.test(value) && mayHoldImageBlock(value)) {
      try {
        collect(parts, JSON.parse(value), depth + 1);
        return;
      } catch {
        /* plain text */
      }
    }
    parts.push({ value });
    return;
  }
  if (!value || typeof value !== 'object') {
    parts.push({ value });
    return;
  }
  const src = toolImageSource(value);
  if (src) {
    parts.push({ src, value });
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) collect(parts, item, depth + 1);
    return;
  }
  const block = value as Block;
  if (block.type === 'text' && typeof block.text === 'string') {
    collect(parts, block.text, depth + 1);
    return;
  }
  if (block.type === 'content' || Array.isArray(block.content)) {
    collect(parts, block.content, depth + 1);
    const metadata: Block = {};
    for (const key of Object.keys(block)) {
      if (key !== 'content' && !(key === 'type' && block.type === 'content')) {
        metadata[key] = block[key];
      }
    }
    if (Object.keys(metadata).length) parts.push({ value: metadata });
    return;
  }
  parts.push({ value });
}

export function toolOutputParts(value: unknown): OutputPart[] {
  const parts: OutputPart[] = [];
  collect(parts, value, 0);
  return parts;
}

/** How to show a tool's output: as-is, unless it carries images. */
export function displayParts(output: unknown): OutputPart[] {
  const parts = toolOutputParts(output);
  return parts.some((part) => !!part.src) ? parts : [{ value: output }];
}

// Outputs are re-scanned whenever a turn changes; a structured result is only
// walked once (a status-only update keeps the same output object).
const imageCache = new WeakMap<object, string[]>();

/** Every image the tool returned, in order. */
export function toolImages(tool: ToolState): string[] {
  const output = toolOutputValue(tool);
  if (output === undefined || output === null) return [];
  if (typeof output === 'string' && !mayHoldImageBlock(output)) return [];
  const cached = typeof output === 'object' ? imageCache.get(output) : undefined;
  if (cached) return cached;
  const images: string[] = [];
  for (const part of toolOutputParts(output)) {
    if (part.src) images.push(part.src);
  }
  if (typeof output === 'object') imageCache.set(output, images);
  return images;
}

export function toolStatusText(status: string | null): string {
  if (status === 'completed') return 'done';
  if (status === 'failed') return 'failed';
  if (status === 'in_progress') return 'running';
  return 'pending';
}
