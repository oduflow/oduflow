<script lang="ts">
  // The scrolling conversation. It follows new output while the reader is at
  // the bottom and leaves them alone once they scroll up to read.
  import { onMount } from 'svelte';
  import type { ChatSession } from '../lib/chat.svelte';
  import ImageViewer from './ImageViewer.svelte';
  import Turn from './Turn.svelte';
  import UserMessage from './UserMessage.svelte';
  import WorkingDots from './WorkingDots.svelte';

  let { chat }: { chat: ChatSession } = $props();

  const FOLLOW_THRESHOLD = 48;
  let scroller: HTMLDivElement;
  let content: HTMLDivElement;
  let follow = true;
  let viewing = $state<string | null>(null);

  let items = $derived(chat.transcript.items);
  // The agent is busy and its reply shows nothing yet (once it does, the
  // reply's own "Working" line or the streaming text says so).
  let waiting = $derived.by(() => {
    if (!chat.busy) return false;
    const last = items[items.length - 1];
    return !last || last.kind !== 'turn' || last.entries.length === 0;
  });

  function onScroll() {
    follow = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= FOLLOW_THRESHOLD;
  }

  onMount(() => {
    scroller.scrollTop = scroller.scrollHeight;
    // Any growth (streamed text, a tool result, an image that finished
    // loading) keeps a following reader at the bottom.
    const observer = new ResizeObserver(() => {
      if (follow) scroller.scrollTop = scroller.scrollHeight;
    });
    observer.observe(content);
    return () => observer.disconnect();
  });

  // Sending a message always brings the reply into view.
  let lastUserId = 0;
  $effect(() => {
    const last = items.findLast((item) => item.kind === 'user');
    if (last && last.id !== lastUserId) {
      lastUserId = last.id;
      follow = true;
    }
  });
</script>

<div class="scroller" bind:this={scroller} onscroll={onScroll}>
  <div class="content" bind:this={content} role="log" aria-live="polite" aria-label="Conversation">
    {#each items as item (item.id)}
      {#if item.kind === 'user'}
        <UserMessage {item} />
      {:else if item.kind === 'turn'}
        <Turn turn={item} transcript={chat.transcript} onimage={(src) => (viewing = src)} />
      {:else}
        <p class="system" class:error={item.tone === 'error'}>{item.text}</p>
      {/if}
    {/each}
    {#if waiting}
      <p class="working" role="status">
        <WorkingDots />
        {chat.status === 'permission' ? 'Waiting for your approval' : 'Working'}
      </p>
    {/if}
  </div>
</div>

{#if viewing}
  <ImageViewer src={viewing} onclose={() => (viewing = null)} />
{/if}

<style>
  .scroller {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
  }
  .content {
    display: flex;
    flex-direction: column;
    gap: 14px;
    max-width: 52rem;
    margin: 0 auto;
    padding: 12px 20px 20px;
  }
  .system {
    color: var(--text-dim);
    font-size: var(--text-xs);
    text-align: center;
    white-space: pre-wrap;
  }
  .system.error {
    color: var(--red);
  }
  .working {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    color: var(--text-dim);
    font-size: var(--text-xs);
  }
  @media (max-width: 640px) {
    .content {
      padding: 10px 12px 16px;
    }
  }
</style>
