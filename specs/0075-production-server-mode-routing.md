# Production server mode and bus routing

**Status:** Accepted | **Type:** Runtime / routing model | **First introduced:** 2026-10-03

**Key code:** `docker_ops/production_ops.py` (`SERVER_MODES`,
`prod_routing_labels`, `routing_drift`), `prod_tune.py`, `stack_production.py`,
`extra_addons.generate_odoo_conf`

## Context

Productions ran Odoo's prefork server behind a single Traefik router pointed
at port 8069. Odoo's prefork server always spawns a separate gevent process on
8072 for the live bus, and a prefork worker refuses `/websocket`. As a result,
Discuss and live notifications could not work on any production. Some
workloads (many long-lived or I/O-bound requests) are also better served by
one cooperative gevent process than by a pool of sync workers.

## Decision

Give every production a **server mode**, recorded in the registry and Stack
manifests (`serverMode`), and derive the Traefik routing from it:

- `workers` (default, sync): pages go to the workers on 8069, and a second
  router sends the bus paths (`/websocket`, `/longpolling`) to 8072.
- `gevent` (async): everything goes to 8072. Odoo still runs prefork with one
  HTTP worker, so 8069 and cron keep working.

Routing labels are one function of the record, shared by container creation
and every drift check. The listen ports are Oduflow-managed: they are pinned
in the generated `odoo.conf` over the base conf and refused as overrides.
`workers` must stay ≥ 1, because the threaded server would not listen on 8072.

## How it works (macro)

- **Routing.** Each container gets one Traefik router/service per target
  port. The bus router's longer rule wins on priority. An
  `oduflow.server_mode` label records the mode the container was built for.
- **Tuning.** In gevent mode the auto-tuner keeps the full `db_maxconn` (it
  is the gevent process's pool), drops to one HTTP worker, and raises the
  gevent process's memory limits so it does not recycle the whole site.
- **Health.** A production is healthy only when both 8069 and 8072 answer, so
  a dead gevent process fails a deploy and triggers the code rollback.
- **Convergence of existing productions.** Docker labels are immutable, so
  stale routing needs a recreate. Instead of a startup migration that would
  take every production down at once on upgrade, a container is recreated
  when its routing drifts and it is about to lose service anyway: after a
  deploy that restarted it, on `restart_production`, on `reconfigure_production`
  and on Stack apply/adoption. The default mode is left out of the Stack spec
  hash, so manifests applied before this change do not all show an update.

## Consequences

- The bus works in both modes. Choosing a mode is a per-production
  trade-off: parallel CPU work (workers) versus cheap concurrency (gevent).
- A legacy production keeps its old routing (no bus) until its next deploy,
  restart or reconfigure. The first deploy after the upgrade costs one extra
  container swap.
- Per-production overrides of listen ports saved earlier are ignored, logged
  and purged from the record on the next save or reconfigure.

## History

- Introduced on branch `production/odoo-worker-mode-routing`.
