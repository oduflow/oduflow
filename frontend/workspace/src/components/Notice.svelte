<script lang="ts">
  import type { Snippet } from 'svelte';

  let { title, body, children }: { title: string; body: string; children?: Snippet } = $props();
</script>

<section class="notice" role="status">
  <h2>{title}</h2>
  <p>{body}</p>
  {#if children}<div class="actions">{@render children()}</div>{/if}
</section>

<style>
  .notice {
    margin: auto;
    max-width: 30rem;
    padding: 24px;
    text-align: center;
  }
  h2 {
    font-size: var(--text-lg);
    font-weight: 600;
    margin-bottom: 6px;
  }
  p {
    color: var(--text-dim);
  }
  .actions {
    margin-top: 14px;
    display: flex;
    justify-content: center;
    gap: 8px;
  }
</style>
