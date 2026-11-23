<script lang="ts">
  // One tool call. Its input and output are rendered only while it is open:
  // results can be large, and most readers never look.
  import {
    displayParts,
    formatToolValue,
    toolOutputValue,
    toolStatusText,
    type ToolState,
  } from '../lib/toolOutput';

  let { tool }: { tool: ToolState } = $props();

  let open = $state(false);
  let parts = $derived(open ? displayParts(toolOutputValue(tool)) : []);
</script>

<details class="tool" bind:open>
  <summary>
    <span class="title mono">{tool.title || tool.kind || 'Tool'}</span>
    <span class="status" data-status={tool.status || 'pending'}>{toolStatusText(tool.status)}</span>
  </summary>
  {#if open}
    <div class="payload">
      <div class="label">Input</div>
      <pre>{tool.hasRawInput ? formatToolValue(tool.rawInput) : 'Not provided by the agent'}</pre>
      <div class="label">Output</div>
      {#each parts as part, i (i)}
        {#if part.src}
          <img src={part.src} alt="Tool result" />
        {:else}
          <pre>{part.value === undefined ? 'Not provided by the agent' : formatToolValue(part.value)}</pre>
        {/if}
      {/each}
    </div>
  {/if}
</details>

<style>
  .tool {
    min-width: 0;
    font-size: var(--text-xs);
    border-bottom: 1px solid var(--border);
  }
  summary {
    display: flex;
    gap: 8px;
    align-items: baseline;
    min-width: 0;
    padding: 4px;
    border-radius: var(--radius-sm);
    cursor: pointer;
  }
  summary:hover {
    background: var(--subtle);
  }
  .title {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text);
  }
  .status {
    flex: 0 0 auto;
    font-weight: 600;
  }
  .status[data-status='completed'] {
    color: var(--green);
  }
  .status[data-status='failed'] {
    color: var(--red);
  }
  .status[data-status='in_progress'] {
    color: var(--accent);
  }
  .payload {
    display: grid;
    gap: 4px;
    padding: 4px 4px 10px 18px;
  }
  .label {
    margin-top: 4px;
    color: var(--text-dim);
    font-size: var(--text-2xs);
    font-weight: 600;
  }
  pre {
    max-height: 40vh;
    overflow: auto;
    padding: 8px 10px;
    border-radius: var(--radius-sm);
    background: var(--terminal-bg);
    color: var(--terminal-text);
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    white-space: pre-wrap;
    word-break: break-word;
  }
  img {
    display: block;
    max-width: 100%;
    max-height: 60vh;
    object-fit: contain;
    object-position: left top;
    border-radius: var(--radius-sm);
  }
</style>
