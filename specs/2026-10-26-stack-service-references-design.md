# Stack Service References — Technical Specification

**Status:** Proposed  
**Date:** 2026-10-26  
**Target:** Declarative Stack V1 (`oduflow.dev/v1alpha1`)  
**Related decisions:** [[0046-declarative-oduflow-stacks]],
[[0007-auxiliary-services-and-volumes]], [[0027-hard-tenant-isolation]]

> This is a forward-looking implementation specification. It does not change
> the current Stack schema until the implementation is merged.

## Problem

A service in a Stack can consume values from the host environment, the Stack's
Odoo target, or a managed PostgreSQL database. It cannot refer to another
service declared in the same manifest.

The missing reference forces a portable Stack to know Oduflow's physical
container naming convention. For example, an HTTP-to-Redis bridge currently
needs this deployment-specific value in `.env`:

```dotenv
REDIS_HOST=oduflow-2-svc-inbox-zero-redis
```

The value embeds the Oduflow prefix and team ID. Copying the Stack to another
team or changing the server prefix therefore requires a manual rewrite. A URL
containing the same hostname has the same problem:

```dotenv
UPSTASH_REDIS_URL=http://oduflow-2-svc-inbox-zero-redis-http:80
```

The manifest already owns both services and Oduflow already knows their
physical names. That information should be resolved at apply time, just like a
managed database host or production container name.

## Goals

1. Let an auxiliary service environment variable refer to a sibling service by
   its logical key under `spec.services`.
2. Resolve the sibling's deterministic internal Docker DNS name without a
   Docker lookup and without exposing the team ID or server prefix in YAML or
   `.env`.
3. Support both a raw hostname and a value composed around that hostname, such
   as an internal URL.
4. Validate references, network reachability, dependency order, and cycles
   before mutating Docker resources.
5. Preserve deterministic `plan`, `apply`, and `status` behaviour with no drift
   after a successful apply.
6. Remain backward compatible with every existing Stack V1 manifest.

## Non-goals

- General-purpose variable interpolation or expression evaluation.
- References to services owned by another Stack or created imperatively.
- Service discovery across teams or Docker networks.
- Reading arbitrary live container attributes with `docker inspect`.
- Injecting sibling service references into `spec.environment.env` or
  `spec.production.env`.
- Defining health checks, readiness probes, or runtime restart propagation.
- Replacing named secrets or managed database value sources.

## Manifest API

Extend `ValueFrom` with a sibling-service source:

```yaml
spec:
  services:
    redis:
      image: redis:7
      port: 8888

    redis-http:
      image: hiett/serverless-redis-http:0.0.10
      port: 8888
      env:
        REDIS_HOST:
          service: redis
          serviceField: containerName

    app:
      image: example/app:1
      port: 3000
      env:
        UPSTASH_REDIS_URL:
          service: redis-http
          serviceField: containerName
          format: "http://{value}:80"
```

The corresponding Python model additions are conceptually:

```python
service: ResourceName | None = None
service_field: Literal["containerName"] | None = None
format: str | None = None
```

`serviceField` deliberately contains only `containerName` in the first
version. More fields can be added later without changing the source shape.

### Source exclusivity

`service` is a fifth mutually exclusive `ValueFrom` source alongside
`fromEnv`, `environmentField`, `productionField`, and `database`.

- `service` and `serviceField` must be set together.
- `format` is allowed only with a `service` source.
- Unknown keys remain rejected by the existing strict Pydantic model.
- A literal string remains a literal string. Oduflow must not interpret
  `${...}`, shell syntax, or braces in ordinary string values.

### Formatting

When `format` is omitted, the resolved environment value is the raw container
name. When present:

- it must contain exactly one literal `{value}` placeholder;
- no other placeholder or expression syntax is accepted;
- Oduflow replaces `{value}` once with the resolved container name;
- the replacement is plain string substitution, not Python `str.format`, shell
  expansion, or a template language;
- an empty result is invalid.

This narrowly supports values such as `redis://{value}:6379` and
`http://{value}:80` without introducing general interpolation or accidentally
interpreting existing shell-oriented values.

## Validation rules

Validation happens during manifest loading and must fail before Stack planning
or Docker mutation when any rule is violated.

1. `service` must match a key in the same `spec.services` mapping.
2. A service cannot reference itself.
3. The consuming service and referenced service must both use the team bridge
   network (`hostMode: false`). A host-mode container cannot resolve the
   sibling's team-network DNS name, and a host-mode target is not reachable by
   that name.
4. References are accepted only in `spec.services.<consumer>.env`.
5. The complete service-reference graph must be acyclic. The validation error
   must show the cycle, for example:
   `services.a -> services.b -> services.a`.
6. `serviceField` values outside the schema enum are rejected.
7. `format` must satisfy the formatting rules above and the final resolved
   value must be a valid Docker environment string.

References are by logical manifest key, not public Traefik hostname and not
Docker label. Renaming the key is therefore an explicit Stack configuration
change.

## Resolution semantics

For `serviceField: containerName`, resolve the value with the same pure naming
function used by auxiliary-service lifecycle operations:

```python
get_service_container_name(service, settings.prefix, team.team_id)
```

Resolution must not require the referenced container to exist. This keeps
`stack plan` deterministic, lets the whole graph be planned before creation,
and avoids live Docker state becoming part of manifest interpretation.

