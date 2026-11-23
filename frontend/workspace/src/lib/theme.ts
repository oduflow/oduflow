// Theme, shared with the dashboard: the same localStorage key and the same
// System -> Light -> Dark cycle (DESIGN.md §2, Light theme). index.html applies
// it before first paint.

export type ThemeMode = 'system' | 'light' | 'dark';

const KEY = 'oduflow-theme';

export function storedMode(): ThemeMode {
  try {
    const stored = localStorage.getItem(KEY);
    return stored === 'light' || stored === 'dark' ? stored : 'system';
  } catch {
    return 'system';
  }
}

export function applyMode(mode: ThemeMode): void {
  try {
    if (mode === 'system') localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, mode);
  } catch {
    /* storage unavailable: still apply for this page */
  }
  const effective =
    mode === 'system'
      ? window.matchMedia('(prefers-color-scheme: light)').matches
        ? 'light'
        : 'dark'
      : mode;
  document.documentElement.dataset.theme = effective;
}

export function nextMode(mode: ThemeMode): ThemeMode {
  return mode === 'system' ? 'light' : mode === 'light' ? 'dark' : 'system';
}

export const MODE_LABELS: Record<ThemeMode, string> = {
  system: 'System',
  light: 'Light',
  dark: 'Dark',
};
