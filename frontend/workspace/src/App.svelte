<script lang="ts">
  // The shared-environment workspace: a chat with the environment's coding
  // agent fills the page; Odoo itself opens in its own tab. Everything here is
  // presentation: the share-link scope is enforced by the server
  // (oduflow/ui_scope.py). See specs/0087-chat-first-shared-workspace.md.
  import { onMount } from 'svelte';
  import Chat from './components/Chat.svelte';
  import Header from './components/Header.svelte';
  import LogsPanel from './components/LogsPanel.svelte';
  import Notice from './components/Notice.svelte';
  import { fetchAgentInfo, type AgentInfo } from './lib/api';
  import type { Brand } from './lib/brand';
  import { ChatSession } from './lib/chat.svelte';
  import { EnvironmentState } from './lib/environment.svelte';

  let {
    envName,
    viewer,
    brand,
  }: { envName: string; viewer: 'share' | 'operator'; brand: Brand } = $props();

  // The page is for one environment for its whole life.
  // svelte-ignore state_referenced_locally
  const env = new EnvironmentState(envName);
  let agent = $state<AgentInfo | null>(null);
  let agentError = $state('');
  let chat = $state<ChatSession | null>(null);
  let logsOpen = $state(false);

  // Why there is no chat, if there is none: the agent must be enabled for the
  // team, and the environment must have a checkout the agent can edit (a
  // live-mounted local_path environment is edited on a developer's machine).
  let blocker = $derived.by(() => {
    if (agentError) return { title: 'The coding agent is unavailable', body: agentError };
    if (agent && !agent.enabled) {
      return {
        title: 'The coding agent is not enabled',
        body: 'Ask whoever shared this link to enable the coding agent for their team.',
      };
    }
    if (env.info?.local_path) {
      return {
        title: 'Chat is not available for this environment',
        body: "Its code lives on a developer's computer, so the agent cannot change it from here. You can still open Odoo.",
      };
    }
    return null;
  });

  onMount(() => {
    if (envName) document.title = envName + ' · ' + brand.name;
    const unwatch = envName ? env.watch() : () => {};
    fetchAgentInfo()
      .then((info) => (agent = info))
      .catch((e) => (agentError = e instanceof Error ? e.message : String(e)));
    return () => {
      unwatch();
      chat?.dispose();
    };
  });

  $effect(() => {
    if (chat || !agent || !env.info || blocker) return;
    const session = new ChatSession(envName, agent.default || 'claude');
    chat = session;
    void session.start();
  });
</script>

<div class="app">
  <Header {env} {viewer} {brand} showStart={!chat} onlogs={() => (logsOpen = true)} />
  <main class="main">
    {#if !envName}
      <Notice title="No environment selected" body="Open the link you were sent to reach your environment." />
    {:else if env.loaded && env.missing}
      <Notice
        title="This environment is not available"
        body="It may have been deleted. Ask whoever shared the link."
      />
    {:else if blocker}
      <Notice title={blocker.title} body={blocker.body} />
    {:else if chat}
      <Chat {chat} {env} />
    {:else if env.loaded && !env.info && env.error}
      <Notice title="Could not load this environment" body={env.error} />
    {:else}
      <p class="loading" role="status">Loading…</p>
    {/if}
  </main>
  {#if logsOpen}
    <LogsPanel {envName} onclose={() => (logsOpen = false)} />
  {/if}
</div>

<style>
  .app {
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
    grid-template-columns: minmax(0, 1fr);
    height: 100%;
  }
  .main {
    min-height: 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .loading {
    margin: auto;
    color: var(--text-dim);
  }
</style>
