# Running Oduflow in Docker

Oduflow can run as a Docker container. Since it manages other Docker containers (Odoo environments, PostgreSQL, etc.), it uses the **Docker-out-of-Docker** pattern — the host's Docker socket is mounted into the container.

## The one rule: the data directory must be the same path on the host

Oduflow creates the containers it manages through the host daemon, and every
bind mount it asks for (PostgreSQL tablespaces, filestores, the mirrored
`postgresql.conf`, Traefik's dynamic config, …) is a path **under its data
directory** (`/srv/oduflow` by default, `[storage].data_dir`). The **host
daemon** resolves those mount sources, not the Oduflow container. If
`/srv/oduflow` inside the container is a named volume, or is bound from a
different host path, the daemon looks for `/srv/oduflow/...` on the host, finds
nothing, and silently auto-creates **empty directories** in its place —
`oduflow-db` then crash-loops on an empty config, Traefik routes nothing, and
filestores land in the void.

So mount a **host directory at the same path on both sides**:

```
-v /srv/oduflow:/srv/oduflow
```

On Docker Desktop (macOS/Windows) that path must also be under a shared
location — for example `-v $HOME/oduflow:$HOME/oduflow` together with
`[storage] data_dir = "/Users/<you>/oduflow"` in `oduflow.toml`. The config
directory (`/etc/oduflow`) has **no** such requirement: the files that get
bind-mounted from it are mirrored into `<data_dir>/conf-mirror/` first, so it
can be a named volume or any host path.

When Oduflow runs in a container it checks this at startup and on
`GET /healthz` (`self_mounts`, also shown as the dashboard's `MOUNTS` chip): a
data directory the daemon cannot see is logged as a warning that names the
offending mount and the fix.

## Build

```bash
docker build -t oduflow .
```

## Run

### Minimal example

```bash
docker run -d \
  --name oduflow \
  -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /srv/oduflow:/srv/oduflow \
  oduflow
```

### Full example with all typical options

```bash
docker run -d \
  --name oduflow \
  --restart unless-stopped \
  -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /srv/oduflow:/srv/oduflow \
  -v /etc/oduflow:/etc/oduflow \
  oduflow
```

## Volume Mounts

| Mount | Purpose |
|---|---|
| `/var/run/docker.sock` | **Required.** Gives Oduflow access to the host Docker daemon to manage Odoo containers, PostgreSQL, Traefik, etc. |
| `/srv/oduflow` | Oduflow data directory. Contains team directories with workspaces, templates, port registry, and the `conf-mirror/` copies of the mounted configs. **Must be a host directory bound at the same path** (see the rule above) — not a named volume. |
| `/etc/oduflow` | System configuration directory. Contains `oduflow.toml`, license key, `postgresql.conf`, `postgresql-prod.conf`, `traefik-dynamic/`, default `odoo.conf`, and other configuration files. Mount to persist configuration across container restarts; a named volume is fine here. |

## Networking

The Oduflow container must be on the same Docker network as the containers it creates. The simplest approach is to connect it to `oduflow-net` after initialization:

```bash
# 1. Start Oduflow (Docker image defaults to HTTP mode)
docker run -d --name oduflow -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /srv/oduflow:/srv/oduflow \
  -v /etc/oduflow:/etc/oduflow \
  oduist/oduflow

# 2. Connect Oduflow to the shared network (created automatically on startup)
docker network connect oduflow-net oduflow
```

Alternatively, start with `--network oduflow-net` if the network already exists.

Shared infrastructure (Docker network, PostgreSQL, team directories) is initialized automatically on startup — no separate init step needed.

Oduflow also reconciles the active PostgreSQL `pg_hba.conf` with the actual
subnets reported by Docker IPAM for `oduflow-net` and every per-team network.
Only a marked `ODUFLOW MANAGED NETWORKS` block is changed; the standard local,
replication, and operator-managed rules remain intact. This works the same way
when Oduflow runs directly on the host or through Docker-out-of-Docker because
the file is updated through the Docker API rather than another host bind mount.

To set up a template database:

```bash
# From scratch (clean Odoo with specified modules)
docker exec oduflow oduflow init-template --odoo-image odoo:19.0 --template-name default --modules base,web,contacts

# Or import from a running Odoo instance
docker exec oduflow oduflow import-template https://my-odoo.example.com master_password --template-name default
```

## Configuration

Oduflow reads its configuration from `oduflow.toml`. When running in Docker, mount the config directory:

```bash
-v /etc/oduflow:/etc/oduflow
```

Key configuration settings in `oduflow.toml`:

```toml
[server]
bind = "0.0.0.0"
port = 8000

[team.1]
hostname = "localhost"
auth_token = "your-secret-token"   # MCP Bearer token
ui_password = "your-ui-password"   # Web UI login password
```

HTTP mode refuses empty MCP or dashboard credentials unless
`[server].allow_insecure_http = true` is set explicitly. Use that escape hatch
only behind another authenticating proxy.

See [Installation — Configuration Reference](installation.md#configuration-reference) for all options.

## Docker Compose

```yaml
services:
  oduflow:
    image: oduist/oduflow
    ports:
      - "8000:8000"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      # Host directory, same path on both sides: the host daemon resolves
      # every mount Oduflow creates under it. A named volume here breaks
      # oduflow-db, Traefik and every environment (see the rule above).
      - /srv/oduflow:/srv/oduflow
      # The config dir may be a named volume or a host path.
      - oduflow_etc:/etc/oduflow
    restart: unless-stopped
    networks:
      - oduflow-net

volumes:
  oduflow_etc:

networks:
  oduflow-net:
    name: oduflow-net
```

After `docker compose up -d`, Oduflow initializes shared infrastructure automatically.

## Security Notes

- Mounting the Docker socket gives the container **full control** over the host Docker daemon. This is equivalent to root access on the host. Only run Oduflow in trusted environments.
- Set `auth_token` in `[team.*]` to protect the MCP endpoint.
- Set `ui_password` in `[team.*]` to protect the Web UI.

## Privileged Mode and fuse-overlayfs

Oduflow uses `fuse-overlayfs` for efficient filestore sharing when templates exceed `overlay_threshold_mb` (default: 50 MB). This requires the `/dev/fuse` device inside the container.

If your templates are small (under the threshold), Oduflow falls back to simple file copy and no special privileges are needed.

For large templates, run with the fuse device:

```bash
docker run -d \
  --name oduflow \
  --device /dev/fuse \
  --cap-add SYS_ADMIN \
  -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /srv/oduflow:/srv/oduflow \
  oduflow
```

Alternatively, set a high threshold in `oduflow.toml` to avoid overlayfs entirely:

```toml
[storage]
overlay_threshold_mb = 999999
```

## Scheduled template sync

System cron runs on the Docker host and invokes the worker via `docker exec`
in this existing container. Add a writable `/etc/cron.d:/etc/cron.d` mount and
set `[template_sync] cron_container = "oduflow"` to the actual container name.
See [the complete setup](templates.md#cron-when-oduflow-runs-in-docker), including
SSH source requirements. The Oduflow web process does not launch sync workers.
