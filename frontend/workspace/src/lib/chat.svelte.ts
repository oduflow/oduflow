// One live conversation with the environment's coding agent over ACP.
//
// The lifecycle mirrors the dashboard chat (static/chat.js, see
// specs/0029-agent-console-and-chat.md): the stored session for this
// environment and agent is resumed through session/load (which replays the
// transcript), or a new one is opened and remembered server-side. Uploads go
// to the environment's workspace resources before the prompt links them.

import { createAcpClient, type AcpClient, type PermissionParams, type SessionResult } from './acp';
import {
  chatSocketUrl,
  deleteUpload,
  fetchChatInfo,
  saveSession,
  uploadFile,
  type HistoryEntry,
  type SessionState,
  type Upload,
} from './api';
import { buildPromptBlocks, type Attachment, type PendingAttachment } from './attachments';
import { formatBytes, titleFrom } from './format';
import { sessionModels, type Models } from './models';
import {
  addSystem,
  addUserMessage,
  applyUpdate,
  finalizeTurn,
  newTranscript,
  resetTranscript,
  type SessionUpdate,
} from './transcript';
import { workspaceContextBlock } from './workspaceContext';

export type ChatStatus = 'connecting' | 'ready' | 'thinking' | 'permission' | 'error' | 'closed';

export const AGENT_LABELS: Record<string, string> = {
  claude: 'Claude',
  codex: 'Codex',
  opencode: 'OpenCode',
};

const DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024;
const DEFAULT_MAX_FILES = 5;

