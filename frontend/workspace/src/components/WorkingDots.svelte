<!-- The agent is busy. Static under reduced motion (app.css). -->
<span class="dots" aria-hidden="true"><span></span><span></span><span></span></span>

<style>
  .dots {
    display: inline-flex;
    gap: 3px;
    margin-right: 6px;
    vertical-align: middle;
  }
  .dots span {
    width: 5px;
    height: 5px;
    border-radius: 999px;
    background: var(--accent);
    animation: blink 1.2s ease-in-out infinite;
  }
  .dots span:nth-child(2) {
    animation-delay: 0.2s;
  }
  .dots span:nth-child(3) {
    animation-delay: 0.4s;
  }
  @keyframes blink {
    0%,
    80%,
    100% {
      opacity: 0.25;
    }
    40% {
      opacity: 1;
    }
  }
</style>
