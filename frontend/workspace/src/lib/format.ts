// Small text helpers shared by the workspace components.

export function formatBytes(size: unknown): string {
  if (typeof size !== 'number' || !isFinite(size) || size < 0) return '';
  if (size < 1024) return size + ' B';
  if (size < 1024 * 1024) return Math.round(size / 1024) + ' KiB';
  return (size / (1024 * 1024)).toFixed(size < 10 * 1024 * 1024 ? 1 : 0) + ' MiB';
}

export function pluralCount(count: number, singular: string, plural: string): string {
  return count + ' ' + (count === 1 ? singular : plural);
}

/** "just now" / "5m ago" / "3h ago" / "2d ago" / a date, like the dashboard. */
export function relTime(iso: string, now: number = Date.now()): string {
  const d = new Date(iso);
  const diff = now - d.getTime();
  if (!isFinite(diff)) return '';
  if (diff < 60000) return 'just now';
  if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
  if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
  if (diff < 30 * 86400000) return Math.floor(diff / 86400000) + 'd ago';
  return d.toLocaleDateString();
}

/** A conversation title from the first line of the first prompt. */
export function titleFrom(text: string): string {
  return (text.split(/\r?\n/, 1)[0] || '').replace(/\s+/g, ' ').trim().slice(0, 80);
}

// Odoo's log lines carry ANSI colour codes when the container has a TTY.
const ANSI = /\x1b\[[0-9;]*[A-Za-z]/g;

export function stripAnsi(text: string): string {
  return text.replace(ANSI, '');
}
