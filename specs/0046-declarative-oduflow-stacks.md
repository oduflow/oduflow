# 0046 — Declarative Oduflow Stacks

**Status:** Adopted (V1)
**Type:** Architecture — significant capability
**First introduced:** this change (2026-08-11), branch `litnimax/declarative-oduflow-distro`
**Key code today:** `stack_models.py` (versioned typed manifest), `stack_loader.py` (safe YAML and value sources), `stack_ops.py` (plan/apply/status reconciler), `stack_state.py` (per-team apply record), Stack CLI/startup integration in `server.py`

## Context

Oduflow already exposed all the individual operations needed to construct an
Odoo solution: create an environment from a Git branch and database template,
attach immutable extra-addons revisions, create auxiliary services and volumes,
write configuration files, and install modules. Reproducing a complete solution
still required an operator or agent to remember and replay a sequence of tool
calls. Service presets covered one container but did not express dependencies
or the Odoo environment itself.

The desired unit is a portable, reviewable product definition that lives in Git
and can bring an Oduflow installation to a known end state. It must reuse the
existing Docker lifecycle rather than introduce a second runtime, must preserve
team isolation and locking, and must not turn ordinary reconciliation into an
implicit database or volume deletion mechanism.

## Decision

Introduce a versioned **Oduflow Stack** manifest (`oduflow.dev/v1alpha1`, kind
`Stack`) and a built-in reconciler. A Stack V1 owns one development environment
plus its extra repositories, named volumes, text volume files, auxiliary
services, and required Odoo modules. `oduflow stack validate/plan/apply/status`
are the explicit control surface; `oduflow --stack` runs the same apply before
the server transport starts.

YAML is only the authoring syntax. Pydantic models are the canonical contract
and generate the shipped JSON Schema. The host's `oduflow.toml` remains the
operator configuration for teams, auth, routing, quotas, and backup; a project
manifest does not override it.

V1 is intentionally additive. It creates missing resources and updates only
configurations for which Oduflow already has a state-preserving update path.
Environment source/template/extra-addon drift, another owner's resource, or an
immutable volume-description change is a preflight conflict. Removal from YAML
never deletes a live resource.

## How it works

The loader rejects duplicate YAML keys, unknown fields, invalid names,
undeclared volume references, unsafe relative file paths, and non-text/oversize
files. Values may be literals, host environment references, or a service-side
reference to the created environment's URL/scoped MCP token. Resolved values
are used only at apply time and never persisted in the Stack state record.

The planner inspects existing label-tracked resources and classifies operations
before mutation. Apply repeats that plan under the team's lock, refuses the
entire run if any conflict exists, and executes repositories → volumes → Odoo →
volume files → services → missing modules. This ordering gives service config
access to generated Odoo outputs while ensuring module install hooks see their
declared services. Failed external operations leave already-created resources
in place; the next idempotent apply continues from live state.

Environments, services, and volumes carry `oduflow.stack`,
`oduflow.stack-resource`, and a per-resource spec hash. A small atomic
`stacks/<name>.json` record stores the last manifest hash and non-secret resource
inventory. Extra repositories remain team-shared and are reused only when name
and sanitized remote URL agree.

## Consequences

- Complete development solutions can be reviewed in Git and reproduced with
  one command or automatically at server startup.
- The reconciler composes the existing Docker/Odoo operations, preserving their
  tenancy, template, networking, and update semantics instead of delegating to
  Docker Compose or another control plane.
- Refusing adoption, replacement, deletion, and prune makes V1 conservative
  around databases and persistent volumes, at the cost of requiring manual
  lifecycle work for renamed or removed resources.
- One manifest owns one development environment. Production, portable database
  artifacts, binary files, immutable Git/image lockfiles, lifecycle hooks,
  dashboard/MCP surfaces, and OCI packaging remain independent future steps.

## Evolution

A later version can add lockfiles and signed distribution artifacts without
changing the V1 ownership or reconcile model. Production support must preserve
the separate production database, backup, health, and rollback semantics in
[[0035-production-hosting]] rather than treating production as a flag on the
development resource.

Services can now reference a sibling service by manifest key
(`service`/`serviceField: containerName`, optional one-placeholder `format`).
The value is the deterministic team-network container name, derived from the
server prefix and team without a Docker lookup, so a manifest no longer embeds
deployment-specific names and `plan` stays stable before the sibling exists.
The fixed V1 apply order gains one refinement: services are applied in a
stable topological order of these references (manifest order otherwise), and
reference cycles are validation errors. Ordering covers configuration only, not
readiness.

## History

- 2026-08-11 — V1 Stack manifest, safe value sources, plan/apply/status,
  ownership labels, and startup reconciliation introduced on
  `litnimax/declarative-oduflow-distro`.
