<script lang="ts">
  import { leave, workspacePath } from '../lib/api';
  import type { Brand } from '../lib/brand';
  import type { EnvironmentState } from '../lib/environment.svelte';
  import { openOdoo } from '../lib/odoo';
  import Menu from './Menu.svelte';
  import ThemeToggle from './ThemeToggle.svelte';

  let {
    env,
    viewer,
    brand,
    showStart,
    onlogs,
  }: {
    env: EnvironmentState;
    viewer: 'share' | 'operator';
    brand: Brand;
    /** The chat shows its own Start banner; without a chat, offer it here. */
    showStart: boolean;
    onlogs: () => void;
  } = $props();

  let status = $derived(
    env.action === 'start'
      ? 'starting'
      : env.action === 'restart'
        ? 'restarting'
        : env.info?.status || (env.loaded ? 'unknown' : 'loading')
  );
  let badge = $derived(
    env.action
      ? 'badge-busy'
      : status === 'running'
        ? 'badge-running'
        : status === 'partial' || status === 'stopped'
          ? 'badge-' + status
          : status === 'exited'
            ? 'badge-exited'
            : 'badge-neutral'
  );
</script>

<header class="topbar">
  <div class="identity">
    <img
      class="logo"
      class:custom={brand.customLogo}
      src={'/logo.png' + (brand.assetVersion ? '?v=' + encodeURIComponent(brand.assetVersion) : '')}
      alt={brand.name + ' logo'}
    />
    {#if env.name}
      <span class="env mono" title={env.info?.git_branch ? 'Branch ' + env.info.git_branch : env.name}>
        {env.name}
      </span>
      <span class="badge {badge}" role="status">{status}</span>
    {/if}
  </div>
  <div class="actions">
    <ThemeToggle />
    {#if env.name}
      {#if showStart && env.info && !env.running}
        <button
          type="button"
          class="btn btn-start"
          disabled={!!env.action}
          onclick={() => env.run('start')}
        >
          {env.action === 'start' ? 'Starting…' : 'Start Odoo'}
        </button>
      {/if}
      <button
        type="button"
        class="btn btn-primary"
        disabled={!env.running}
        title={env.running ? 'Open Odoo in a new tab, signed in' : 'Start Odoo first'}
        onclick={() => openOdoo(env.name)}
      >
        Open Odoo <span aria-hidden="true">&#8599;</span>
      </button>
      <Menu label="More">
        <button type="button" role="menuitem" disabled={!env.running || !!env.action} onclick={() => env.run('restart')}>
          Restart Odoo
        </button>
        <button type="button" role="menuitem" onclick={onlogs}>View Odoo logs</button>
        <a role="menuitem" href={workspacePath(env.name) + '?view=console'}>Engineer view</a>
        <hr />
        {#if viewer === 'operator'}
          <a role="menuitem" href="/">Back to dashboard</a>
        {:else}
          <button type="button" role="menuitem" onclick={leave}>Leave</button>
        {/if}
      </Menu>
    {/if}
  </div>
</header>
{#if env.error && env.info}
  <p class="env-error" role="alert">{env.error}</p>
{/if}

<style>
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 20px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
  }
  .identity {
    display: flex;
    align-items: center;
    gap: 12px;
    min-width: 0;
  }
  .logo {
    height: 24px;
    flex: 0 0 auto;
  }
  /* The stock logo is a white glyph: ink on the light field. An operator's
     own logo (white label) is shown as is. */
  :global(:root[data-theme='light']) .logo:not(.custom) {
    filter: brightness(0);
  }
  .env {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--text-md);
    font-weight: 600;
  }
  .actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 0 0 auto;
  }
  .env-error {
    padding: 6px 20px;
    font-size: var(--text-xs);
    color: var(--red);
    border-bottom: 1px solid var(--border);
  }
  @media (max-width: 640px) {
    .topbar {
      padding: 8px 12px;
    }
    .logo {
      display: none;
    }
  }
</style>
