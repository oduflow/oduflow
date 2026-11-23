# 0087 — Chat-first workspace for shared environments

**Status:** Adopted
**Type:** Web capability / Frontend delivery
**First introduced:** 2026-10-10
**Key code:** `frontend/workspace/` (Svelte 5 + Vite source; committed build in `templates/static/workspace/`), `web_ui.py` (`_render_workspace`, `scoped_env_page` `?view=console`, `/static/{path}`, `connect-open` `?next=`), `templates/static/acp-client.js` (shared ACP wire client), `templates/static/chat.js` (context stripping), `.github/workflows/frontend.yml`

## Context

[[0055-scoped-environment-ui-sharing]] made one environment shareable with a
client in a browser, and named the point of it: "a client grows their Odoo by
talking to the agent". But the page it serves is the operator dashboard reduced
to one card. The chat sits in the card's More menu and opens as a modal over a
wall of engineering controls: logs, consoles, module dialogs, MCP credentials.
For the person a share link is for, often not a developer, it reads as an
engineer's console with a chat hidden in it. Tools such as Lovable show the
opposite shape: the conversation is the product, and you start typing.

The dashboard chat (`chat.js`) is also a hand-built DOM window manager. Growing
it into a whole page by hand would cost more than the page is worth.

## Decision

`/env/<name>` serves a new **chat-first workspace**. The existing scoped
dashboard stays reachable from it as the **Engineer view** (`?view=console`).

- **Same boundary, new face.** The workspace only calls endpoints the share
  allowlist (`ui_scope`) already grants, so the server-side scope is unchanged.
  This deliberately ends 0055's "one page, two faces": the dashboard and the
  workspace now differ in layout and audience. They share the API, the session
  store and the ACP client, not the markup.
- **No live preview.** A Lovable-style split with Odoo in an iframe was
  considered and rejected. Odoo's web client sends `X-Frame-Options: DENY`
  (16–18), so embedding it would mean stripping the environments' framing
  protection through per-environment Traefik labels, and it could not work in
  port mode at all. The Odoo backend at half width is also cramped. Instead, the
  result comes to the chat: "Open Odoo" signs in through Connect As in a new tab;
  links the agent writes into the environment open through `connect-open?next=`
  so they land signed in; and the last screenshot the agent took
  ([[0037-built-in-agent-browser-and-noninteractive-codex]]) is shown under its
  answer.
- **Standing instructions for the agent.** ACP has no portable per-session
  system prompt, so the first prompt of each workspace conversation carries a
  fenced text block. It tells the agent that the user is probably not a
  developer, to apply changes itself, and to end with a summary, a screenshot
  and a link. Both chats strip the block when they show a message, including on
  replay.
- **Svelte, own components, Node only for development.** The workspace is an
  application (streaming, approvals, uploads), not content, so Svelte 5 + Vite
  was chosen over Astro, and plain Vite over SvelteKit because the server is
  Python. Ready-made chat kits were weighed: the strongest ACP-aware ones are
  React (acp-components, assistant-ui), and the framework-agnostic ones model
  request/response chat, not ACP's tool calls and permission requests. So the
  components are our own, ported from `chat.js`. The bundle imports
  `static/acp-client.js`, so there is one ACP client. The build is committed
  into the package: installing and running Oduflow still needs no Node. CI
  rebuilds it and fails on any diff.

## How it works (macro)

- `scoped_env_page` keeps its authentication (share key → scoped cookie, or an
  operator session) and renders the built `index.html`. It fills in the
  environment name and whether the viewer is a share-link visitor or an
  operator: the latter gets "Back to dashboard" instead of "Leave". Operators
  open it from the card's **Workspace** item.
- The page polls the filtered environment list and resumes the same durable
  per-(environment, agent) ACP session the dashboard chat uses
  ([[0029-agent-console-and-chat]]), so a conversation continues in either.
  Tool calls, reasoning, the plan and interim notes fold into one
  "Worked · N steps" line under the answer. A failed step opens it.
- Bundle assets carry a content hash and are served from `/static/workspace/`
  as immutable. The dashboard's `CHAT_V` cache-version rule does not apply to
  them. `/static/` now accepts nested paths behind the same containment check.
- `connect-open` and the cross-subdomain landing
  ([[0036-cross-subdomain-connect-as-landing]]) accept `next` only as a path on
  the environment's own host, validated at both ends, so it cannot become an
  open redirect.

## Consequences

- A share link now opens straight into "What would you like to change?". The
  engineering surface is one menu item away for whoever needs it.
- Two chat UIs exist, the dashboard modal and the workspace, and the session
  lifecycle and tool-output reading are ported rather than shared. The shared
  ACP client and tests on both sides contain the drift. Moving the dashboard
  chat onto the Svelte components is the natural next step, if it pays.
- Contributors who touch the workspace need Node 22 and must commit the
  rebuilt bundle. Python-only contributors and users are unaffected.
- No new security surface: the allowlist is unchanged and `next` is path-only.
  The agent the workspace drives is the same one Agent Chat drives: since
  [[0079-per-environment-agent-identities]] it runs as the environment's own
  uid in the team container, sharing only what that record lists (git
  credentials, provider credentials, the network).

## History

- 2026-10-10 — workspace at `/env/<name>`, Engineer view, card "Workspace"
  item, `connect-open?next=`, workspace context block, frontend CI.
