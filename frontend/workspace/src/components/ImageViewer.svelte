<script lang="ts">
  // A screenshot at full size. Escape, the close button or a click outside
  // the image closes it, and focus returns to where it was.
  import { onMount } from 'svelte';

  let { src, onclose }: { src: string; onclose: () => void } = $props();

  let closeButton: HTMLButtonElement;

  onMount(() => {
    const previous = document.activeElement as HTMLElement | null;
    closeButton.focus();
    return () => previous?.focus();
  });

  function onkeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') onclose();
  }

  function onBackdrop(event: MouseEvent) {
    if (event.target === event.currentTarget) onclose();
  }
</script>

<svelte:window {onkeydown} />

<!-- svelte-ignore a11y_click_events_have_key_events -->
<div class="backdrop" role="dialog" aria-modal="true" aria-label="Screenshot" tabindex="-1" onclick={onBackdrop}>
  <div class="frame">
    <button bind:this={closeButton} type="button" class="btn close" onclick={onclose}>Close</button>
    <img {src} alt="Screenshot the agent took to check its work" />
  </div>
</div>

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    z-index: var(--z-modal);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
    background: var(--backdrop);
    backdrop-filter: blur(3px);
  }
  .frame {
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 8px;
    max-width: 100%;
    max-height: 100%;
  }
  img {
    max-width: 100%;
    max-height: calc(100vh - 100px);
    object-fit: contain;
    border: 1px solid var(--border-raised);
    border-radius: var(--radius-sm);
    background: var(--surface-raised);
    box-shadow: var(--shadow-modal);
  }
</style>
