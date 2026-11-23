<script lang="ts">
  // The first thing a visitor sees: what this page is for, and a few things
  // to ask for. Picking an example fills the message box; it is not sent.
  let { onpick, ready }: { onpick: (text: string) => void; ready: boolean } = $props();

  const EXAMPLES = [
    'Add a "Tax ID" field to the customer form',
    "Show the salesperson's phone number on printed quotations",
    'Create a report of confirmed sales by month',
    'What can this Odoo do right now? Give me a short tour.',
  ];
</script>

<div class="empty">
  <h1>What would you like to change?</h1>
  <p>
    Describe it in your own words. The agent changes this Odoo environment for you and shows you
    the result. Your changes stay in this environment until your team ships them.
  </p>
  <div class="examples" aria-label="Examples">
    {#each EXAMPLES as example (example)}
      <button type="button" class="example" disabled={!ready} onclick={() => onpick(example)}>
        {example}
      </button>
    {/each}
  </div>
</div>

<style>
  .empty {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 12px;
    width: 100%;
    max-width: 52rem;
    margin: 0 auto;
    padding: 24px 20px;
  }
  h1 {
    font-size: var(--text-xl);
    font-weight: 600;
    line-height: 1.3;
  }
  p {
    max-width: 38rem;
    color: var(--text-dim);
  }
  .examples {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
    margin-top: 8px;
  }
  .example {
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--surface);
    color: var(--text);
    text-align: left;
    cursor: pointer;
    transition: border-color 0.15s, color 0.15s;
  }
  .example:hover {
    border-color: var(--accent);
    color: var(--accent);
  }
  .example:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .example:disabled:hover {
    border-color: var(--border);
    color: var(--text);
  }
  @media (max-width: 640px) {
    .empty {
      padding: 16px 12px;
    }
    .examples {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
