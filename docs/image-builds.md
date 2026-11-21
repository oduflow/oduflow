# Container Image Builds

Oduflow can build a **container image from a pinned git commit**, run it as an
auxiliary service, and publish it to your registry — without handing the agent
a Docker socket or registry credentials. Oduflow keeps both and builds on the
agent's behalf; the agent only names what to build.

The motivating case is a service image: a gateway, a worker sidecar, a
companion API. Its repository does not need to contain any Odoo code — or any
relation to Odoo at all.

A build has one of two sources:

- **a repository URL** — `repo_url` + `ref`, any git repository, no environment
  involved. Available on the team endpoint (`/mcp`) only.
- **an environment** — `env_name`, that environment's managed checkout at its
  current `HEAD`. On the scoped `/mcp/<env>` endpoint this is the only source,
  and it is implied.

**Building needs no configuration.** Publishing needs a registry namespace, and
an operator can switch building off per team — see
[Prerequisites](#prerequisites).

!!! warning "A Dockerfile is untrusted code with the team's reach"
    Builds run on the host Docker daemon, so a `RUN` step reaches the host's
    network the same way the team's own environment containers already do. That
    is acceptable for the self-hosted, single-trust-boundary deployments
    Oduflow targets — one team, or a few mutually trusting ones, on their own
    server. It is **not** an isolation boundary between mutually untrusting
    tenants. An operator who does not want it for a team sets
    `builds_enabled = false`.

## Prerequisites

| To do this | You need |
|---|---|
| Build an image and run it on this host | Nothing. A team with no `[team.X.image_registry]` section builds with the default limits |
| Build Dockerfiles written for current Docker | The `docker` CLI and its `buildx` plugin beside Oduflow — see [Builders](#builders). The official Oduflow image ships both |
| Publish a build to a registry | `repository_prefix` in `[team.X.image_registry]`, plus credentials or a host `docker login` |
| Build from a private repository | The team's stored git credentials ([`setup_repo_auth`](mcp-tools.md#setup_repo_auth)) or its SSH deploy key ([`get_ssh_public_key`](mcp-tools.md#get_ssh_public_key)) |
| Refuse builds for a team | `builds_enabled = false` in that team's section |

The publishing half is two lines of `oduflow.toml`:

```toml
[team.1.image_registry]
repository_prefix = "acme"     # the authorization boundary for pushes
```

That confines every push to `docker.io/acme/*` and uses the host Docker
daemon's own `docker login` credentials. To keep credentials in Oduflow
instead, set `username` and `token` together — they are sent per-push to the
Docker API, never as a daemon-wide login. Full key list, defaults and
validation rules: [Image registry
settings](installation.md#image-registry-settings).

The section is read at startup, so adding it needs an Oduflow restart; the
[Server settings console](admin.md) applies individual field changes live.

## The flow

```
start_image_build ──► queued ──► building ──► succeeded
                                     │            │
                                     │            ├──► create_service / update_service
                                     │            │    (run the staging image, verify it)
                                     │            │
                                     │            └──► publish_image_build
                                     │                 (promote the exact bits)
                                     ├──► failed
                                     ├──► cancelled      (cancel_image_build)
                                     └──► interrupted    (the process running it died)
```

A build is identified by a `build_id` such as `bld-9f3c1a7b22e4`. A repository
build belongs to the **team** and is addressed by `build_id` alone; an
environment build belongs to that environment's identity — it survives a
rename and is dropped when the environment is deleted, so a re-used name never
inherits an older instance's builds — and is addressed with `env_name` as well.

### 1. Build from a repository

```bash
oduflow call start_image_build '{"repo_url": "https://github.com/acme/gateway.git", "ref": "main", "dockerfile": "docker/Dockerfile"}'
```

`ref` is a branch, tag or full commit SHA, and is resolved to a commit with one
`git ls-remote` when the call is admitted; empty means the repository's default
branch. That commit is then fetched shallow and exported into a sealed context,
so the build is pinned no matter what the branch does afterwards.

Private repositories use the team's existing credential store or SSH deploy
key. Do not put a token in `repo_url`: inline credentials are moved into the
store on the MCP path and rejected outright in a Stack manifest, so they never
reach `job.json` or the build log.

### 2. …or from an environment checkout

```bash
oduflow call start_image_build '{"env_name": "feature-x", "dockerfile": "docker/Dockerfile", "context": "."}'
```

```
Image build started.
build_id: bld-9f3c1a7b22e4
Source: branch 'feature-x' @ 4f21ac9b1d77 (dockerfile docker/Dockerfile, context .)
Timeout: 1800s
```

Here the source is the checkout's **HEAD at call time**, exported while the
environment lock is held, so it cannot shift mid-build.

!!! warning "Push and sync first"
    An environment build pins HEAD of the *managed checkout*, not your laptop.
    Push your commits and run [`pull_and_apply`](mcp-tools.md#pull_and_apply)
    before building, or you will build the previous revision.

Either way the call returns immediately and the build runs in the background.
`dockerfile` is resolved relative to `context`, `context` relative to the
repository root, and both must stay inside the tree. A missing Dockerfile fails
before any Docker work.

!!! note "What the context contains"
    `git archive` of the pinned commit: tracked files only — no `.git`
    directory, no submodule contents, no Git LFS objects.

!!! warning "Build arguments are not secret"
    `build_args` values reach the Dockerfile and the image history. Never pass
    credentials. Build secrets are not supported.

### 3. Watch it

```bash
oduflow call get_image_build '{"build_id": "bld-9f3c1a7b22e4", "tail_lines": 50}'
```

Reports status, the source commit, which builder ran, the publication history
and a tail of the build log. Poll until the status is terminal. A hung build
can be stopped with `cancel_image_build`, which ends the builder's process
group even when the current Dockerfile step prints nothing.

### 4. Run it before publishing it

A succeeded build stays in the host's Docker daemon under a **staging tag**:

```
oduflow-build/team-<team-id>:<build-id>
```

`get_image_build` reports it as the local image. Pass it as the `image` of
[`create_service`](mcp-tools.md#create_service) or
[`update_service`](mcp-tools.md#update_service) and it is resolved from the
local daemon instead of being pulled — so the build can run as a real service,
reachable on the team network and through Traefik, before anything reaches a
registry:

```bash
oduflow call create_service '{"name": "gateway", "image": "oduflow-build/team-1:bld-9f3c1a7b22e4", "port": 8080}'
```

Only the owning team's staging repository is accepted: the Docker daemon is
shared between teams, and that prefix check is what keeps one team from running
another team's build. For an image that only ever runs on this host, this is
the whole story — no registry is involved. See [Auxiliary
Services](services.md#running-an-image-build-before-publishing-it).

### 5. Publish

Publishing is `docker tag` + `docker push` of the **exact image that was
built** — never a rebuild, so the bits you verified are the bits you ship.

```bash
oduflow call publish_image_build '{"build_id": "bld-9f3c1a7b22e4", "repository": "gateway", "tags": "1.4.0,latest"}'
```

```
Publish results for build bld-9f3c1a7b22e4 (docker.io/acme/gateway):
  ✓ 1.4.0: sha256:7c0d…
  ✓ latest: sha256:7c0d…
```

- The destination is always `{host}/{repository_prefix}/{repository}:{tag}`.
  `repository` is relative to the configured prefix and cannot escape it.
- Any syntactically valid tag is accepted, including `latest` and semver. Tag
  naming is deliberately **not** an authorization layer: overwriting a tag is
  intentional and last-writer-wins, and every publish is recorded in the job's
  publication history.
- Up to 10 tags per call, pushed one at a time. **Partial success is possible**
  — each tag reports its own outcome and the call is retryable per tag.
- Temporary local tags created for the push are removed afterwards.

A published image is an ordinary registry image: use it as the `image` of a
service on any host, or as the `odoo_image` of an environment or production.

## In a Stack

A [Stack](stacks.md) service can declare `build` in place of `image`, which
makes the whole flow above declarative:

```yaml
services:
  gateway:
    build:
      repoUrl: https://github.com/acme/gateway.git
      ref: main
      dockerfile: docker/Dockerfile
    port: 8080
```

Each build carries a **build key** — a digest of the repository URL, commit,
Dockerfile, context, target and arguments. A plan looks for a finished build
with the same key, so re-applying an unmoved ref is a no-op, while a moved
branch rebuilds and updates the service. Builds run before any other change
and before apply takes the team lock, one after another. Details and the full
field list: [Building a service
image](stacks.md#building-a-service-image).

## Builders

With the `docker` CLI and its `buildx` plugin installed next to Oduflow, the
build is a `docker buildx build` subprocess — **BuildKit**, the builder current
Dockerfiles expect. The CLI is pointed at the same daemon the rest of Oduflow
uses, so the image it loads is the one services can see.

Without the plugin, Oduflow falls back to the Docker Engine's legacy builder
and says so in the build log. The fallback fails on Dockerfiles that use
`$BUILDPLATFORM`, `RUN --mount` or `COPY --link`. `get_image_build` reports
which builder ran, so a surprising failure is quick to attribute. The
availability check runs per build, so installing the plugin takes effect
without restarting Oduflow.

## Limits and retention

Every bound is per team, in `[team.X.image_registry]`; a team without the
section gets these defaults:

| Bound | Default | What happens at the limit |
|---|---|---|
| `max_concurrent_builds` | `2` | A further build is refused as busy. MCP, standalone CLI and Stack builds share the team's slots |
| `build_timeout_seconds` | `1800` | Hard wall-clock deadline, fetching the source included; the build is terminated and the job fails |
| `max_context_mb` | `512` | The sealed context is refused before the build starts (a fixed 200 000-file ceiling also applies) |
| `max_log_mb` | `16` | The persisted log is truncated with a marker; the build continues |
| `keep_images` | `10` | Older staging images are untagged after each successful build |

Pruning keeps the staging tags of the newest `keep_images` succeeded builds and
never removes a tag that a running **or stopped** container references, so a
service built on a staging image keeps working. Underlying image objects are
removed only once Docker reports no container uses them. `keep_images = 0`
disables pruning.

**Build records outlive their images.** Jobs live under
`{data_dir}/team_{ID}/image_builds/<build-id>/` (`job.json` plus `build.log`)
and are removed only by [`oduflow cleanup
--force`](cli.md#maintenance-commands) or the dashboard's **Cleanup**: records
of failed, cancelled and interrupted builds, and of succeeded builds whose
staging image is gone. A running build, and a succeeded build whose image is
still present, are always kept — a Stack reuses it and publishing needs it. The
sealed build context is deleted as soon as the job reaches a terminal state.

The dashboard's cleanup can additionally prune the Docker **build cache**, as
its own opt-in. That cache is shared by every team on the server, so the next
build of any image runs without it — which is exactly what makes a retried
build cheap. Prefer lowering `keep_images`.

## Ownership and restarts

Builds are never resumed. The server, a detached `oduflow call` worker, or
`oduflow stack apply` holds the job's file lock until the build finishes. If
the owner exits early, the kernel releases the lock and Oduflow reports the
unfinished job as `interrupted` the next time it is inspected.

Succeeded builds stay publishable while their staging image exists. Retrying is
simply a new build — the layer cache usually makes it cheap. Use
`cancel_image_build` with the build ID even if another process owns the build,
including a running `oduflow stack apply`.

## Out of scope

Multi-platform images, build secrets, submodules and Git LFS in the context,
image signing, SBOM/provenance attestations, registry authentication for
*pulling* private service images, and webhook-triggered rebuilds are all
deliberately absent. See
[`specs/0054`](https://github.com/oduflow/oduflow/blob/main/specs/0054-agent-container-image-build-and-publish.md)
and
[`specs/0085`](https://github.com/oduflow/oduflow/blob/main/specs/0085-repository-image-builds.md)
for the reasoning, and for the multi-tenant design that was shelved.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| *"Container image builds are disabled for team …"* | `builds_enabled = false`. An operator has to turn it back on |
| *"No container registry is configured for this team"* | Building worked, publishing needs `repository_prefix` in `[team.X.image_registry]` |
| *"already has N active image build(s)"* | `max_concurrent_builds` reached. Wait, cancel a build, or raise the bound |
| Dockerfile fails on `$BUILDPLATFORM`, `RUN --mount` or `COPY --link` | The legacy builder ran. Install the `docker` CLI with its `buildx` plugin beside Oduflow — see [Builders](#builders) |
| *"Build context exceeds the configured limit"* | The commit tree is bigger than `max_context_mb`. Narrow `context` to a subdirectory or raise the bound |
| *"dockerfile … not found under context … at commit"* | The path is resolved **in the committed tree**. Commit and push it first (for an environment build, then `pull_and_apply`) |
| A submodule or LFS file is missing in the image | The context is `git archive`: tracked files only, no submodule contents, no LFS objects. Vendor what the build needs, or fetch it in the Dockerfile |
| The ref cannot be resolved, or the clone is refused | For a private repository, store credentials with `setup_repo_auth` or add the team's SSH deploy key to it. Never put a token in `repo_url` |
| An environment build used the wrong code | HEAD of the managed checkout was behind. `pull_and_apply`, then build again |
| Status is `interrupted` | The process running the build died (an Oduflow restart, or an interrupted `stack apply`). Start a new build |
| *"The staging image … is no longer in the local Docker daemon"* | Retention or a manual `docker rmi` removed it. Run `start_image_build` again |
| *"Staging builds can only be used by the team that built them"* | The staging tag belongs to another team. Use the tag reported for your own build |
| A push fails with authentication or permission errors | Either the daemon's `docker login` or the configured `username`/`token` lacks rights. The token must be able to write every repository below `repository_prefix` |
| Some tags published, others failed | Expected — tag writes are not a transaction. Re-call `publish_image_build` with the failed tags |

Host-side symptoms — a job stuck with no log progress, builds filling the disk,
reading the full log — are covered in
[Troubleshooting](troubleshooting.md#an-image-build-fails-or-hangs).

## Security notes

- The agent never receives Docker access or registry credentials; Oduflow
  builds and pushes on its behalf.
- `repository_prefix` is the authorization boundary for publishing, enforced by
  Oduflow. Still issue a **least-privilege registry token** restricted to that
  namespace — defence in depth if the config is ever misread.
- The token is stored in `oduflow.toml`. Protect the file (mode 0600) and
  prefer a token you can revoke over an account password.
- The scoped `/mcp/<env>` endpoint can build **only its own environment's
  checkout**: it neither lists nor accepts `repo_url`/`ref`, so choosing an
  arbitrary repository stays a team-level capability. Where a registry is
  configured, an environment-scoped credential can publish within the team's
  namespace. See [Authentication &
  Security](security.md#scoped-single-environment-access-mcpenv).
