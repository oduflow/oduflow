<script lang="ts">
  import { setContext } from 'svelte';
  import type { ChatSession, ChatStatus } from '../lib/chat.svelte';
  import type { EnvironmentState } from '../lib/environment.svelte';
  import { relTime } from '../lib/format';
  import { isEmpty } from '../lib/transcript';
  import Composer from './Composer.svelte';
  import EmptyState from './EmptyState.svelte';
  import Menu from './Menu.svelte';
  import PermissionPrompt from './PermissionPrompt.svelte';
  import Transcript from './Transcript.svelte';

  let { chat, env }: { chat: ChatSession; env: EnvironmentState } = $props();

  // Markdown links into this environment's Odoo open through Connect As.
  setContext('workspace', {
    get envName() {
      return env.name;
    },
    get envUrl() {
      return env.info?.url || '';
    },
  });

  const STATUS: Record<ChatStatus, { label: string; tone: string }> = {
    connecting: { label: 'Connecting…', tone: 'dim' },
    ready: { label: 'Ready', tone: 'green' },
    thinking: { label: 'Working…', tone: 'accent' },
    permission: { label: 'Needs your approval', tone: 'yellow' },
    error: { label: 'Error', tone: 'red' },
    closed: { label: 'Disconnected', tone: 'red' },
  };

  let draft = $state('');
  let composer: Composer;
  let empty = $derived(isEmpty(chat.transcript));
  let status = $derived(STATUS[chat.status]);

  function pick(example: string) {
    draft = example;
    composer.focus();
  }
</script>

<div class="chat">
  <div class="toolbar">
    <span class="status tone-{status.tone}" role="status">
      <span class="agent">{chat.agentLabel}</span>
      <span class="state">{status.label}</span>
    </span>
    <div class="tools">
      <Menu label="History" title="Earlier conversations" disabled={chat.busy || chat.status === 'connecting'}>
        {#if chat.history.length}
          {#each chat.history as entry (entry.session_id)}
            <button
              type="button"
              role="menuitem"
              class="history-item"
              disabled={entry.session_id === chat.sessionId}
              onclick={() => chat.switchTo(entry.session_id)}
            >
              <span class="history-title">{entry.title || 'Untitled conversation'}</span>
              <span class="history-meta">
                {entry.last_used_at || entry.created_at
                  ? relTime(entry.last_used_at || entry.created_at || '')
                  : ''}
                {#if entry.session_id === chat.sessionId}· current{/if}
              </span>
            </button>
          {/each}
        {:else}
          <button type="button" role="menuitem" disabled>No earlier conversations</button>
        {/if}
      </Menu>
      <button
        type="button"
        class="btn"
        title="Start a fresh conversation"
        disabled={chat.busy || chat.status === 'connecting' || chat.status === 'closed'}
        onclick={() => chat.newConversation()}
      >
        New chat
      </button>
    </div>
  </div>

  {#if env.info && !env.running && !env.action}
    <div class="banner banner-top" role="status">
      <span>Odoo is not running, so changes cannot be tried yet.</span>
      <button type="button" class="btn btn-start" onclick={() => env.run('start')}>Start Odoo</button>
    </div>
  {/if}

  {#if empty && (chat.status === 'ready' || chat.status === 'connecting')}
    <EmptyState onpick={pick} ready={chat.status === 'ready'} />
  {:else}
    <Transcript {chat} />
  {/if}

  <div class="dock">
    {#if chat.status === 'closed' || chat.status === 'error'}
      <div class="banner banner-error" role="alert">
        <span>
          {chat.status === 'closed'
            ? 'The connection to the agent was lost.'
            : 'Something went wrong with the agent.'}
        </span>
        <button type="button" class="btn" onclick={() => chat.reconnect()}>Reconnect</button>
      </div>
    {/if}
    {#if chat.permissions.length}
      <PermissionPrompt request={chat.permissions[0]} onrespond={(id) => chat.respond(chat.permissions[0], id)} />
    {/if}
    <Composer bind:this={composer} bind:draft {chat} />
  </div>
</div>

<style>
  .chat {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .toolbar,
  .banner,
  .dock {
    width: 100%;
    max-width: 52rem;
    margin: 0 auto;
  }
  .toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 10px 20px 6px;
  }
  .tools {
    display: flex;
    gap: 8px;
  }
  .status {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: var(--text-xs);
    color: var(--text-dim);
  }
  .agent {
    font-weight: 600;
    color: var(--text);
  }
  .state {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .state::before {
    content: '';
    width: 8px;
    height: 8px;
    border-radius: 999px;
    background: currentColor;
  }
  .tone-green .state {
    color: var(--green);
  }
  .tone-accent .state {
    color: var(--accent);
  }
  .tone-yellow .state {
    color: var(--yellow);
  }
  .tone-red .state {
    color: var(--red);
  }
  .history-item {
    display: flex !important;
    flex-direction: column;
    gap: 2px;
    max-width: 320px;
  }
  .history-title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-weight: 600;
  }
  .history-meta {
    color: var(--text-dim);
    font-size: var(--text-2xs);
  }
  .banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-top: 6px;
    padding: 8px 12px;
    border: 1px solid color-mix(in oklab, var(--yellow) 30%, transparent);
    border-radius: var(--radius-sm);
    background: color-mix(in oklab, var(--yellow) 8%, var(--surface));
    font-size: var(--text-xs);
  }
  .banner-top {
    width: calc(100% - 40px);
    max-width: calc(52rem - 40px);
  }
  .banner-error {
    border-color: color-mix(in oklab, var(--red) 30%, transparent);
    background: color-mix(in oklab, var(--red) 8%, var(--surface));
    margin: 0 0 8px;
  }
  .dock {
    padding: 0 20px 16px;
  }
  @media (max-width: 640px) {
    .toolbar {
      padding: 8px 12px 4px;
    }
    .dock {
      padding: 0 12px 12px;
    }
    .banner-top {
      width: calc(100% - 24px);
    }
  }
</style>
