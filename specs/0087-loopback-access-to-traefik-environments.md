# 0087 — Docker-host access to Traefik development environments

**Status:** Accepted

**Type:** Environment addressing

**First introduced:** 2026-11-23

**Key code:** `docker_ops/env_ops.py`, `port_registry.py`, `server.py`

## Context

A coding agent on the Docker host can reach Oduflow's MCP endpoint directly.
After provisioning an environment, the agent also needs an Odoo URL for HTTP
requests and browser tests. Requiring a working tunnel for this step makes
local development depend on public ingress.

## Decision

Give every Traefik development environment a second address on IPv4 loopback.
The public URL continues to serve remote users. Both addresses lead to the
same Odoo process and database.

## How it works

Docker publishes the environment's Odoo HTTP port on a stable team port from
[[0004-stable-addressing-port-registry-and-traefik]]. The reservation follows
the environment through rename and recreation. True deletion frees it.
MCP and the dashboard API expose both addresses. The dashboard displays the
port number so it can also be copied from a remote browser.

## Consequences

Each development environment consumes one team port, including while it is
stopped. An existing Traefik environment gains local access when its container
is next replaced. Operators can use `update_environment` to do this while
retaining the database and filestore.

## History

- 2026-11-23 — introduced on `litnimax/localhost-traefik`.