The resolved value is used only in the effective environment passed to service
creation or replacement. The declarative source object remains the desired
configuration used for hashing and reconciliation.

## Dependency ordering

The current manifest order is not a sufficient dependency contract. Stack
planning and apply must derive a stable topological order from service
references:

- a referenced service is created or updated before each consumer;
- unrelated services retain their manifest order as the stable tie-breaker;
- deletion remains out of scope for additive Stack V1;
- module installation remains after all auxiliary services;
- a cycle is a validation error and never reaches apply.

Ordering guarantees configuration availability, not application readiness. A
consumer must still retry its connection until the referenced process is
listening. Readiness probes are a separate capability.

## Reconciliation behaviour

### Validate

`stack validate` checks the source shape, target existence, network rules,
format, and graph cycles without requiring server settings or Docker.

### Plan

`stack plan` resolves the effective value using server prefix and team ID when
comparing desired and live service configuration. Plan output identifies the
environment key that changed but does not need to print the resolved value.

A missing referenced service still produces its normal `create` action before
the consumer's action. A reference to an undeclared service is a validation
error, not a plan conflict.

### Apply

`stack apply` uses the same resolution path as plan and applies services in the
stable topological order. It must not persist the resolved physical name back
into the manifest, `.env`, or Stack ownership hash in place of the declarative
reference. The saved imperative service preset may retain the effective
resolved environment, as it does for existing Stack value sources; the
manifest remains the source of truth for later Stack reconciliation.

The live container necessarily receives the resolved value in its environment,
under the same host-administrator trust boundary as every existing service
environment value.

### Status and drift

`stack status` compares the live effective environment with a freshly resolved
desired environment. Re-running plan/apply/status with the same manifest,
prefix, and team must produce no update drift.

Changing `settings.prefix` or moving the Stack to another team changes the
effective container name and must plan a consumer service replacement. An
ordinary restart or replacement of the referenced service does not change the
name and must not replace consumers.

## Error messages

Errors should name the consumer, environment key, and referenced logical
service. Examples:

```text
service 'app' env 'REDIS_HOST' references undeclared service 'redis'
service 'redis' env 'REDIS_HOST' cannot reference itself
service 'app' env 'REDIS_HOST' cannot reference host-mode service 'redis'
service reference cycle: services.a -> services.b -> services.a
service 'app' env 'REDIS_URL' format must contain exactly one '{value}'
```

No error or plan line may reveal a named-secret value from another environment
entry.

## Implementation areas

The expected implementation touches:

- `src/oduflow/stack_models.py` — source fields and cross-service validation;
- `src/oduflow/stack_loader.py` — deterministic resolution and narrow format
  substitution;
- `src/oduflow/stack_ops.py` — dependency graph, stable topological order, and
  effective-environment reconciliation;
- `src/oduflow/schemas/oduflow-stack-v1alpha1.json` — regenerated schema;
- `docs/stacks.md` — syntax, semantics, limits, and examples;
- unit and integration tests covering the acceptance matrix below.

No dashboard work, data migration, startup migration, or new MCP tool is
required.

## Test matrix

### Model and schema tests

- accept raw `containerName` and formatted references;
- reject missing `serviceField`, missing `service`, unknown field, bad format,
  undeclared target, self-reference, host-mode endpoints, and cycles;
- continue accepting all existing literal and `ValueFrom` forms;
- generated JSON Schema matches the checked-in schema.

### Resolution tests

- resolve default and custom Oduflow prefixes;
- resolve at least two team IDs;
- preserve valid service-name normalization;
- format a Redis URI and HTTP URL exactly once;
- resolve without a Docker client or live referenced container.

### Plan/apply/status tests

- create a target before its consumer even when YAML order is reversed;
- keep manifest order between unrelated services;
- create a multi-hop dependency chain in topological order;
- report no drift after apply;
- update only the consumer whose effective environment changed;
- compute Stack ownership hashes from the declarative source rather than a
  team-specific resolved value;
- fail before mutation for a cycle or invalid reference;
- exercise both development-target and production-target Stacks.

## Acceptance criteria

The capability is complete when all of the following are true:

1. An Inbox Zero Stack can remove `REDIS_HOST` and `UPSTASH_REDIS_URL` from
   `.env` and express both using sibling-service references.
2. The same unchanged manifest applies successfully to team 1, team 2, and a
   server with a non-default Oduflow prefix.
3. Repeated `stack plan`, `stack apply`, and `stack status` converge without
   false environment drift.
4. Reversed YAML service order still creates Redis, then its HTTP bridge, then
   Inbox Zero.
5. Invalid references and cycles fail validation before any resource is
   created or updated.
6. Existing Stack V1 manifests and their hashes remain valid without edits.
7. Unit tests, Stack integration tests, `ruff`, and `mypy` pass.

## Rollout and compatibility

This is an additive schema change within `oduflow.dev/v1alpha1`. Existing
manifests require no migration. Older Oduflow versions correctly reject the new
fields as unknown rather than silently applying an incomplete configuration.

After the feature ships, update the Inbox Zero example in
`oduflow/oduflow-stacks` to use service references and delete its documented
team-ID substitution step. That example is the end-to-end portability test for
the capability.
