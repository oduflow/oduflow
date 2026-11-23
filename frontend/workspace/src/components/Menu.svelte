<script lang="ts">
  // A small dropdown: closes on outside click, Escape, or choosing an item.
  import type { Snippet } from 'svelte';

  let {
    label,
    title = '',
    disabled = false,
    button,
    children,
  }: {
    label: string;
    title?: string;
    disabled?: boolean;
    button?: Snippet;
    children: Snippet;
  } = $props();

  let open = $state(false);
  let root: HTMLDivElement;
  let trigger: HTMLButtonElement;

  function onWindowClick(event: MouseEvent) {
    if (open && !root.contains(event.target as Node)) open = false;
  }

  function onKeydown(event: KeyboardEvent) {
    if (open && event.key === 'Escape') {
      open = false;
      trigger.focus();
    }
  }

  function onMenuClick(event: MouseEvent) {
    if ((event.target as Element).closest('button, a')) open = false;
  }
</script>

<svelte:window onclick={onWindowClick} onkeydown={onKeydown} />

<div class="wrap" bind:this={root}>
  <button
    bind:this={trigger}
    type="button"
    class="btn"
    {title}
    {disabled}
    aria-haspopup="menu"
    aria-expanded={open}
    onclick={() => (open = !open)}
  >
    {#if button}{@render button()}{:else}{label}{/if}
  </button>
  {#if open}
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <div class="menu" role="menu" aria-label={label} tabindex="-1" onclick={onMenuClick}>
      {@render children()}
    </div>
  {/if}
</div>

<style>
  .wrap {
    position: relative;
  }
</style>
