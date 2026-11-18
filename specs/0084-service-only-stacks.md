# 0084 — Service-only Stacks

**Status:** Adopted
**Type:** Architecture / Deployment
**First introduced:** Service Stacks (2026-11-18)
**Key code:** `stack_models.py`, `stack_ops.py`, `service_database_ops.py`

## Context

Multica needs a Web service, an API, a PostgreSQL database and a persistent
volume for attachments. The Stack introduced in [[0046-declarative-oduflow-stacks]]
could describe each resource. Applying it also required provisioning Odoo.
[[0063-production-stack-targets]] added production as an alternative to a
development environment and retained the single-target contract.

## Decision

A Stack may own zero or one Odoo target. Omitting both targets describes a
service-only deployment. Each service database selects its PostgreSQL cluster at
creation, independently of whether the Stack also hosts Odoo. Existing database
declarations default to the development cluster.

## How it works

The planner and executor run the Odoo lifecycle only when a target is declared.
Services can consume database credentials directly. Odoo value references and
add-on repositories require their corresponding target. Existing ownership
checks, dependency order and team locking also apply to service-only Stacks.

## Consequences

An operator can add Odoo to the same Stack later while preserving existing
service containers and data that match the manifest. The choice of database
cluster determines eligibility for cluster backups from the first deployment.
Changing that placement requires a separate data migration.

## History

- 2026-11-18 — Extended the single-target contract from commit `6a26ac88`
  to support independent service deployments.
