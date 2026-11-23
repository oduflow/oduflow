<script lang="ts">
  // The message box: Ctrl+Enter (Cmd+Enter on macOS) sends, Enter breaks the
  // line. Files can be
  // attached with the button, pasted (a screenshot of what you mean is often
  // the best description) or dropped onto the box.
  import { tick } from 'svelte';
  import type { ChatSession } from '../lib/chat.svelte';
  import { formatBytes } from '../lib/format';
  import type { PendingAttachment } from '../lib/attachments';

  let { chat, draft = $bindable('') }: { chat: ChatSession; draft?: string } = $props();

  let textarea: HTMLTextAreaElement;
  let fileInput: HTMLInputElement;
  let dragging = $state(false);
  const sendHint = /Mac|iPhone|iPad/.test(navigator.platform)
    ? '⌘+Enter to send · Enter for a new line'
    : 'Ctrl+Enter to send · Enter for a new line';

  export function focus() {
    textarea.focus();
    void tick().then(resize);
  }

  function resize() {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 480) + 'px';
  }

  $effect(() => {
    void draft;
    void tick().then(resize);
  });

  function submit(event?: Event) {
    event?.preventDefault();
    if (chat.send(draft)) {
      draft = '';
      textarea.focus();
    }
  }

  function onkeydown(event: KeyboardEvent) {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  function oninput() {
    if (draft.trim()) chat.composerError = '';
  }

  function onpaste(event: ClipboardEvent) {
    const files = event.clipboardData?.files;
    if (files && files.length && chat.canAttach) chat.addFiles(files);
  }

  function ondrop(event: DragEvent) {
    event.preventDefault();
    dragging = false;
    const files = event.dataTransfer?.files;
    if (files && files.length && chat.canAttach) chat.addFiles(files);
  }

  function ondragleave(event: DragEvent) {
    if (!(event.currentTarget as Element).contains(event.relatedTarget as Node)) dragging = false;
  }

  function attachmentState(a: PendingAttachment): string {
    if (a.state === 'queued') return 'waiting';
    if (a.state === 'uploading') return 'uploading ' + (a.progress || 0) + '%';
    if (a.state === 'ready') return 'ready';
    return 'failed' + (a.error ? ': ' + a.error : '');
  }
</script>

<form
  class="composer"
  class:dragging
  onsubmit={submit}
  ondragover={(event) => {
    event.preventDefault();
    dragging = true;
  }}
  {ondragleave}
  {ondrop}
>
  {#if chat.attachments.length}
    <ul class="pending" aria-live="polite" aria-label="Files to send">
      {#each chat.attachments as attachment, i (i)}
        <li data-state={attachment.state}>
          <span class="name mono">{attachment.name}</span>
          <span class="meta">
            {[formatBytes(attachment.size), attachmentState(attachment)].filter(Boolean).join(' · ')}
          </span>
          <button type="button" class="remove" onclick={() => chat.removeAttachment(attachment)}>
            Remove <span class="sr-only">{attachment.name}</span>
          </button>
        </li>
      {/each}
    </ul>
  {/if}
  <label class="sr-only" for="composer-input">Message to the agent</label>
  <textarea
    id="composer-input"
    bind:this={textarea}
    bind:value={draft}
    rows="4"
    placeholder="Describe what you would like to change…"
    aria-describedby="composer-help"
    aria-invalid={chat.composerError ? 'true' : 'false'}
    {onkeydown}
    {oninput}
    {onpaste}
  ></textarea>
  <div class="row">
    <div class="left">
      <input
        bind:this={fileInput}
        type="file"
        multiple
        hidden
        onchange={() => {
          if (fileInput.files) chat.addFiles(fileInput.files);
          fileInput.value = '';
        }}
      />
      <button
        type="button"
        class="btn btn-ghost"
        disabled={!chat.canAttach}
        title="Attach files or screenshots (or paste them into the box)"
        onclick={() => fileInput.click()}
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
        </svg>
        Attach
      </button>
      {#if chat.models && chat.models.availableModels.length > 1}
        <label class="sr-only" for="composer-model">Model</label>
        <select
          id="composer-model"
          class="model"
          value={chat.models.currentModelId}
          disabled={chat.busy}
          onchange={(event) => chat.setModel((event.currentTarget as HTMLSelectElement).value)}
        >
          {#each chat.models.availableModels as model (model.modelId)}
            <option value={model.modelId}>{model.name}</option>
          {/each}
        </select>
      {/if}
    </div>
    <div class="right">
      <span id="composer-help" class="hint" class:error={!!chat.composerError} aria-live="polite">
        {chat.composerError || sendHint}
      </span>
      {#if chat.busy}
        <button type="button" class="btn btn-stop" onclick={() => chat.stop()}>Stop</button>
      {:else}
        <button type="submit" class="btn btn-primary" disabled={!chat.canSend || !draft.trim()}>
          Send
        </button>
      {/if}
    </div>
  </div>
</form>

<style>
  .composer {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 10px 12px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    transition: border-color 0.15s;
  }
  .composer:focus-within {
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }
  .composer.dragging {
    border-color: var(--accent);
    background: var(--subtle);
  }
  textarea {
    width: 100%;
    min-height: 5.5rem;
    max-height: 480px;
    resize: none;
    border: 0;
    background: transparent;
    color: var(--text);
    font-size: var(--text-sm);
    line-height: 1.5;
  }
  textarea:focus,
  textarea:focus-visible {
    outline: none;
    box-shadow: none;
  }
  textarea::placeholder {
    color: var(--text-dim);
  }
  .row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    flex-wrap: wrap;
  }
  .left,
  .right {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  .btn-ghost {
    margin-left: -6px;
  }
  .model {
    height: 1.9rem;
    max-width: 220px;
    padding: 0 6px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    font-size: var(--text-xs);
  }
  .hint {
    color: var(--text-dim);
    font-size: var(--text-2xs);
  }
  .hint.error {
    color: var(--red);
  }
  .pending {
    list-style: none;
    display: grid;
    gap: 2px;
  }
  .pending li {
    display: flex;
    align-items: baseline;
    gap: 10px;
    min-width: 0;
    padding: 3px 0;
    border-bottom: 1px solid var(--border);
  }
  .pending .name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--text-xs);
    font-weight: 600;
  }
  .pending .meta {
    flex: 1;
    color: var(--text-dim);
    font-size: var(--text-2xs);
  }
  .pending li[data-state='failed'] .meta {
    color: var(--red);
  }
  .pending li[data-state='uploading'] .meta {
    color: var(--accent);
  }
  .pending li[data-state='ready'] .meta {
    color: var(--green);
  }
  .remove {
    flex: 0 0 auto;
    padding: 2px 5px;
    border: 0;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text-dim);
    font-size: var(--text-2xs);
    font-weight: 600;
    cursor: pointer;
  }
  .remove:hover,
  .remove:focus-visible {
    color: var(--red);
  }
  @media (max-width: 640px) {
    .hint {
      display: none;
    }
    .hint.error {
      display: inline;
    }
  }
</style>
