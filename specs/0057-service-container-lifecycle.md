# Explicit service container lifecycle settings

Status: Implemented · Type: Runtime capability · First introduced: 2026-09-12

Key code: `service_runtime.py`, `docker_ops/service_ops.py`, `stack_ops.py`.

## Context

The Oduflow platform bundles Salt Master/API and LiteLLM/exporter as application
releases using systemd. These containers need temporary runtime mounts, a private
cgroup namespace and enough time for an orderly shutdown. Recreating a service
must retain these settings just as it retains its volumes and command.

## Decision

Expose a small validated `runtime` mapping through service creation/update and
Stack manifests. Supported fields are `tmpfs` (only `/run`, `/run/lock`, `/tmp`),
`cgroupns: private`, `stop_signal` and `stop_timeout`. No arbitrary Docker SDK
arguments or host bind mounts are accepted. Existing privilege settings remain
explicit; this mapping never grants capabilities or privileged mode implicitly.

## How it works

The mapping is recorded with the container and preset, compared by stack planning,
and reused during replacement. Updates omit the mapping to preserve it or pass
an empty mapping to clear it. Stop/restart operations respect the old container's
configured timeout before replacing it with the new configuration.

## Consequences

Oduflow can describe the lifecycle requirements of systemd applications without
managing their internal units. Whether a particular image can boot under a given
Docker/cgroup configuration still requires runtime verification. A healthy PID 1
does not establish application health; images should supply their own healthcheck.

Related: [[0053-explicit-start-commands-for-auxiliary-services]],
[[0052-managed-postgresql-databases-for-auxiliary-services]].

## History

Initial implementation accompanies the platform native-services change.
