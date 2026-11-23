// Files the user attaches to a prompt: how they are shown in the transcript and
// how they are sent to the agent. Uploads land in the environment's workspace
// resources first (POST .../agent-acp/attachments, see
// specs/0038-agent-chat-file-attachments.md); the prompt then links them.

export interface Attachment {
  name: string;
  uri?: string;
  path?: string;
  mimeType?: string;
  size?: number;
}

export type UploadState = 'queued' | 'uploading' | 'ready' | 'failed';

export interface PendingAttachment extends Attachment {
  /** Upload id assigned by the server; used to delete it again. */
  id?: string;
  file: File;
  state: UploadState;
  progress: number;
  error: string;
  removed: boolean;
}

export interface ContentBlock {
  type: string;
  [key: string]: unknown;
}

export const INLINE_IMAGE_MAX_BYTES = 5 * 1024 * 1024;

export function contentText(content: unknown): string {
  if (!content) return '';
  if (typeof content === 'string') return content;
  const block = content as ContentBlock;
  if (block.type === 'text') return (block.text as string) || '';
  return '';
}

export function fileNameFromUri(uri: unknown): string {
  const tail = String(uri || '').split('/').pop() || 'Attachment';
  try {
    return decodeURIComponent(tail);
  } catch {
    return tail;
  }
}

interface OduflowMeta {
  name?: string;
  path?: string;
  size?: number;
}

function oduflowMeta(block: ContentBlock): OduflowMeta | undefined {
  const meta = block._meta as { oduflow?: OduflowMeta } | undefined;
  return meta && meta.oduflow;
}

/** The attachment a replayed user content block refers to, if any. */
export function attachmentFromContent(content: unknown): Attachment | null {
  if (!content || typeof content !== 'object') return null;
  const block = content as ContentBlock;
  const meta = oduflowMeta(block);
  if (block.type === 'resource_link') {
    return {
      name: (block.name as string) || fileNameFromUri(block.uri),
      uri: (block.uri as string) || '',
      path: (meta && meta.path) || '',
      mimeType: (block.mimeType as string) || '',
      size: block.size as number | undefined,
    };
  }
  if (block.type === 'image' || block.type === 'audio') {
    return {
      name: (meta && meta.name) || fileNameFromUri(block.uri),
      uri: (block.uri as string) || '',
      path: (meta && meta.path) || '',
      mimeType: (block.mimeType as string) || '',
      size: meta && meta.size,
    };
  }
  if (block.type === 'resource' && block.resource) {
    const resource = block.resource as { uri?: string; mimeType?: string };
    return {
      name: (meta && meta.name) || fileNameFromUri(resource.uri),
      uri: resource.uri || '',
      path: (meta && meta.path) || '',
      mimeType: resource.mimeType || '',
      size: meta && meta.size,
    };
  }
  return null;
}

export function attachmentKey(attachment: Attachment): string {
  return attachment.uri || attachment.name + ' ' + attachment.mimeType;
}

export function resourceLinkBlock(attachment: PendingAttachment): ContentBlock {
  return {
    type: 'resource_link',
    name: attachment.name,
    uri: attachment.uri,
    mimeType: attachment.mimeType,
    size: attachment.size,
    _meta: { oduflow: { id: attachment.id, path: attachment.path } },
  };
}

export type FileReaderFn = (file: File) => Promise<string>;

export function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function attachmentBlocks(
  attachment: PendingAttachment,
  inlineImage: boolean,
  read: FileReaderFn
): Promise<ContentBlock[]> {
  const link = resourceLinkBlock(attachment);
  if (!inlineImage) return [link];
  // Keep the link even when the image bytes are inlined: ACP adapters commonly
  // drop image URI/_meta fields but keep resource links as text.
  let dataUrl: string;
  try {
    dataUrl = await read(attachment.file);
  } catch {
    return [link];
  }
  const comma = dataUrl.indexOf(',');
  if (comma === -1) return [link];
  return [
    link,
    {
      type: 'image',
      data: dataUrl.slice(comma + 1),
      mimeType: attachment.mimeType,
      uri: attachment.uri,
      _meta: {
        oduflow: {
          id: attachment.id,
          name: attachment.name,
          path: attachment.path,
          size: attachment.size,
        },
      },
    },
  ];
}

/**
 * The ACP prompt for one message: optional leading context, the user's text,
 * then each attachment as a resource link (plus inline image bytes when the
 * agent accepts images, up to INLINE_IMAGE_MAX_BYTES per prompt).
 */
export async function buildPromptBlocks(
  text: string,
  attachments: PendingAttachment[],
  options: { acceptsImages: boolean; context?: ContentBlock | null; read?: FileReaderFn }
): Promise<ContentBlock[]> {
  const read = options.read || readAsDataUrl;
  const groups: Promise<ContentBlock[]>[] = [];
  if (options.context) groups.push(Promise.resolve([options.context]));
  groups.push(Promise.resolve([{ type: 'text', text }]));
  let inlineBytes = 0;
  for (const attachment of attachments) {
    const size = attachment.size || 0;
    const inline =
      options.acceptsImages &&
      (attachment.mimeType || '').indexOf('image/') === 0 &&
      inlineBytes + size <= INLINE_IMAGE_MAX_BYTES;
    if (inline) inlineBytes += size;
    groups.push(attachmentBlocks(attachment, inline, read));
  }
  return (await Promise.all(groups)).flat();
}
