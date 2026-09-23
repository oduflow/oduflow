# 0054 — Agent-driven container image build and publication

**Status:** Adopted
**Type:** Architecture
**First introduced:** 2026-09-01
**Key code today:** `image_builds.py`, `docker_ops/build_ops.py`, `ImageRegistrySettings` in `settings.py`, MCP tools in `server.py`, allowlist entries in `scoped_access.py`

## Context

Agents working through the scoped `/mcp/<env>` endpoint can develop, test, and
deploy Odoo code, but shipping an auxiliary-service image (a FreeSWITCH build,
a worker sidecar) still required a human with Docker and registry access. The
coder container is deliberately unprivileged — no Docker socket, no
credentials — and that boundary is worth keeping ([[0029-agent-console-and-chat]]).

An earlier, much larger design proposed per-team rootless BuildKit daemons,
packet-level egress policy, durable applied-code state woven through every
environment lifecycle operation, and a registry-aware OCI copy client.
Reviewing it, we concluded that nearly all of that complexity priced in a
hosted **multi-tenant** threat model, while Oduflow's real deployments are
single-trust-boundary: one team (or a few mutually trusting ones) on their own
server, where the agent *already* runs arbitrary code inside environment
containers on the team network. A Dockerfile `RUN` step adds no materially new
capability there. That design is summarised under "Shelved alternative" below.

## Decision

Give environment-scoped agents four MCP tools — `start_image_build`,
`get_image_build`, `publish_image_build`, `cancel_image_build` — implemented as
a thin control plane over the host Docker daemon:

- **Oduflow keeps the socket.** The agent never receives Docker access or
  registry credentials; the server builds and pushes on its behalf.
- **Config is the gate.** The tools work only for teams with a
  `[team.X.image_registry]` section; everyone else gets a clear prerequisite
  error (same pattern as `[production].enabled`). `repository_prefix` is the
  authorization boundary: agents choose any repository and any valid tag —
  including `latest` — below it. Tag naming is deliberately *not* an
  authorization layer; overwrites are last-writer-wins and auditable in the
  job's publication history.
- **The source pin is HEAD-at-admission, not applied-code state.** Under the
  existing environment lock, the managed checkout's `HEAD` commit is exported
  with `git archive` (traversal-safe extraction, size caps) into a sealed
  per-job context. The immutable git object closes the build/pull race without
  any new cross-cutting lifecycle state.
- **Build ownership follows the environment instance.** Jobs store a digest of
  the environment's persistent scoped MCP token, never the token itself. The
  digest survives an environment rename but changes on delete/recreate, so a
  reused name cannot inherit an older instance's builds.
- **The local daemon is the staging registry.** A successful build is tagged
  `oduflow-build/team-<id>:<build-id>`; publishing is `docker tag` +
  `docker push` of that exact image — promote-without-rebuild with zero
  remote-staging machinery. Destination tags are temporary local references
  removed after push; old staging tags are pruned by build age, and untagged
  image objects are removed once Docker reports that no container uses them.
- **Cleanup instead of resume.** Jobs have a bounded-concurrency monitor thread
  and a disposable worker process. The process owns the Docker HTTP connection,
  so cancellation and the wall-clock deadline can terminate even a silent
  Dockerfile step. `job.json` and a size-capped build log persist per team.
  After a restart, a job found non-terminal on disk is lazily marked
  `interrupted` on first access; succeeded builds remain publishable while
  their retained staging image exists. Retry is a new build, cheap via the
  layer cache.
- **Credentials are request-scoped.** `username` + `token` are read from the
  Oduflow config and passed per-push to the Docker API; with neither set, the
  host's own `docker login` is used. There is no daemon-wide login, and the
  credentials are not persisted in build jobs.

## Consequences

- One PR instead of four subsystems; no new daemons, no BuildKit matrix, no
  startup reconciliation pass.
- The trust model is explicit: a Dockerfile is untrusted code with the same
  reach as the team's environment containers. That is acceptable for
  self-hosted single-trust-boundary deployments and **not** for hosted
  multi-tenant ones — the shelved alternative below is the starting point if
  that ever changes.
- Registry-side enforcement matters: Oduflow validates the destination prefix,
  but the operator should still issue a least-privilege token restricted to
  that namespace.
- Multi-platform builds, build secrets, SBOMs, and a dashboard UI are out of
  scope and layer on cleanly later.

## Evolution

- **2026-09-23 — staging images run as auxiliary services.** `create_service`
  and `update_service` accept the local staging tag
  (`oduflow-build/team-<id>:<build-id>`) and resolve it from the daemon instead
  of pulling, so an agent can build, run the result as a service, verify it,
  and only then publish. The tag prefix is checked against the calling team's
  own staging repository: the daemon is shared, and without that check one
  team could run another team's build through these service operations.
  Pruning preserves staging tags referenced by running or stopped containers,
  even when a cached rebuild gives the same image another tag. It shares the
  service registry mutex with creation and recreation, protecting the interval
  between image lookup and container creation. Updates recheck the staging tag
  under that mutex before removing the old container. Before this, the only path
  from a build to a running service went through the registry, and a locally
  configured registry (or none) left the agent stuck.

## Shelved alternative — the multi-tenant builder

Recorded because it is the design to return to if Oduflow is ever hosted for
mutually untrusting tenants. Each pillar and why it was dropped:

- **Per-team rootless BuildKit + packet-level egress policy.** Isolate the
  build from the host Docker API, the application networks, and private /
  link-local / cloud-metadata ranges, gated behind a feasibility spike across
  the supported host matrix. Dropped: it defends against a Dockerfile the
  agent could equivalently run inside its own environment container today, at
  the cost of a whole daemon topology and a per-distro security matrix.
- **Durable applied-code state.** A per-environment record of the last
  revision for which create / branch-switch / `pull_and_apply` fully succeeded,
  advanced only on success and following the environment through rename, reuse
  and deletion — so a build could never source a half-applied revision.
  Dropped: it threads new state through every lifecycle operation, and
  exporting HEAD under the environment lock closes the same race for free. The
  trade-off is real — a build can pin a commit that was pulled but whose Odoo
  actions failed — and it is the agent's job to `pull_and_apply` first.
- **Registry staging repository + digest promotion.** Push a reserved
  write-once candidate tag to the registry, record its content digest, then
  copy that digest to the requested tags with a pinned OCI client, verifying
  each tag by read-back and auditing previous/observed digests. Dropped: the
  local daemon already stores the exact built image, so `docker tag` + push is
  promote-without-rebuild with none of the remote-staging, TTL, retention, or
  copy-client machinery. What is lost is cross-writer race detection — the
  contract is last-writer-wins either way, but we no longer verify the digest a
  tag resolves to afterwards.
- **Named registry profiles.** Several credentialed registry destinations per
  team, reused for private image pulls by auxiliary services. Collapsed to one
  optional section per team; a second destination is an additive change.
- **Phased delivery (spike → single-platform → deployment integration →
  provenance/SBOM/UI).** Phases 2–3 remain a reasonable roadmap: registry auth
  for service pulls, digest-first service create/update, multi-platform builds
  behind binfmt preflight, provenance attestations, signing, and dashboard
  build history.

## History

- 2026-09-01 — introduced (simplified from a 2026-08-31 design after review;
  that document's rationale is folded into this record).
- 2026-09-23 — staging tags accepted by `create_service`/`update_service` (see Evolution).
- 2026-09-02 — registry tokens moved from an environment-variable reference to
  a direct config value, matching how Oduflow stores its other deployment
  credentials while preserving request-scoped Docker authentication.
