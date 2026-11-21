// The shared environment's live state, polled like the dashboard card.

import { fetchEnvironment, restartEnvironment, startEnvironment, type EnvInfo } from './api';

const POLL_MS = 10000;

export type EnvAction = 'start' | 'restart';

export class EnvironmentState {
  info = $state<EnvInfo | null>(null);
  loaded = $state(false);
  /** The environment no longer exists (deleted since the link was shared). */
  missing = $state(false);
  action = $state<EnvAction | null>(null);
  error = $state('');

  readonly name: string;
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor(name: string) {
    this.name = name;
  }

  get running(): boolean {
    return this.info?.status === 'running';
  }

  async refresh(): Promise<void> {
    try {
      const info = await fetchEnvironment(this.name);
      this.info = info;
      this.missing = info === null;
      this.error = '';
    } catch (e) {
      this.error = e instanceof Error ? e.message : String(e);
    } finally {
      this.loaded = true;
    }
  }

  watch(): () => void {
    void this.refresh();
    this.timer = setInterval(() => {
      if (!document.hidden) void this.refresh();
    }, POLL_MS);
    return () => {
      if (this.timer) clearInterval(this.timer);
      this.timer = null;
    };
  }

  async run(action: EnvAction): Promise<void> {
    if (this.action) return;
    this.action = action;
    this.error = '';
    try {
      if (action === 'start') await startEnvironment(this.name);
      else await restartEnvironment(this.name);
    } catch (e) {
      this.error = e instanceof Error ? e.message : String(e);
    } finally {
      this.action = null;
      await this.refresh();
    }
  }
}
