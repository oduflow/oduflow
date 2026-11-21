// The ACP wire client is shared with the dashboard chat: one copy, in the
// package (static/acp-client.js), bundled here at build time. It registers
// itself as window.AcpClient.
import '../../../../src/oduflow/templates/static/acp-client.js';

export interface PermissionOption {
  optionId: string;
  name?: string;
  kind?: string;
}

export interface PermissionParams {
  toolCall?: { title?: string };
  options?: PermissionOption[];
}

export interface AcpEvents {
  update: { update?: Record<string, unknown> };
  permission: { requestId: number | string; params: PermissionParams };
  error: string;
  notice: string;
  close: string;
  open: undefined;
}

export interface SessionResult {
  sessionId?: string;
  models?: unknown;
  configOptions?: unknown;
  [key: string]: unknown;
}

export interface InitializeResult {
  agentCapabilities?: { promptCapabilities?: { image?: boolean } };
}

export interface AcpClient {
  closed: boolean;
  on<K extends keyof AcpEvents>(event: K, cb: (payload: AcpEvents[K]) => void): AcpClient;
  connect(): Promise<void>;
  initialize(): Promise<InitializeResult>;
  newSession(cwd: string): Promise<SessionResult>;
  loadSession(sessionId: string, cwd: string): Promise<SessionResult>;
  prompt(sessionId: string, content: unknown[]): Promise<unknown>;
  cancel(sessionId: string): void;
  setModel(sessionId: string, modelId: string): Promise<SessionResult>;
  respondPermission(requestId: number | string, optionId: string | null): void;
  close(): void;
}

type AcpClientConstructor = new (url: string) => AcpClient;

export function createAcpClient(url: string): AcpClient {
  const ctor = (window as unknown as { AcpClient: AcpClientConstructor }).AcpClient;
  return new ctor(url);
}
