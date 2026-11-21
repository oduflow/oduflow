# Declarative Stacks

An Oduflow Stack is a versioned YAML manifest describing the complete desired
state of services, PostgreSQL databases, named volumes and configuration files.
A Stack can also manage one Odoo development environment or one production.
It keeps the host-level `oduflow.toml` separate from project configuration: teams, routing,
authentication, quotas, and backups remain operator settings, while the Stack
file can live beside the project's code and move between Oduflow installations.

## Commands

```bash
oduflow stack validate oduflow.yaml
oduflow stack plan oduflow.yaml --team 1
oduflow stack apply oduflow.yaml --team 1
oduflow stack status oduflow.yaml --team 1
```

`validate` is local and does not require Docker or `oduflow.toml`. `plan` reads
live state without changing it. `apply` validates and plans again under the
team lock, refuses all conflicts before creating anything, and then converges
resources in dependency order. `status` emits JSON containing the current plan
and last successful apply record; `inSync` is true exactly when that plan is
empty. A declared service whose container is not running is drift: the plan
shows `start services.<name>` and `apply` starts it again, as it does for a
stopped production container. `plan`, `apply`, and `status` also accept
`--env-file` to supply `fromEnv` values from a dotenv file; see
[Secrets from a `.env` file](#secrets-from-a-env-file).

`apply` logs its progress at the `info` level. For output that shows only the
resulting plan plus any warnings, pass the global
`oduflow --log-level warning stack apply oduflow.yaml`
(see [CLI Reference](cli.md#global-options)).

To reconcile before the MCP server accepts clients:

```bash
oduflow --stack /etc/oduflow/acme/oduflow.yaml \
  --stack-team 1 \
  --transport http
```

A failed startup reconciliation exits without starting the MCP transport. Any
resources already created before an external failure remain owned by the Stack;
rerunning the same command safely continues from live state.

## Example

```yaml
apiVersion: oduflow.dev/v1alpha1
kind: Stack

metadata:
  name: acme-erp

spec:
  environment:
    name: acme-dev
    hostname: qa                # optional; dev.example.com -> qa.example.com
    branch: "18.0"
    repoUrl: https://github.com/acme/odoo-addons.git
    odooImage: odoo:18.0
    template: acme-18
    sanitize: true

    env:
      LOG_LEVEL: info
      PRIVATE_API_KEY:
        fromEnv: ACME_PRIVATE_API_KEY

    modules:
      install:
        - acme_base
        - acme_sale

  extraRepositories:
    enterprise:
      repoUrl: https://github.com/odoo/enterprise.git
      branch: "18.0"

    oca-web:
      repoUrl: https://github.com/OCA/web.git
      branch: "18.0"

  volumes:
    fs-sounds:
      description: FreeSWITCH sounds and configuration

  files:
    - source: files/freeswitch.xml
      volume: fs-sounds
      path: config/freeswitch.xml

  services:
    fs:
      image: oduist/freeswitch:1.4.0
      port: 8080
      hostMode: true

      # Optional: replaces the image CMD. A shell-quoted string
      # ("server /data") is accepted and split into the same argv.
      command: ["freeswitch", "-nonat"]

      volumes:
        - source: fs-sounds
          target: /usr/share/freeswitch/sounds
          mode: rw

      env:
        ODOO_URL:
          environmentField: url
        FS_WEBHOOK_TOKEN:
          environmentField: token
        FS_ESL_PASSWORD:
          fromEnv: FS_ESL_PASSWORD
```

The generated JSON Schema is shipped at
`oduflow/schemas/oduflow-stack-v1alpha1.json`. Unknown fields, duplicate YAML
keys, undeclared volume references, invalid names, unsafe file paths, and a
`spec` that declares no resource at all are rejected.

## Service-only Stacks

Omit both Odoo targets to deploy services on their own. This example uses
placeholder Web and API images. Replace them with the images for your application.

```yaml
apiVersion: oduflow.dev/v1alpha1
kind: Stack
metadata:
  name: multica
spec:
  volumes:
    attachments: {}
  databases:
    multica:
      cluster: prod
  services:
    web:
      image: example/multica-web:1
      port: 3000
    api:
      image: example/multica-api:1
      port: 8080
      env:
        DATABASE_URL:
          database: multica
          databaseField: url
      volumes:
        - source: attachments
          target: /data/attachments
```

Choose the database cluster at creation. The default is `dev`, including for
existing manifests that specify `{}`. The `prod` cluster requires production
hosting to be enabled and participates in cluster-wide WAL-G backups when
configured. Those backups cover PostgreSQL data. Attachment volumes need their
own backup. Changing the cluster of an existing database requires a migration.

To add Odoo later, keep the Stack name and resource names and add either
`spec.environment` or `spec.production`. If the existing resources match their
declarations, the next apply adds Odoo and leaves those resources in place.
`extraRepositories` requires an Odoo target. Service variables may use
`environmentField` or `productionField` only with the corresponding target.

## Value sources

An environment variable can be a literal string:

```yaml
LOG_LEVEL: info
```

It can be read from the process starting Oduflow:

```yaml
ESL_PASSWORD:
  fromEnv: FS_ESL_PASSWORD
```

### Secrets from a `.env` file

`fromEnv` values do not have to come from exported shell variables. If a
`.env` file sits next to the manifest, `stack plan`, `stack apply`,
`stack status`, and the `--stack` startup reconciliation parse it and use it
for `fromEnv` lookups. Pass `--env-file path/to/file` to read a different
file instead; an explicitly named file must exist.

```dotenv
# .env — keep this file out of version control
FS_ESL_PASSWORD=s3cret
export ACME_PRIVATE_API_KEY="matching surrounding quotes are stripped"
```

The format is deliberately dumb: `KEY=VALUE` lines, blank lines and `#`
comments, an optional `export ` prefix. There is no `${VAR}` interpolation,
no escape processing, and no multi-line values; malformed lines and duplicate
keys are rejected. Real process environment variables override file values,
so CI can override a checked-in default without editing the file.

The file only feeds `fromEnv:` references. It never defines container
variables by itself, is never persisted anywhere, and never enters the
manifest hash. Add `.env` to `.gitignore`: it is the one file in a stack
directory meant to hold secrets.

Or a service can consume a value generated for the Stack's Odoo environment:

```yaml
ODOO_URL:
  environmentField: url
MCP_TOKEN:
  environmentField: token
```

Managed PostgreSQL credentials can be wired into an auxiliary service
without putting the generated secret in YAML:

```yaml
databases:
  events: {}

services:
  worker:
    image: example/worker:1
    port: 8080
    env:
      DATABASE_URL:
        database: events
        databaseField: url
      PGPASSWORD:
        database: events
        databaseField: password
```

Supported database fields are `url`, `host`, `port`, `database`, `username`,
and `password`. The database must be declared under `spec.databases`, and these
references are accepted only in auxiliary service environments. They cannot be
injected into the Odoo environment.

`environmentField` is deliberately unavailable under `spec.environment.env`,
because an environment cannot depend on an output that exists only after that
same environment has been created. Resolved values are passed directly to the
container. They are never written to the Stack state file or printed by
`plan`; a service update names the variables that changed, not their values.

Docker can expose container environment values to host administrators through
`docker inspect`; Stack value sources do not change that existing Docker trust
boundary. Configure private Git credentials separately with `setup_repo_auth`.

### Sibling service references

A service can refer to another service declared in the same manifest by its
key under `spec.services`. Oduflow resolves that sibling's internal Docker DNS
name on the team network at apply time, so the manifest and `.env` never spell
out the server prefix or team ID:

```yaml
services:
  redis:
    image: redis:7
    port: 6379

  redis-http:
    image: hiett/serverless-redis-http:0.0.10
    port: 8888
    env:
      REDIS_HOST:
        service: redis
        serviceField: containerName   # oduflow-2-svc-redis on team 2

  app:
    image: example/app:1
    port: 3000
    env:
      UPSTASH_REDIS_URL:
        service: redis-http
        serviceField: containerName
        format: "http://{value}:80"   # http://oduflow-2-svc-redis-http:80
```

`containerName` is the only `serviceField` today. It is computed from the server
prefix, the team ID, and the service key — the name Oduflow gives the
container — without asking Docker, so `plan` can resolve it before the sibling
exists and the same unchanged manifest applies to any team on any server.

`format` wraps the name in a fixed string. It must contain exactly one
`{value}` and no other braces; Oduflow replaces that single placeholder and
nothing else. It is not a template language: there is no `${...}`, shell, or
`str.format` expansion, and literal values that happen to contain braces are
never interpreted.

`stack validate` checks the references before anything is planned:

- the target must be declared under `spec.services`, and a service cannot
  reference itself;
- both services must be on the team network: a `hostMode: true` service can
  neither reference a sibling nor be referenced, because a host-network
  container does not take part in the team network's DNS;
- references are accepted only in `spec.services.<name>.env`, not in
  `spec.environment.env` or `spec.production.env`;
- references must not form a cycle; the error shows the loop, for example
  `service reference cycle: services.a -> services.b -> services.a`.

A referenced service is created or updated before the services that consume
it; services without references between them keep their manifest order. This
orders configuration, not readiness: a consumer can start before the process
it points at is listening, so it must retry its connection.

References are by manifest key, so renaming a key is a configuration change for
its consumers. Changing the server prefix or moving the Stack to another team
changes the resolved name, and the next `plan` updates only the consuming
services. Restarting or replacing a referenced service keeps its name and does
not touch them.
## Building a service image

A service can build its image from a git repository instead of naming one.
Declare `build` in place of `image`:

```yaml
apiVersion: oduflow.dev/v1alpha1
kind: Stack
metadata:
  name: gateway
spec:
  volumes:
    gateway-home: {}
  services:
    gateway:
      build:
        repoUrl: https://github.com/acme/gateway.git
        ref: main                    # branch, tag or full commit SHA
        dockerfile: docker/Dockerfile
        # context: .                 # directory inside the repository
        # target: runtime            # multi-stage target
        # args: { APP_VERSION: "1.4.0" }
        # gitUser: acme-ci           # account whose stored token to use
      port: 8080
      volumes:
        - source: gateway-home
          target: /home/app
```

The repository needs no Odoo code and the Stack needs no Odoo target. Every
plan resolves `ref` to a commit with one `git ls-remote`:

- no finished build of that commit and those build inputs exists yet — apply
  builds it first, then creates or updates the service with the result;
- the service already runs that build — nothing to do;
- the branch moved — the next apply builds the new commit and recreates the
  service on it. Pin a tag or a commit SHA to keep the image fixed.

Builds run before any other change, so a failing Dockerfile leaves the Stack
untouched; the error carries the build ID and the tail of the build log. A
build can take as long as the team's `build_timeout_seconds`, and apply waits
for it. That wait happens before apply takes the team lock, so the team's
environment tools keep working while an image builds; if the lock is busy once
the builds are done, run apply again — it reuses them. An operator can refuse
builds for a team with `builds_enabled = false` in `[team.X.image_registry]`;
the plan then reports the service as a conflict.

A build started by `oduflow stack apply` belongs to that command:
[`get_image_build`](mcp-tools.md#get_image_build) shows its progress, and
interrupting the command is how to cancel it.

The image stays in the host's Docker daemon as a staging tag
(`oduflow-build/team-<id>:<build-id>`), which the running service keeps alive.
No registry is involved; publish a build with
[`publish_image_build`](mcp-tools.md#publish_image_build) when it has to reach
another host. Private repositories use the team's stored git credentials or SSH
deploy key — `repoUrl` itself must not contain a token. Build `args` are not
secret: they end up in the image history.

A Stack's builds run one after another. Each build leaves a record (status and
log) in the team's data directory. `oduflow cleanup` and the dashboard's
**Cleanup** remove the records of failed, cancelled and interrupted builds
(including one whose `stack apply` process died), and of succeeded builds whose
staging image is gone; a running build and a build whose image still exists
are kept, so a re-apply keeps reusing it. See
[Maintenance Commands](cli.md#maintenance-commands).

## Reconciliation and ownership

Resources created by a Stack carry these Docker labels:

```text
oduflow.stack=acme-erp
oduflow.stack-resource=services.fs
oduflow.stack-spec-hash=<sha256>
```

Oduflow will not silently adopt an existing environment, service, volume, or database
with the same name. It reports an ownership conflict instead. Extra-addon bare
repositories remain team-shared by design: an existing repository with the same
name and URL is reused, while a different URL is a conflict.

The V1 apply order is:

1. extra-addon repositories;
2. named volumes;
3. managed PostgreSQL databases;
4. the Odoo target, when declared;
5. text files in volumes;
6. auxiliary services, each after the [services it
   references](#sibling-service-references), otherwise in manifest order;
7. missing Odoo modules.

Module installation happens after services so an install hook can connect to a
declared dependency. Only missing modules are installed; Stack apply never
uninstalls a module.

## Safe and replacement changes

V1 can reconcile these changes in place:

- Odoo image and Odoo container environment variables;
- service image (including a rebuild of a `build` source whose ref moved),
  environment, port/routes, hostname, volumes, host mode, and capabilities;
- new extra repositories, volumes, databases, files, services, and modules.

Changing an existing environment's `repoUrl`, `branch`, `template`, or
`extraRepositories` requires replacement and is reported as a conflict. Volume
descriptions are also immutable in V1. There is no automatic deletion or
`prune`: removing something from YAML does not destroy persisted data.

Portable database artifacts, binary volume files, lockfiles, lifecycle shell
hooks, dashboard controls, and OCI distribution are intentionally deferred.

Service definitions also accept the explicit [`runtime` lifecycle mapping](services.md#container-lifecycle-settings).
Stack planning detects changes to it and replacement preserves the declared settings.

## Production targets

Use `spec.production` instead of `spec.environment` for a long-lived production.
An Odoo production target requires `[production] enabled = true` and Traefik
routing. Its Odoo database uses the dedicated production cluster. Each service
database uses its own declared cluster.

```yaml
apiVersion: oduflow.dev/v1alpha1
kind: Stack
metadata:
  name: control
spec:
  production:
    name: control
    domain: demo.example.org
    repoUrl: https://github.com/acme/control.git
    branch: production
    odooImage: odoo:19.0
    autoUpdate: false
    allowCopyToDevMcp: false
    serverMode: workers   # or gevent; see Production Hosting → Server mode
    env:
      APP_KEY: secret:control-key
    odooConf:
      workers: "2"
  services:
    gateway:
      image: acme/gateway:1
      port: 8080
      env:
        ODOO_URL:
          productionField: url
        ODOO_HOST:
          productionField: containerName
```

`productionField` supports `url`, `containerName` and `database`. Productions do
not have a development scoped MCP token; `environmentField` is rejected with a
production target. Production variables accept literals, `fromEnv` and `secret:`
references. Target variables cannot reference their own target or a service DB.
Named secret references remain references in the production registry.

A fresh production is created through the normal production lifecycle. Optional
`template` seeds it once. Stack does not promote or stop an existing development
environment. Use the production promotion API first if existing data must move.
Modules and application revisions are delivered through `update_production`;
Stack reconciliation does not pull branch commits or run schema migrations.

### Bringing an existing production under Stack management

First describe the existing production exactly, including its domain, source,
image, environment variables, extra repositories, update policy, copy policy and
configuration overrides. Add `adoptExisting: true`, then review `stack plan`.
`adopt production` records ownership in `productions.json` without restarting
Odoo or copying its database/filestore. An absent production, configuration drift,
a stopped/missing/foreign container, another Stack owner or an active deploy
blocks adoption. The one exception is a container whose Traefik routing
predates the current [server mode](production.md#server-mode) routes: the plan
shows the adoption as recreating the container, and apply does so from the
production's (matching) record. Remove `adoptExisting` after adoption if desired; doing so does
not trigger an update. The flag never creates a missing production.

Owned productions reconcile domain, image, variables, server mode, update/copy
policies and `odooConf` overrides through production operations.
Image/domain/environment/server-mode/conf changes can restart Odoo; database and filestore persist. Major Odoo version
changes still require a separate migration. Source repository, branch, git user,
extra repositories and seed-template changes are conflicts: use an explicit
production workflow for these changes instead of silently running different code.

Ownership lives in registry metadata, so container replacement retains it.
Stack holds the production lock as well as the team lock. It records an incomplete
apply before mutation and a completed fingerprint only after success; a retry
cannot mistake updated registry intent for a successfully replaced container.
`plan` and `status` remain read-only. A repeated successful apply is a no-op.

Service and volume ownership rules remain unchanged. Auxiliary resources managed
outside a Stack must remain outside its manifest; production adoption does not
implicitly adopt those resources. Removing declarations never deletes resources,
including a retained dev environment from a previous deployment layout.
