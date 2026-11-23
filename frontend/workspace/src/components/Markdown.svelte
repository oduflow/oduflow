<script lang="ts">
  // Agent markdown, sanitized (lib/markdown). A link into this environment's
  // Odoo is opened through Connect As so it lands signed in: the visitor of a
  // share link has no Odoo password of their own.
  import { getContext } from 'svelte';
  import { renderMarkdown } from '../lib/markdown';
  import { odooPath, openOdoo } from '../lib/odoo';

  let { text, dim = false }: { text: string; dim?: boolean } = $props();

  const workspace = getContext<{ envName: string; envUrl: string } | undefined>('workspace');
  let html = $derived(renderMarkdown(text));

  function onclick(event: MouseEvent) {
    if (!workspace || event.defaultPrevented || event.button !== 0) return;
    const link = (event.target as Element).closest('a');
    if (!link) return;
    const path = odooPath(link.getAttribute('href') || '', workspace.envUrl);
    if (path === null) return;
    event.preventDefault();
    openOdoo(workspace.envName, path);
  }
</script>

<!-- Link clicks are delegated; the links themselves stay keyboard reachable. -->
<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div class="md" class:dim {onclick}>{@html html}</div>

<style>
  .dim {
    color: var(--text-dim);
  }
</style>
