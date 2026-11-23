<script lang="ts">
  // The tail of Odoo's log, for when something does not work and someone asks
  // "what does the log say?". Read-only; refresh on demand.
  import { onMount } from 'svelte';
  import { fetchLogs } from '../lib/api';
  import { stripAnsi } from '../lib/format';

  let { envName, onclose }: { envName: string; onclose: () => void } = $props();

  const LINES = 300;
  let logs = $state('');
  let loading = $state(false);
  let error = $state('');
  let output: HTMLPreElement;
  let closeButton: HTMLButtonElement;

  async function load() {
    loading = true;
    error = '';
    try {
      logs = stripAnsi(await fetchLogs(envName, LINES));
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
    requestAnimationFrame(() => {
      if (output) output.scrollTop = output.scrollHeight;
    });
  }

  onMount(() => {
    const previous = document.activeElement as HTMLElement | null;
    closeButton.focus();
    void load();
    return () => previous?.focus();
  });

  function onkeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') onclose();
  }
</script>

<svelte:window {onkeydown} />

<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div class="backdrop" onclick={onclose}></div>
<div class="panel" role="dialog" aria-modal="true" aria-labelledby="logs-title">
  <header>
    <h2 id="logs-title">Odoo logs</h2>
    <div class="actions">
      <button type="button" class="btn" disabled={loading} onclick={load}>
        {loading ? 'Refreshing…' : 'Refresh'}
      </button>
      <button bind:this={closeButton} type="button" class="btn" onclick={onclose}>Close</button>
    </div>
  </header>
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  <!-- Focusable so the log can be scrolled from the keyboard. -->
  <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
  <pre bind:this={output} tabindex="0" aria-label="Last {LINES} log lines">{logs || (loading ? 'Loading…' : 'No log output.')}</pre>
</div>

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    z-index: var(--z-modal);
    background: var(--backdrop);
  }
  .panel {
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    z-index: var(--z-modal);
    display: flex;
    flex-direction: column;
    width: min(56rem, 100vw);
    background: var(--surface-raised);
    border-left: 1px solid var(--border-raised);
    box-shadow: var(--shadow-modal);
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
  }
  h2 {
    font-size: var(--text-lg);
    font-weight: 600;
  }
  .actions {
    display: flex;
    gap: 8px;
  }
  .error {
    padding: 8px 16px;
    color: var(--red);
    font-size: var(--text-xs);
  }
  pre {
    flex: 1;
    min-height: 0;
    overflow: auto;
    margin: 12px;
    padding: 10px 12px;
    border-radius: var(--radius-sm);
    background: var(--terminal-bg);
    color: var(--terminal-text);
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
  }
</style>
