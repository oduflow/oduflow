# Quick Start

## Install

The fastest way — run directly without installing (requires [uv](https://docs.astral.sh/uv/)):

```bash
uvx oduflow
```

Or install permanently:

```bash
uv tool install oduflow
```

On first launch, Oduflow automatically:

- Creates a default `oduflow.toml` config with generated secrets
- Initializes shared infrastructure (Docker network, PostgreSQL, team directories)

The config is created at `/etc/oduflow/oduflow.toml` when that directory is
writable, otherwise at `~/.oduflow/conf/oduflow.toml`. Oduflow searches for the
config in this order:

1. `ODUFLOW_TOML` environment variable (explicit file path)
2. `/etc/oduflow/oduflow.toml`
3. `~/.oduflow/conf/oduflow.toml`

Fresh configs include generated values for:

- `[database].password` — PostgreSQL superuser password
- `[team.1].auth_token` — HTTP MCP Bearer token and OAuth client secret
- `[team.1].ui_password` — Web Dashboard password

The generated `auth_token` and `ui_password` are also printed in the startup log.

## Single-user mode (stdio)

Stdio is the default transport — Oduflow communicates with the MCP client over stdin/stdout. The client starts and manages the Oduflow process directly. No network port is needed.

```bash
# These are all equivalent:
uvx oduflow
oduflow
oduflow --transport stdio
```

Add to your MCP client config (Claude Desktop, Windsurf, etc.):

```json
{
  "mcpServers": {
    "oduflow": {
      "command": "uvx",
      "args": ["oduflow"]
    }
  }
}
```

If Oduflow is installed globally (`uv tool install oduflow`), you can use the shorter form:

```json
{
  "mcpServers": {
    "oduflow": {
      "command": "oduflow"
    }
  }
}
```

## Server mode (HTTP)

HTTP transport starts a persistent server with Streamable HTTP, a Web Dashboard, and a REST API. Suitable for remote and multi-user deployments.

```bash
# Start the HTTP server:
uvx oduflow --transport http
uvx oduflow -t http
# or, if installed:
oduflow --transport http
oduflow -t http
```

The server starts on `http://0.0.0.0:8000` by default (configurable via `[server]` section in `oduflow.toml`). The MCP endpoint is at `/mcp`.

### Authentication

Fresh HTTP installs already have a generated Bearer token for MCP and a separate
generated password for the Web Dashboard. Read them from `oduflow.toml`:

```toml
[team.1]
hostname = "localhost"
auth_token = "..."     # Bearer token for MCP clients
ui_password = "..."    # Web Dashboard password for user admin
```

To sign in to the Web Dashboard, open `http://<host>:8000/`, use username
`admin`, and enter the `ui_password` value. To connect an HTTP MCP client, use
`http://<host>:8000/mcp` with:

```
Authorization: Bearer <auth_token>
```

MCP auth and Web Dashboard auth are independent — they use different credentials
and different mechanisms (Bearer vs form/Basic auth).

### Self-hosted OAuth (Claude.ai)

Some MCP clients (e.g. Claude.ai Remote MCP) require an OAuth flow instead of a static Bearer token. Oduflow can act as its own OAuth 2.1 Authorization Server — no external identity provider needed. It is enabled automatically whenever a team has an `auth_token` and runs on that team's own hostname in both port and [traefik mode](traefik.md), so no separate OAuth URL is normally required:

```toml
[team.1]
hostname = "oduflow.example.com"
auth_token = "..."
```

Behind Cloudflare Tunnel, publish that same hostname and forward it to port 8000; use split DNS if LAN clients should reach it directly. The OAuth `client_id` is the non-secret `team_<id>` (e.g. `team_1`); each team's `auth_token` is the `client_secret`, and OAuth mints an independent expiring access token. See [Authentication & Security](security.md#self-hosted-oauth-for-claudeai-and-other-mcp-clients) for the full setup and how to connect from Claude.ai.

### MCP client configuration

Point your MCP client (Cursor, Cline, Amp, etc.) to the server with the Authorization header:

```json
{
  "mcpServers": {
    "oduflow": {
      "type": "http",
      "url": "http://your-server:8000/mcp",
      "headers": {
        "Authorization": "Bearer my-secret-mcp-token"
      }
    }
  }
}
```

If the server is behind a reverse proxy with HTTPS (see [Traefik Routing](traefik.md)):

```json
{
  "mcpServers": {
    "oduflow": {
      "type": "http",
      "url": "https://oduflow.example.com/mcp",
      "headers": {
        "Authorization": "Bearer my-secret-mcp-token"
      }
    }
  }
}
```

### Claude Desktop (remote server via `mcp-remote`)

Claude Desktop only launches MCP servers as local processes — it cannot call a
remote HTTP endpoint with a custom `Authorization` header on its own. Use the
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote) bridge: Claude Desktop
starts it over stdio, and it forwards everything to Oduflow's `/mcp` endpoint
with the Bearer token attached. Node.js (which provides `npx`) must be installed.

Edit `claude_desktop_config.json` — **Settings → Developer → Edit Config** opens
it directly:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

=== "Windows"

    ```json
    {
      "mcpServers": {
        "oduflow": {
          "command": "cmd.exe",
          "args": [
            "/c",
            "npx",
            "-y",
            "mcp-remote",
            "https://your.oduflow.server/mcp",
            "--header",
            "Authorization:${AUTH_HEADER}",
            "--transport",
            "http-only"
          ],
          "env": {
            "AUTH_HEADER": "Bearer TOKEN"
          }
        }
      }
    }
    ```

=== "macOS / Linux"

    ```json
    {
      "mcpServers": {
        "oduflow": {
          "command": "npx",
          "args": [
            "-y",
            "mcp-remote",
            "https://your.oduflow.server/mcp",
            "--header",
            "Authorization:${AUTH_HEADER}",
            "--transport",
            "http-only"
          ],
          "env": {
            "AUTH_HEADER": "Bearer TOKEN"
          }
        }
      }
    }
    ```

Replace:

- `https://your.oduflow.server/mcp` — your Oduflow MCP endpoint (in
  [traefik mode](traefik.md), the team's own hostname; in port mode,
  `http://<host>:8000/mcp`).
- `TOKEN` — the team's `auth_token` from `oduflow.toml`. Keep the `Bearer `
  prefix: the header value must read `Bearer <auth_token>`.

!!! note "Why the token lives in `env`"

    `mcp-remote` substitutes `${AUTH_HEADER}` into the `--header` value at
    startup. Keeping the secret in `env` instead of inline in `args` avoids
    both the shell-quoting problems of a space inside an argument and leaking
    the token into process listings and logs.

`--transport http-only` pins the bridge to Streamable HTTP, which is what
Oduflow serves; without it `mcp-remote` first probes for an SSE endpoint and the
connection can fail. Do **not** use the OAuth setup from
[Authentication & Security](security.md#self-hosted-oauth-for-claudeai-and-other-mcp-clients)
here — that flow is for Claude.ai custom connectors; Claude Desktop authenticates
with the static Bearer token above.

To scope the connection to a single environment, point the URL at
`https://your.oduflow.server/mcp/<env>` and use that environment's Secret Key as
the token instead — see
[Scoped single-environment access](security.md#scoped-single-environment-access-mcpenv).

After saving the file, quit Claude Desktop completely (not just close the
window) and start it again. The Oduflow tools then appear in the tools menu.

### Web Dashboard

When running in HTTP mode, a web dashboard is available at the root URL (`http://your-server:8000/`). Sign in as `admin` with the `ui_password` from `oduflow.toml`. It provides environment management, service controls, a WebSocket terminal, and more. See [Web Dashboard & REST API](web-api.md) for details.

## Next steps

- **Set up a template** — `oduflow init-template` (see [Template Management](templates.md))
- **Customize configuration** — edit `oduflow.toml` (see [Configuration Reference](installation.md#configuration-reference))
- **Auto-start on boot** — `oduflow systemd-install` (see [systemd setup](installation.md#auto-start-with-systemd))
- **Multi-team isolation** — add multiple `[team.*]` sections (see [Multi-Team Support](multi-instance.md))
