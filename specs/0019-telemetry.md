# 0019 — Anonymous usage telemetry

**Status:** Adopted (still in force)
**Type:** Product
**First introduced:** `2fc92c8` "release: v1.20.1 — telemetry, ..." (2026-03-11)
**Key code today:** `telemetry.py` (event sending), `server.py` (startup + `env_created` hooks), `settings.py` (`disable_telemetry`)

## Context

Oduflow is self-hosted and runs on the user's own Docker host, so the project
has **no inherent visibility** into whether anyone is using it, which versions
are deployed, or whether installs convert into real use. Product decisions
(what to build, which versions to support, whether the tool is being adopted at
all) were being made blind.

The forces:

- The maintainers need a basic adoption signal — *roughly how many instances
  exist and whether they create environments* — to steer the product.
- The tool handles private repos and databases; telemetry must be obviously
  **non-invasive** and trivially auditable, or it will (rightly) erode trust.
- It must never affect the running tool: no added dependency, no blocking call,
  no failure mode that touches the user's workflow if the network is down.

## Decision

Ship **anonymous, fire-and-forget usage telemetry**, on by default but easily
disabled, that reports only coarse lifecycle events to the project backend.

- **What is sent (and only this):** an event name (`first_run` or
  `env_created`), the Oduflow `version`, and a random per-instance UUID
  (`instance_id`). No repo URLs, branch names, database contents, user
  identities, IP-derived data, or any environment specifics are included.
- **Anonymous by construction.** The `instance_id` is a locally generated random
  UUID persisted in the config dir; it correlates events from the *same install*
  over time but is not derived from and cannot be reversed to any user, host, or
  account.
- **Opt-out, not opt-in, but loud about it.** Telemetry is on by default to get a
  representative signal, controlled by a single `disable_telemetry` flag in
  `oduflow.toml`; the entire module is stdlib-only and open source, so anyone can
  read exactly what leaves the box.
- **Never load-bearing.** Sending runs in a daemon thread with a short timeout
  and swallows all errors. If the endpoint is unreachable, nothing in the tool
  changes.

## How it works (macro)

- **Two events, minimal payload.** On startup, a first-ever run emits `first_run`
  (the UUID is created and stored on that first run); creating an environment
  emits `env_created`. Each is a single JSON POST to the project's usage
  endpoint, sent and forgotten.
- **Backend receives, not the tool.** Events are POSTed to the oduflow.dev usage
  endpoint, where the project's website backend stores them as anonymous usage
  records for aggregate adoption metrics. The client knows nothing about that
  storage; it only fires the event. Described here at macro level only — the
  backend lives in a separate repo.
- **Edge-friendly marker.** Requests carry a branded `User-Agent`
  (`oduflow/<version>`) and a shared `X-Oduflow-Telemetry` marker header. The
  branded UA exists because the default `Python-urllib` signature was being
  blocked by the backend's edge bot-protection (Cloudflare error 1010), which
  silently dropped all events; the marker lets the endpoint cheaply discard
  traffic that does not carry it. The marker is a **speed bump against random
  scanners, not authentication** — the value is in the open-source client.

## Consequences

- The project gains a **basic, honest adoption signal** (install count via
  `first_run`, activity via `env_created`, version spread) without instrumenting
  anything sensitive.
- The privacy stance is defensible and auditable: the payload is three
  non-identifying fields, the code is short and dependency-free, and a single
  config flag turns it off entirely.
- Telemetry is decoupled from the product's reliability — it can fail, be
  blocked, or be disabled with zero functional impact, which keeps it from ever
  becoming a runtime liability.
- It ties Oduflow to the **oduflow.dev backend** as the metrics sink, but only as
  a write-only event target; the tool does not depend on that backend to run.

## History

- `2fc92c8` (2026-03-11) — introduce `telemetry.py`: anonymous `first_run` /
  `env_created` events with version + per-instance UUID, `disable_telemetry`
  flag, daemon-thread fire-and-forget.
- `16b9158` (2026-06-15) — send a branded `User-Agent` (to bypass edge
  bot-protection that was dropping every event) and an `X-Oduflow-Telemetry`
  marker header; no change to the privacy contract or the opt-out flag.
- `#287` (2026-10-03) — add the global `--no-telemetry` CLI flag, a per-run
  override of `disable_telemetry`, so test runs stay out of usage stats
  without editing `oduflow.toml`.
