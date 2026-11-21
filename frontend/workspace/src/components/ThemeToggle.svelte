<script lang="ts">
  // The header theme toggle, the same as the dashboard's (DESIGN.md §2, Light
  // theme): System -> Light -> Dark, an inline SVG plus its name, persisted to
  // localStorage. In System mode the page follows the OS live.
  import { onMount } from 'svelte';
  import { applyMode, MODE_LABELS, nextMode, storedMode, type ThemeMode } from '../lib/theme';

  let mode = $state<ThemeMode>(storedMode());

  function cycle() {
    mode = nextMode(mode);
    applyMode(mode);
  }

  onMount(() => {
    const query = window.matchMedia('(prefers-color-scheme: light)');
    const follow = () => {
      if (mode === 'system') applyMode('system');
    };
    query.addEventListener('change', follow);
    return () => query.removeEventListener('change', follow);
  });
</script>

<button
  type="button"
  class="btn toggle"
  title="Theme: {MODE_LABELS[mode]} (click to cycle)"
  aria-label="Theme: {MODE_LABELS[mode]}. Click to change."
  onclick={cycle}
>
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    {#if mode === 'system'}
      <rect x="2" y="3" width="20" height="14" rx="2" /><path d="M8 21h8M12 17v4" />
    {:else if mode === 'light'}
      <circle cx="12" cy="12" r="4" /><path
        d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M6.3 17.7l-1.4 1.4M19.1 4.9l-1.4 1.4"
      />
    {:else}
      <path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z" />
    {/if}
  </svg>
  <span class="label">{MODE_LABELS[mode]}</span>
</button>

<style>
  .toggle {
    color: var(--text-dim);
  }
  .toggle svg {
    width: 15px;
    height: 15px;
  }
  @media (max-width: 640px) {
    .label {
      display: none;
    }
  }
</style>