export interface PermissionRequest {
  requestId: number | string;
  title: string;
  options: { optionId: string; name: string; allow: boolean }[];
}

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export class ChatSession {
  status = $state<ChatStatus>('connecting');
  busy = $state(false);
  transcript = $state(newTranscript());
  history = $state<HistoryEntry[]>([]);
  models = $state<Models | null>(null);
  permissions = $state<PermissionRequest[]>([]);
  attachments = $state<PendingAttachment[]>([]);
  composerError = $state('');
  sessionId = $state<string | null>(null);
  maxFiles = $state(DEFAULT_MAX_FILES);

  readonly envName: string;
  readonly agentType: string;
  private client: AcpClient | null = null;
  // Reactive: canAttach reads it first, so it must be tracked.
  private cwd = $state('');
  private maxFileBytes = DEFAULT_MAX_FILE_BYTES;
  private acceptsImages = false;
  private titled = false;
  // The standing workspace instructions go with the first prompt of a
  // conversation that does not carry them yet.
  private needsContext = true;
  private uploads = new Map<PendingAttachment, Upload>();
  private uploading: PendingAttachment | null = null;

  constructor(envName: string, agentType: string) {
    this.envName = envName;
    this.agentType = agentType;
  }

  get agentLabel(): string {
    return AGENT_LABELS[this.agentType] || this.agentType;
  }

  get canSend(): boolean {
    return (
      !this.busy &&
      !!this.sessionId &&
      this.status !== 'closed' &&
      this.attachments.every((a) => a.state === 'ready')
    );
  }

  get canAttach(): boolean {
    return (
      !!this.cwd &&
      !!this.sessionId &&
      this.status !== 'connecting' &&
      this.status !== 'closed' &&
      this.attachments.length < this.maxFiles
    );
  }

  // -- lifecycle --------------------------------------------------------------

  async start(): Promise<void> {
    this.status = 'connecting';
    try {
      const info = await fetchChatInfo(this.envName, this.agentType);
      this.cwd = info.cwd;
      const limits = info.attachment_limits || {};
      this.maxFileBytes = limits.max_file_bytes || DEFAULT_MAX_FILE_BYTES;
      this.maxFiles = limits.max_files || DEFAULT_MAX_FILES;
      this.history = Array.isArray(info.history) ? info.history : [];
      this.titled = !!this.history.find((h) => h.session_id === info.session_id)?.title;

      const client = createAcpClient(chatSocketUrl(this.envName, this.agentType));
      this.client = client;
      client.on('update', (p) => applyUpdate(this.transcript, p.update as SessionUpdate));
      client.on('permission', (p) => this.onPermission(p.requestId, p.params));
      client.on('error', (message) => addSystem(this.transcript, message, 'error'));
      client.on('notice', (message) => addSystem(this.transcript, message));
      client.on('close', () => {
        if (this.client !== client) return;
        this.status = 'closed';
        this.busy = false;
        this.permissions = [];
      });
      await client.connect();
      const init = await client.initialize();
      this.acceptsImages = !!init?.agentCapabilities?.promptCapabilities?.image;

      if (info.session_id) {
        try {
          const res = await client.loadSession(info.session_id, this.cwd);
          finalizeTurn(this.transcript);
          this.sessionId = info.session_id;
          this.needsContext = !this.transcript.sawContext;
          this.applyModels(res);
        } catch {
          // A stale or invalid stored session: start fresh.
          resetTranscript(this.transcript);
          await this.openNewSession();
        }
      } else {
        await this.openNewSession();
      }
      this.status = 'ready';
    } catch (e) {
      addSystem(this.transcript, errorText(e), 'error');
      this.status = 'error';
    }
  }

  /** Drop the connection and resume the same conversation on a fresh one. */
  async reconnect(): Promise<void> {
    this.closeClient();
    resetTranscript(this.transcript);
    this.permissions = [];
    this.busy = false;
    await this.start();
  }

  dispose(): void {
    this.clearAttachments(true);
    this.closeClient();
  }

  private closeClient(): void {
    const client = this.client;
    this.client = null;
    if (client) {
      try {
        client.close();
      } catch {
        /* already closed */
      }
    }
  }

  private async openNewSession(): Promise<void> {
    const res = await this.requireClient().newSession(this.cwd);
    this.sessionId = res.sessionId || null;
    this.titled = false;
    this.needsContext = true;
    this.applyModels(res);
    if (this.sessionId) {
      this.refreshHistory(await saveSession(this.envName, this.agentType, this.sessionId));
    }
  }

  private requireClient(): AcpClient {
    if (!this.client || this.client.closed) {
      throw new Error('The connection to the agent is closed. Reconnect to continue.');
    }
    return this.client;
  }

  private refreshHistory(state: SessionState | null): void {
    if (state && Array.isArray(state.history)) this.history = state.history;
    this.titled = !!this.history.find((h) => h.session_id === this.sessionId)?.title;
  }

  private applyModels(res: SessionResult | undefined): void {
    const models = sessionModels(res);
    if (models) this.models = models;
  }

  // -- conversation -----------------------------------------------------------

  /** Validate and send a message; false (with composerError set) if refused. */
  send(rawText: string): boolean {
    const text = rawText.trim();
    if (this.busy || !this.sessionId || !this.client) return false;
    if (!text) {
      this.composerError = this.attachments.length
        ? 'Describe what the agent should do with the attached files.'
        : 'Write what you would like to change.';
      return false;
    }
    if (this.attachments.some((a) => a.state === 'failed')) {
      this.composerError = 'Remove failed attachments before sending.';
      return false;
    }
    if (this.attachments.some((a) => a.state !== 'ready')) {
      this.composerError = 'Wait for attachments to finish uploading.';
      return false;
    }

    const attachments = this.attachments.slice();
    const shown: Attachment[] = attachments.map((a) => ({
      name: a.name,
      uri: a.uri,
      path: a.path,
      mimeType: a.mimeType,
      size: a.size,
    }));
    addUserMessage(this.transcript, text, shown);
    this.attachments = [];
    this.composerError = '';
    this.busy = true;
    this.status = 'thinking';

    const sessionId = this.sessionId;
    const context = this.needsContext ? workspaceContextBlock() : null;
    this.needsContext = false;
    if (!this.titled) {
      this.titled = true;
      saveSession(this.envName, this.agentType, sessionId, titleFrom(text)).then((state) =>
        this.refreshHistory(state)
      );
    }
    void this.prompt(sessionId, text, attachments, context);
    return true;
  }

  private async prompt(
    sessionId: string,
    text: string,
    attachments: PendingAttachment[],
    context: ReturnType<typeof workspaceContextBlock> | null
  ): Promise<void> {
    try {
      const blocks = await buildPromptBlocks(text, attachments, {
        acceptsImages: this.acceptsImages,
        context,
      });
      await this.requireClient().prompt(sessionId, blocks);
    } catch (e) {
      addSystem(this.transcript, errorText(e), 'error');
    } finally {
      finalizeTurn(this.transcript);
      this.busy = false;
      if (this.status !== 'closed') this.status = 'ready';
    }
  }

  stop(): void {
    if (!this.client || !this.sessionId) return;
    // A cancelled turn must not leave the agent waiting on an approval.
    for (const request of this.permissions) this.client.respondPermission(request.requestId, null);
    this.permissions = [];
    this.client.cancel(this.sessionId);
    this.busy = false;
    if (this.status !== 'closed') this.status = 'ready';
  }

  async newConversation(): Promise<void> {
    if (this.busy) {
      addSystem(this.transcript, 'Stop the current reply before starting a new conversation.');
      return;
    }
    this.status = 'connecting';
    try {
      this.clearAttachments(true);
      resetTranscript(this.transcript);
      await this.openNewSession();
      this.status = 'ready';
    } catch (e) {
      addSystem(this.transcript, errorText(e), 'error');
      this.status = this.client && !this.client.closed ? 'error' : 'closed';
    }
  }

  async switchTo(sessionId: string): Promise<void> {
    if (sessionId === this.sessionId) return;
    if (this.busy) {
      addSystem(this.transcript, 'Stop the current reply before switching conversations.');
      return;
    }
    const previous = this.sessionId;
    this.status = 'connecting';
    this.clearAttachments(true);
    resetTranscript(this.transcript);
    try {
      const client = this.requireClient();
      try {
        const res = await client.loadSession(sessionId, this.cwd);
        this.adoptLoaded(sessionId, res);
      } catch {
        resetTranscript(this.transcript);
        if (!previous) throw new Error('fallback');
        const res = await client.loadSession(previous, this.cwd);
        this.adoptLoaded(previous, res);
        addSystem(this.transcript, 'Could not open that conversation. Restored the current one.');
      }
      this.status = 'ready';
      this.refreshHistory(await saveSession(this.envName, this.agentType, this.sessionId!));
    } catch {
      try {
        resetTranscript(this.transcript);
        await this.openNewSession();
        addSystem(this.transcript, 'Could not restore the conversation. Started a new one.');
        this.status = 'ready';
      } catch (e) {
        addSystem(this.transcript, errorText(e), 'error');
        this.status = this.client && !this.client.closed ? 'error' : 'closed';
      }
    }
  }

  private adoptLoaded(sessionId: string, res: SessionResult): void {
    finalizeTurn(this.transcript);
    this.sessionId = sessionId;
    this.needsContext = !this.transcript.sawContext;
    this.applyModels(res);
  }

  async setModel(modelId: string): Promise<void> {
    if (!this.client || !this.sessionId) return;
    try {
      this.applyModels(await this.client.setModel(this.sessionId, modelId));
      if (this.models) this.models.currentModelId = modelId;
    } catch (e) {
      addSystem(this.transcript, 'Could not switch the model: ' + errorText(e), 'error');
    }
  }

  // -- approvals --------------------------------------------------------------

  private onPermission(requestId: number | string, params: PermissionParams): void {
    const options = (params.options || []).map((option) => ({
      optionId: option.optionId,
      name: option.name || option.optionId,
      allow: !!option.kind && option.kind.indexOf('allow') === 0,
    }));
    this.permissions.push({
      requestId,
      title: (params.toolCall && params.toolCall.title) || 'an action',
      options,
    });
    this.status = 'permission';
  }

  respond(request: PermissionRequest, optionId: string | null): void {
    if (this.client) this.client.respondPermission(request.requestId, optionId);
    this.permissions = this.permissions.filter((r) => r.requestId !== request.requestId);
    if (!this.permissions.length && this.status === 'permission') {
      this.status = this.busy ? 'thinking' : 'ready';
    }
  }

  // -- attachments ------------------------------------------------------------

  addFiles(files: ArrayLike<File>): void {
    const room = this.maxFiles - this.attachments.length;
    if (room <= 0) {
      this.composerError = 'You can attach up to ' + this.maxFiles + ' files.';
      return;
    }
    const count = Math.min(files.length, room);
    for (let i = 0; i < count; i++) {
      const file = files[i];
      const tooBig = file.size > this.maxFileBytes;
      this.attachments.push({
        name: file.name || 'attachment',
        mimeType: file.type || 'application/octet-stream',
        size: file.size,
        file,
        state: tooBig ? 'failed' : 'queued',
        progress: 0,
        error: tooBig ? 'Larger than the ' + formatBytes(this.maxFileBytes) + ' limit' : '',
        removed: false,
      });
    }
    this.composerError =
      files.length > count ? 'You can attach up to ' + this.maxFiles + ' files.' : '';
    this.pumpUploads();
  }

  removeAttachment(attachment: PendingAttachment): void {
    attachment.removed = true;
    this.attachments = this.attachments.filter((a) => a !== attachment);
    this.uploads.get(attachment)?.abort();
    if (attachment.state === 'ready' && attachment.id) deleteUpload(this.envName, attachment.id);
    this.composerError = '';
    this.pumpUploads();
  }

  private clearAttachments(removeRemote: boolean): void {
    for (const attachment of this.attachments) {
      attachment.removed = true;
      this.uploads.get(attachment)?.abort();
      if (removeRemote && attachment.state === 'ready' && attachment.id) {
        deleteUpload(this.envName, attachment.id);
      }
    }
    this.attachments = [];
    this.uploading = null;
    this.composerError = '';
  }

  // One upload at a time, in the order the files were added.
  private pumpUploads(): void {
    if (this.uploading) return;
    const next = this.attachments.find((a) => a.state === 'queued');
    if (!next) return;
    this.uploading = next;
    next.state = 'uploading';
    next.progress = 0;
    const upload = uploadFile(this.envName, next.file, next.name, (percent) => {
      if (!next.removed) next.progress = percent;
    });
    this.uploads.set(next, upload);
    upload.done
      .then((result) => {
        next.id = result.id;
        next.path = result.path;
        next.uri = result.uri;
        next.mimeType = result.mimeType || next.mimeType;
        next.size = result.size;
        next.state = 'ready';
        next.progress = 100;
        // Removed while it was still uploading: delete the orphan.
        if (next.removed) deleteUpload(this.envName, result.id);
      })
      .catch((e) => {
        next.state = 'failed';
        next.error = errorText(e);
      })
      .finally(() => {
        this.uploads.delete(next);
        if (this.uploading === next) this.uploading = null;
        this.pumpUploads();
      });
  }
}
