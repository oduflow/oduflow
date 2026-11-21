// The REST surface the workspace uses. All of it is on the shared-link
// allowlist (oduflow/ui_scope.py); the server enforces the scope, not us.

export interface Container {
  name: string;
  status: string;
}

export interface EnvInfo {
  env_name: string;
  branch: string;
  status: string;
  url?: string;
  containers: Container[];
  local_path?: string;
  git_branch?: string;
  note?: string;
}

export interface AgentInfo {
  enabled: boolean;
  default: string;
}

export interface HistoryEntry {
  session_id: string;
  title?: string | null;
  created_at?: string;
  last_used_at?: string;
}

export interface ChatInfo {
  cwd: string;
  type: string;
  session_id: string | null;
  history: HistoryEntry[];
  attachment_limits?: { max_file_bytes?: number; max_files?: number };
}

export interface SessionState {
  session_id: string | null;
  history: HistoryEntry[];
}

export class ApiError extends Error {}

export function envApiPath(envName: string): string {
  return '/api/environments/' + encodeURIComponent(envName);
}

/** This page's own path. Environment names may contain slashes (feature/x). */
export function workspacePath(envName: string): string {
  return '/env/' + envName.split('/').map(encodeURIComponent).join('/');
}

// A session that expired or a link that was revoked: reload the page, which
// the server then answers with how to get back in. (Under `npm run dev` the
// page is Vite's, which would answer with itself, so just report it.)
function onUnauthorized(): never {
  if (!import.meta.env.DEV) window.location.reload();
  throw new ApiError('Your session has ended. Open the link you were sent again.');
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { credentials: 'same-origin', ...init });
  if (response.status === 401) onUnauthorized();
  let payload: ({ ok?: boolean; error?: string } & T) | null = null;
  try {
    payload = await response.json();
  } catch {
    /* not JSON */
  }
  if (!response.ok || !payload || payload.ok === false) {
    throw new ApiError((payload && payload.error) || 'Request failed (' + response.status + ').');
  }
  return payload;
}

function post<T>(url: string, body?: unknown): Promise<T> {
  return request<T>(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export async function fetchEnvironment(envName: string): Promise<EnvInfo | null> {
  const data = await request<{ environments: EnvInfo[] }>('/api/environments');
  return data.environments.find((env) => env.env_name === envName) || null;
}

export function fetchAgentInfo(): Promise<AgentInfo> {
  return request<AgentInfo>('/api/agent');
}

export function startEnvironment(envName: string): Promise<unknown> {
  return post(envApiPath(envName) + '/start');
}

export function restartEnvironment(envName: string): Promise<unknown> {
  return post(envApiPath(envName) + '/restart');
}

export async function fetchLogs(envName: string, lines: number): Promise<string> {
  const data = await request<{ logs: string }>(envApiPath(envName) + '/logs?n=' + lines);
  return data.logs || '';
}

function chatPath(envName: string): string {
  return envApiPath(envName) + '/agent-acp';
}

export function chatSocketUrl(envName: string, agentType: string): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return proto + '//' + location.host + chatPath(envName) + '?type=' + encodeURIComponent(agentType);
}

export function fetchChatInfo(envName: string, agentType: string): Promise<ChatInfo> {
  return request<ChatInfo>(chatPath(envName) + '/info?type=' + encodeURIComponent(agentType));
}

/** Remember the conversation to resume next time (and its title). */
export async function saveSession(
  envName: string,
  agentType: string,
  sessionId: string,
  title?: string
): Promise<SessionState | null> {
  const body: Record<string, string> = { type: agentType, session_id: sessionId };
  if (title !== undefined) body.title = title;
  try {
    return await post<SessionState>(chatPath(envName) + '/session', body);
  } catch {
    return null;
  }
}

export function deleteUpload(envName: string, id: string): void {
  fetch(chatPath(envName) + '/attachments/' + encodeURIComponent(id), {
    method: 'DELETE',
    credentials: 'same-origin',
  }).catch(() => {});
}

export interface UploadResult {
  id: string;
  path: string;
  uri: string;
  mimeType?: string;
  size: number;
}

export interface Upload {
  done: Promise<UploadResult>;
  abort(): void;
}

/** POST one file with progress; XHR because fetch has no upload progress. */
export function uploadFile(
  envName: string,
  file: File,
  name: string,
  onProgress: (percent: number) => void
): Upload {
  const xhr = new XMLHttpRequest();
  const done = new Promise<UploadResult>((resolve, reject) => {
    xhr.open('POST', chatPath(envName) + '/attachments?name=' + encodeURIComponent(name));
    xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.min(99, Math.round((event.loaded * 100) / event.total)));
    };
    xhr.onload = () => {
      let payload: { attachment?: UploadResult; error?: string } | null = null;
      try {
        payload = JSON.parse(xhr.responseText || '{}');
      } catch {
        /* not JSON */
      }
      if (xhr.status >= 200 && xhr.status < 300 && payload && payload.attachment) {
        resolve(payload.attachment);
      } else {
        reject(new ApiError((payload && payload.error) || 'Upload failed'));
      }
    };
    xhr.onerror = () => reject(new ApiError('Network error'));
    xhr.onabort = () => reject(new ApiError('Upload cancelled'));
    xhr.send(file);
  });
  return { done, abort: () => xhr.abort() };
}

/** End a shared session (clears the share cookie) — a plain form POST. */
export function leave(): void {
  const form = document.createElement('form');
  form.method = 'POST';
  form.action = '/logout';
  document.body.appendChild(form);
  form.submit();
}
