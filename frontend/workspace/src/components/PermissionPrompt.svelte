<script lang="ts">
  import type { PermissionRequest } from '../lib/chat.svelte';

  let {
    request,
    onrespond,
  }: { request: PermissionRequest; onrespond: (optionId: string | null) => void } = $props();
</script>

<div class="permission" role="alertdialog" aria-labelledby="permission-title">
  <p id="permission-title" class="title">The agent asks for approval: <strong>{request.title}</strong></p>
  <div class="actions">
    {#each request.options as option (option.optionId)}
      <button
        type="button"
        class="btn"
        class:btn-primary={option.allow}
        onclick={() => onrespond(option.optionId)}
      >
        {option.name}
      </button>
    {/each}
    {#if !request.options.length}
      <button type="button" class="btn" onclick={() => onrespond(null)}>Dismiss</button>
    {/if}
  </div>
</div>

<style>
  .permission {
    margin-bottom: 8px;
    padding: 10px 12px;
    border: 1px solid color-mix(in oklab, var(--yellow) 35%, transparent);
    border-radius: var(--radius-sm);
    background: color-mix(in oklab, var(--yellow) 8%, var(--surface));
  }
  .title {
    margin-bottom: 8px;
    overflow-wrap: anywhere;
  }
  .title strong {
    font-family: var(--font-mono);
    font-size: var(--text-xs);
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
</style>
