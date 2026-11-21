# 0085 — Image builds from a repository, without an environment

**Status:** Adopted
**Type:** Architecture
**First introduced:** this change (2026-11-19)
**Key code:** `docker_ops/build_ops.py` (git source, BuildKit runner), `image_builds.py` (team-owned jobs, build keys), `stack_models.py` / `stack_ops.py` (`build` on a service), `scoped_access.py` (hidden parameters), `Dockerfile` (docker CLI + buildx)

## Context

[[0054-agent-container-image-build-and-publish]] built images from an
*environment's* checkout, and only for teams with a registry configured.
[[0084-service-only-stacks]] then made it normal to run services that have
nothing to do with Odoo. The two did not meet: to build such a service — Paseo
was the case in hand — one had to create an Odoo environment out of a
repository with no Odoo code, purely to obtain a pinned git checkout.

Looking at what the build takes from the environment showed three things, none
of them Odoo: a place to find the repository, an owner for the job, and a lock
that keeps `HEAD` still while the context is sealed. Two further obstacles
surfaced on the same attempt. The Docker SDK drives the Engine's legacy
builder, so Paseo's `FROM --platform=$BUILDPLATFORM` failed outright, and no
build argument can supply a variable the legacy builder ignores unless the
Dockerfile declares it. And an image meant only to run on this host still
required a registry section.

## Decision

- **A build source is a pinned commit, however it was found.** Next to
  `env_name`, `start_image_build` takes `repo_url` + `ref`. The ref is resolved
  with `git ls-remote` when the call is admitted, the commit is fetched shallow
  by SHA into the job, and from there the path is the existing one:
  `git archive` into a sealed context. No environment, checkout or lock exists
  for such a build.
- **Such a job belongs to the team.** Environment builds stay bound to the
  environment's identity; repository builds carry a fixed team owner and are
  addressed without `env_name`. The scoped `/mcp/<env>` endpoint neither lists
  nor accepts `repo_url`/`ref`: a sandboxed agent builds its own checkout, and
  choosing an arbitrary repository stays a team-level capability.
- **BuildKit is the builder; the legacy builder is the fallback.** With the
  docker CLI and its buildx plugin present, the build is a
  `docker buildx build` subprocess in its own process group, so cancellation
  and the deadline still end a silent step. Without them the previous SDK path
  runs and says so in the build log. The Oduflow image now ships both.
- **Building needs no configuration; publishing needs a registry.**
  `[team.X.image_registry]` is no longer the gate for building. Absent, builds
  use default limits; `repository_prefix` became optional and gates only
  `publish_image_build`. The section keeps one switch for the operator:
  `builds_enabled = false` refuses new builds for the team, on both endpoints
  and in a Stack.
- **A Stack service may declare `build` instead of `image`.** The planner
  resolves the ref and looks for a finished build with the same inputs; apply
  builds what is missing — before any other mutation, and before it takes the
  team lock — and runs the staging tag.
- **A Stack's builds run one after another, by design.** Building them in
  parallel, up to `max_concurrent_builds`, was considered and deferred. Builds
  no longer hold the team lock, so a sequence costs only apply's own wall time;
  a typical Stack has one or two build services; BuildKit already parallelises
  the steps of one build, and several builds at once compete for the host's
  CPU, memory, disk and network; and it would need Ctrl-C handling across
  threads and per-service progress output. If revisited, a failed build should
  let the others finish, so a retry reuses them, rather than fail fast.

## How it works (macro)

A build key (digest of repository URL, commit, Dockerfile, context, target and
arguments) is stored on each repository job. It is how a Stack recognises that
the image it needs already exists, which makes a re-apply with an unmoved ref a
no-op and a moved branch a rebuild followed by a service update. The staging
tag a service runs is already protected from retention by the rule from 0054,
so no new lifecycle state was needed. An apply finishes every build before it
creates the first service, so for that interval it names the tags it is about
to run and the retention pass after each build leaves them alone.

Apply plans twice. The first plan is unlocked and only decides what to build;
a build can last half an hour, and the team lock refuses every environment
operation of the team while it is held. The second plan, under the lock, is the
authority: it finds the fresh builds by commit, and builds on the spot only
when a ref moved in between.

A build runs in whichever process admitted it — the server, or an
`oduflow stack apply` command beside it. That process holds a file lock in the
job directory for as long as the job runs, and the kernel releases it if the
process dies. It is what lets the server's lazy cleanup tell a build in
progress elsewhere from an abandoned one before it declares the job
interrupted and removes its context. While it runs, a build reports on the
server log once a minute: its output goes to the job's own log, and a Stack
applied at server start would otherwise look to the startup watchdog like a
hung process.

Job directories outlive their builds and are removed only by cleanup
(`oduflow cleanup`, the dashboard's Cleanup). It deletes failed, cancelled and
interrupted jobs, and succeeded ones whose staging image has left the daemon.
A job still recorded as running whose owner lock is free was abandoned by a
dead process: the preview lists it, and removal first declares it interrupted,
as the lazy cleanup would. A job whose owner lock is held is never touched,
and a succeeded job whose image is present stays — a Stack reuses it and
publishing needs it. Each job is judged again right before its state is
rewritten or its directory deleted. The dashboard can also prune the daemon's
build cache; it is shared by all teams, so it is a separate opt-in there and
absent from the CLI, like unused images.

The docker CLI is pointed at the daemon the Docker SDK uses (`DOCKER_HOST`, or
the default socket) rather than at its current context, so the image BuildKit
loads is the one the rest of Oduflow can see.

Private repositories go through the team's existing credential store and SSH
deploy key. Inline URL credentials are moved to the store on the MCP path and
rejected in a manifest, so they never reach `job.json` or the build log.

## Alternatives rejected

- **An environment without Odoo.** Every layer of an environment — database,
  template, filestore, modules, most scoped tools — is Odoo-shaped. The result
  would have been a shell in which only git worked.
- **Reusing the extra-addons repository registry as the source list.** It adds
  a registration step and puts unrelated repositories in every add-on picker.
- **Requiring Dockerfiles to declare `ARG BUILDPLATFORM`.** It works, but asks
  every upstream repository to accommodate a builder Docker has deprecated.

## Consequences

- A service repository goes from URL to running container in one tool call or
  one manifest, and Dockerfiles written for current Docker build unchanged.
- Scoped agents can now build on any team, not only on teams whose operator
  added a registry section. 0054 already judged a Dockerfile `RUN` to add no
  capability beyond the code those agents run in their environment; publishing
  remains opt-in, and building is opt-out per team (`builds_enabled`).
- Stack `plan` and `status` make a network call per declared build. An apply
  with builds makes that call twice and waits for its builds, but not while
  holding the team lock; if the lock turns out busy afterwards, the retry
  reuses the builds.
- A build started by `oduflow stack apply` runs in the CLI process, so only
  that process can cancel it; it is not resumed if the CLI is interrupted.
  `max_concurrent_builds` is counted per process, so such a build does not
  take one of the server's slots.
- Build arguments are passed to the builder as command-line arguments. They
  are documented as non-secret and end up in the image history regardless.
- Still out of scope: multi-platform images, build secrets, submodules and LFS
  in the context, and webhook-triggered rebuilds.

## History

- 2026-11-19 — introduced.
