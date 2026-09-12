# Declarative Stacks

An Oduflow Stack is a versioned YAML manifest describing the complete desired
state of one development environment and its supporting resources. It keeps the
host-level `oduflow.toml` separate from project configuration: teams, routing,
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
and last successful apply record.

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
keys, undeclared volume references, invalid names, and unsafe file paths are
rejected.

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
`plan`.

Docker can expose container environment values to host administrators through
`docker inspect`; Stack value sources do not change that existing Docker trust
boundary. Configure private Git credentials separately with `setup_repo_auth`.

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
4. the Odoo environment;
5. text files in volumes;
6. auxiliary services;
7. missing Odoo modules.

Module installation happens after services so an install hook can connect to a
declared dependency. Only missing modules are installed; Stack apply never
uninstalls a module.

## Safe and replacement changes

V1 can reconcile these changes in place:

- Odoo image and Odoo container environment variables;
- service image, environment, port/routes, hostname, volumes, host mode, and
  capabilities;
- new extra repositories, volumes, databases, files, services, and modules.

Changing an existing environment's `repoUrl`, `branch`, `template`, or
`extraRepositories` requires replacement and is reported as a conflict. Volume
descriptions are also immutable in V1. There is no automatic deletion or
`prune`: removing something from YAML does not destroy persisted data.

V1 supports one development environment per manifest. Production stacks,
portable database artifacts, binary volume files, lockfiles, lifecycle shell
hooks, dashboard controls, and OCI distribution are intentionally deferred.

Service definitions also accept the explicit [`runtime` lifecycle mapping](services.md#container-lifecycle-settings).
Stack planning detects changes to it and replacement preserves the declared settings.
