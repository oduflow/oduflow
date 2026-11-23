<script lang="ts">
  import { formatBytes } from '../lib/format';
  import type { UserItem } from '../lib/transcript';
  import { stripWorkspaceContext } from '../lib/workspaceContext';

  let { item }: { item: UserItem } = $props();

  let text = $derived(stripWorkspaceContext(item.text));
</script>

{#if text || item.attachments.length}
  <div class="user">
    <div class="bubble">
      {#if text}<div class="text">{text}</div>{/if}
      {#if item.attachments.length}
        <ul class="files" aria-label="Attached files">
          {#each item.attachments as file, i (i)}
            <li>
              <span class="name mono">{file.name || 'Attachment'}</span>
              <span class="meta">{[formatBytes(file.size), file.mimeType].filter(Boolean).join(' · ')}</span>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  </div>
{/if}

<style>
  .user {
    display: flex;
    justify-content: flex-end;
  }
  .bubble {
    max-width: 80%;
    padding: 8px 12px;
    border-radius: var(--radius);
    background: color-mix(in oklab, var(--accent) 18%, var(--surface));
    border: 1px solid color-mix(in oklab, var(--accent) 35%, transparent);
    overflow-wrap: anywhere;
  }
  .text {
    white-space: pre-wrap;
  }
  .files {
    list-style: none;
    display: grid;
    gap: 4px;
    margin-top: 7px;
  }
  .text + .files {
    margin-top: 7px;
  }
  .files li {
    display: flex;
    align-items: baseline;
    gap: 8px;
    min-width: 0;
    padding-top: 5px;
    border-top: 1px solid var(--border);
  }
  .name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--text-xs);
    font-weight: 600;
  }
  .meta {
    flex: 0 0 auto;
    color: var(--text-dim);
    font-size: var(--text-2xs);
  }
</style>
