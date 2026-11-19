# Container Image Builds

Oduflow can build a **container image from an environment's git revision** and
publish it to the team's registry namespace — without handing the agent a
Docker socket or registry credentials.

The motivating case is an auxiliary-service image: a FreeSWITCH build, a worker
sidecar, a companion API that lives in the same repository as the Odoo modules.
Before this existed, shipping one required a human with Docker and push rights.
Now an agent working through the scoped `/mcp/<env>` endpoint can build it, run
the result as a [service](services.md), verify it, and promote exactly those
bits to the registry.

The capability is **off until an operator configures it**. Teams without a
`[team.X.image_registry]` section get a clear prerequisite error from all four
tools.

!!! warning "A Dockerfile is untrusted code with the team's reach"
    Oduflow builds on the host Docker daemon. A `RUN` step reaches the host's
    network the same way the team's environment containers already do. That is
    acceptable for the self-hosted, single-trust-boundary deployments Oduflow
    targets — one team, or a few mutually trusting ones, on their own server.
    It is **not** an isolation boundary for mutually untrusting tenants.

## Prerequisites

```toml
[team.1.image_registry]
repository_prefix = "acme"     # required — the authorization boundary
```

That single key enables the tools for team 1 and confines every push to
`docker.io/acme/*`. Pushes then use the host Docker daemon's own `docker login`
credentials. To keep credentials in Oduflow instead, set `username` and `token`
together — they are sent per-push to the Docker API, never as a daemon-wide
login. Full key list and defaults: [Image registry
settings](installation.md#image-registry-settings).

The section is read at startup, so adding it needs an Oduflow restart; the
[Server settings console](admin.md) applies individual field changes live.

There is **no dashboard UI** for builds — they are driven over MCP, or locally
with `oduflow call`.

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
                                     └──► interrupted    (Oduflow restarted mid-build)
```

A build is identified by a `build_id` such as `bld-9f3c1a7b22e4` and belongs to
the environment that started it. Ownership follows the environment *instance*:
it survives a rename, and is dropped when the environment is deleted, so a
re-used name never inherits an older instance's builds.

### 1. Build from the current revision

The source is the environment checkout's **HEAD commit at call time**, exported
with `git archive` while the environment lock is held. The sealed copy cannot
shift mid-build, so a later `pull_and_apply` never changes what is being built.

```bash
oduflow call start_image_build '{"env_name": "feature-x", "dockerfile": "docker/Dockerfile", "context": "."}'
```

```
Image build started.
build_id: bld-9f3c1a7b22e4
Source: branch 'feature-x' @ 4f21ac9b1d77 (dockerfile docker/Dockerfile, context .)
Timeout: 1800s
```

The call returns immediately; the build runs in a background worker process.

!!! warning "Push and sync first"
    The build pins HEAD of the *managed checkout*, not your laptop. Push your
    commits and run [`pull_and_apply`](mcp-tools.md#pull_and_apply) before
    building, or you will build the previous revision.

!!! warning "Build arguments are not secret"
    `build_args` values reach the Dockerfile and the image history. Never pass
    credentials. Build secrets are not supported.

`dockerfile` is resolved relative to `context`, `context` relative to the
repository root, and both must stay inside the checkout. A missing Dockerfile
or a context that is not a directory at that commit fails immediately, before
any Docker work.

### 2. Watch it

```bash
oduflow call get_image_build '{"env_name": "feature-x", "build_id": "bld-9f3c1a7b22e4", "tail_lines": 50}'
```

Reports status, the source commit, the publication history, and a tail of the
build log. Poll this until the status is terminal. A hung build can be stopped
with `cancel_image_build`, which terminates the worker and its Docker
connection even when the current Dockerfile step prints nothing.

### 3. Run it before publishing it

A succeeded build stays in the host's Docker daemon under a **staging tag**:

```
oduflow-build/team-<team-id>:<build-id>
```

`get_image_build` reports it as `Local image`. Pass it as the `image` of
[`create_service`](mcp-tools.md#create_service) or
[`update_service`](mcp-tools.md#update_service) and it is resolved from the
local daemon instead of being pulled — so the build can be smoke-tested as a
real service before anything reaches a registry:

```bash
oduflow call create_service '{"name": "worker", "image": "oduflow-build/team-1:bld-9f3c1a7b22e4", "port": 8099}'
```

Only the owning team's staging repository is accepted here: the Docker daemon
is shared between teams, and that prefix check is what keeps one team from
running another team's build.

### 4. Publish

Publishing is `docker tag` + `docker push` of the **exact image that was
built** — never a rebuild, so the bits you verified are the bits you ship.

```bash
oduflow call publish_image_build '{"env_name": "feature-x", "build_id": "bld-9f3c1a7b22e4", "repository": "worker", "tags": "1.4.0,latest"}'
```

```
Publish results for build bld-9f3c1a7b22e4 (docker.io/acme/worker):
  ✓ 1.4.0: sha256:7c0d…
  ✓ latest: sha256:7c0d…
```

- The destination is always `{host}/{repository_prefix}/{repository}:{tag}`.
  `repository` is relative to the configured prefix and cannot escape it.
- Any syntactically valid tag is accepted, including `latest` and semver. Tag
  naming is deliberately **not** an authorization layer: overwriting an
  existing tag is intentional and last-writer-wins, and every publish is
  recorded in the job's publication history.
- Up to 10 tags per call, pushed one at a time. **Partial success is possible**
  — each tag reports its own outcome and the call is retryable per tag.
- Temporary local tags created for the push are removed afterwards.

A published image is an ordinary registry image: use it as the `image` of a
service on any host, or as the `odoo_image` of an environment or production.

## Limits and retention

Every bound is per team, in `[team.X.image_registry]`:

| Bound | Default | What happens at the limit |
|---|---|---|
| `max_concurrent_builds` | `2` | A further `start_image_build` is refused as busy |
| `build_timeout_seconds` | `1800` | Hard wall-clock deadline; the worker is terminated and the job fails |
| `max_context_mb` | `512` | The sealed context is refused before the build starts (also a fixed 200 000-file ceiling) |
| `max_log_mb` | `16` | The persisted log is truncated with a marker; the build continues |
| `keep_images` | `10` | Older staging images are untagged after each successful build |

Pruning keeps the staging tags of the newest `keep_images` succeeded builds and
never removes a tag that a running **or stopped** container references, so a
service built on a staging image keeps working. Underlying image objects are
removed only once Docker reports no container uses them. Set `keep_images = 0`
to disable pruning entirely.

Jobs and logs live under `{data_dir}/team_{ID}/image_builds/<build-id>/`
(`job.json` plus `build.log`). The sealed build context is deleted when the job
reaches a terminal state.

## After an Oduflow restart

Builds are not resumed. A job found non-terminal on disk is marked
`interrupted` the first time it is read. Succeeded builds stay publishable for
as long as their staging image is still in the daemon. Retrying is simply a new
`start_image_build` — the Docker layer cache usually makes it cheap.

## Out of scope

Multi-platform builds, build secrets, image signing, SBOM/provenance
attestations, registry authentication for *pulling* private service images, and
a dashboard build history are all deliberately absent. See
[`specs/0054`](https://github.com/oduist/oduflow/blob/main/specs/0054-agent-container-image-build-and-publish.md)
for the reasoning and for the multi-tenant design that was shelved.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| *"Container image building is not configured for this team"* | No `[team.X.image_registry]` section. Add one (`repository_prefix` is enough) and restart Oduflow |
| *"already has N active image build(s)"* | `max_concurrent_builds` reached. Wait, cancel a build, or raise the bound |
| *"Build context exceeds the configured limit"* | The whole commit tree is bigger than `max_context_mb`. Narrow `context` to a subdirectory or raise the bound |
| *"dockerfile … not found under context … at commit"* | The path is resolved **in the committed tree**. Commit and push the Dockerfile, then `pull_and_apply` |
| The build used the wrong code | HEAD of the managed checkout was behind. `pull_and_apply`, then build again |
| Status is `interrupted` | Oduflow restarted mid-build. Start a new build |
| *"The staging image … is no longer in the local Docker daemon"* | Retention or a manual `docker rmi` removed it. Run `start_image_build` again |
| *"Staging builds can only be used by the team that built them"* | The staging tag belongs to another team. Use the tag `get_image_build` reports for your own build |
| A push fails with authentication or permission errors | Either the daemon's `docker login` lacks rights to the prefix, or the configured `username`/`token` does. The token must be able to write every repository below `repository_prefix` |
| Some tags published, others failed | Expected — tag writes are not a transaction. Re-call `publish_image_build` with the failed tags |

## Security notes

- The agent never receives Docker access or registry credentials; Oduflow
  builds and pushes on its behalf.
- `repository_prefix` is the authorization boundary, enforced by Oduflow. Still
  issue a **least-privilege registry token** restricted to that namespace —
  defence in depth if the config is ever misread.
- The token is stored in `oduflow.toml`. Protect the file (mode 0600) and
  prefer a token you can revoke over an account password.
- The four build tools are available on the scoped `/mcp/<env>` endpoint, so an
  environment-scoped credential can publish within the team's namespace. See
  [Authentication & Security](security.md#scoped-single-environment-access-mcpenv).
