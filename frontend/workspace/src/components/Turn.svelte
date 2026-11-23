<script lang="ts">
  // One agent reply. The answer is shown in full; everything the agent did to
  // get there (tool calls, reasoning, its plan, interim notes) is folded into
  // one "Worked · N steps" line a curious reader can open. The last
  // screenshot the agent took is lifted out under the answer: that is the
  // result the user came to see.
  import { pluralCount } from '../lib/format';
  import {
    latestTurnImage,
    splitTurn,
    summarizeActivity,
    type Transcript,
    type TurnItem,
  } from '../lib/transcript';
  import Markdown from './Markdown.svelte';
  import ToolCall from './ToolCall.svelte';
  import WorkingDots from './WorkingDots.svelte';

  let {
    turn,
    transcript,
    onimage,
  }: { turn: TurnItem; transcript: Transcript; onimage: (src: string) => void } = $props();

  let view = $derived(splitTurn(turn));
  let summary = $derived(summarizeActivity(transcript, view.activity));
  let image = $derived(latestTurnImage(transcript, turn));
  let open = $state(false);

  // A failed step opens the activity, so a failure is never hidden.
  let seenFailures = 0;
  $effect(() => {
    if (summary.failed > seenFailures) open = true;
    seenFailures = summary.failed;
  });

  let label = $derived.by(() => {
    const parts = [];
    if (summary.steps) parts.push(pluralCount(summary.steps, 'step', 'steps'));
    if (summary.notes) parts.push(pluralCount(summary.notes, 'note', 'notes'));
    return (turn.done ? 'Worked' : 'Working') + (parts.length ? ' · ' + parts.join(', ') : '');
  });
</script>

<article class="turn">
  {#if view.activity.length}
    <details class="activity" bind:open>
      <summary>
        {#if !turn.done}<WorkingDots />{/if}
        <span>{label}</span>
        {#if summary.failed}
          <span class="state failed">· {summary.failed} failed</span>
        {/if}
      </summary>
      {#if open}
        <div class="activity-body">
          {#each view.activity as entry (entry.id)}
            {#if entry.type === 'tool'}
              {#if transcript.tools[entry.toolId]}
                <ToolCall tool={transcript.tools[entry.toolId]} />
              {/if}
            {:else if entry.type === 'thought'}
              <details class="thought">
                <summary>Reasoning</summary>
                <p>{entry.text}</p>
              </details>
            {:else if entry.type === 'plan'}
              <div class="plan">
                <div class="plan-title">Plan</div>
                <ol>
                  {#each entry.entries as step, i (i)}
                    <li class:done={step.status === 'completed'} class:active={step.status === 'in_progress'}>
                      {step.content}
                      {#if step.status === 'in_progress'}<span class="sr-only">(in progress)</span>{/if}
                      {#if step.status === 'completed'}<span class="sr-only">(done)</span>{/if}
                    </li>
                  {/each}
                </ol>
              </div>
            {:else}
              <Markdown text={entry.text} dim />
            {/if}
          {/each}
        </div>
      {/if}
    </details>
  {/if}
  {#if view.answer}
    <Markdown text={view.answer.text} />
  {/if}
  {#if image}
    <figure class="shot">
      <button type="button" class="shot-button" onclick={() => onimage(image)} aria-label="Enlarge screenshot">
        <img src={image} alt="Screenshot the agent took to check its work" />
      </button>
      <figcaption>Screenshot from the agent's check</figcaption>
    </figure>
  {/if}
</article>

<style>
  .turn {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
  }
  .activity {
    color: var(--text-dim);
  }
  .activity > summary {
    width: fit-content;
    max-width: 100%;
    padding: 2px 6px;
    margin-left: -6px;
    border-radius: var(--radius-sm);
    font-size: var(--text-xs);
    cursor: pointer;
  }
  .activity > summary:hover {
    color: var(--text);
    background: var(--subtle);
  }
  .state {
    font-weight: 600;
  }
  .state.failed {
    color: var(--red);
  }
  .activity-body {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
    padding: 8px 0 4px 14px;
    margin-top: 4px;
    border-left: 1px solid var(--border);
  }
  .thought {
    font-size: var(--text-xs);
  }
  .thought summary {
    width: fit-content;
    font-weight: 600;
    cursor: pointer;
  }
  .thought p {
    margin-top: 6px;
    white-space: pre-wrap;
    font-family: var(--font-mono);
  }
  .plan {
    font-size: var(--text-xs);
  }
  .plan-title {
    font-weight: 600;
    margin-bottom: 4px;
  }
  .plan ol {
    padding-left: 1.4em;
  }
  .plan li.done {
    text-decoration: line-through;
  }
  .plan li.active {
    color: var(--text);
    font-weight: 600;
  }
  .shot {
    display: flex;
    flex-direction: column;
    gap: 4px;
    width: fit-content;
    max-width: 100%;
  }
  .shot-button {
    display: block;
    padding: 0;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--surface);
    overflow: hidden;
    cursor: zoom-in;
  }
  .shot-button:hover {
    border-color: var(--border-raised);
  }
  .shot img {
    display: block;
    max-width: 100%;
    max-height: 22rem;
    object-fit: contain;
  }
  figcaption {
    color: var(--text-dim);
    font-size: var(--text-2xs);
  }
</style>
