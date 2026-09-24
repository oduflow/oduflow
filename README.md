<p align="center">
  <a href="https://github.com/oduflow/oduflow/actions/workflows/tests.yml"><img src="https://github.com/oduflow/oduflow/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <img src="https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/Docker-Required-2496ED?logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/Protocol-MCP-green" alt="MCP">
  <img src="https://img.shields.io/badge/License-PolyForm_Noncommercial_1.0.0-blue" alt="PolyForm Noncommercial 1.0.0">
  <img src="https://img.shields.io/badge/Odoo-15.0--19.0-714B67?logo=odoo&logoColor=white" alt="Odoo">
</p>

# Oduflow

An **AI-first** Odoo development and CI tool, powered by **reusable database templates**. Oduflow provisions isolated, ephemeral Odoo environments on Docker — one per git branch — and exposes them to AI coding agents via [MCP](https://modelcontextprotocol.io/), creating a **closed feedback loop** that enables fully autonomous Odoo development.

### Beyond Vibe Coding: Spec-Driven Development

**Vibe coding** — chatting with an AI and eyeballing the output — was the first wave. It works for prototypes, but breaks down on real ERP systems where a module must install cleanly, pass tests, and work against production data.

**Spec-Driven Development (SDD)** is the next step: you write a precise specification of *what* the module should do, and the AI agent autonomously implements *how* — because it has a **closed feedback loop** with the running system:

```
┌──────────────────────────────────────────────────────┐
│                    AI Agent                          │
│          (Cursor, Cline, Amp, Claude, …)             │
└──────┬──────────────────────────────▲────────────────┘
       │ 1. Read spec                 │ 5. Read errors,
       │ 2. Write code                │    fix code,
       │ 3. Install module via MCP    │    retry
       │ 4. Click-test UI via         │
       │    Playwright MCP            │
┌──────▼──────────────────────────────┴────────────────┐
│               Oduflow (MCP Server)                   │
│  • install_odoo_modules → traceback or success       │
│  • run_odoo_tests → test pass/fail with details      │
│  • get_environment_logs → runtime errors             │
│  • upgrade_odoo_modules → upgrade output             │
├──────────────────────────────────────────────────────┤
│            + Playwright MCP / other tools            │
│  • Navigate Odoo UI, click buttons, fill forms       │
│  • Verify business logic end-to-end                  │
│  • Validate acceptance criteria from the spec        │
└──────────────────────────────────────────────────────┘
```

The agent writes code, installs the module, reads the traceback, fixes the error, retries — and when it installs cleanly, it can open the browser via [Playwright MCP](https://github.com/anthropics/mcp-playwright) to click through the UI, verify business flows, and validate acceptance criteria — **all without human intervention**. `connect_as_user` closes the last gap: it mints a passwordless Odoo session and hands back the cookie, so Playwright lands past `/web/login` as any role (admin, sales manager, portal) — no credentials to type, no login form.

| | Vibe Coding | Spec-Driven Development |
|---|---|---|
| **Input** | Conversational prompts | Formal specification with acceptance criteria |
| **Feedback** | Human eyeballs the code | System returns errors, test results, and UI state automatically |
| **Iteration** | Human copy-pastes errors back | Agent retries autonomously via MCP |
| **Scope** | Single files, prototypes | Full modules against real databases |
| **Verification** | "Looks right" | Module installs, tests pass, UI works on production data |

---

## Quick Start

### 1. Install

```bash
pip install oduflow
```

### 2. Start the MCP server

```bash
oduflow
```

That's it. On first launch, Oduflow automatically creates a default config and initializes shared infrastructure (Docker network, PostgreSQL, team directories).

The generated config is written to `/etc/oduflow/oduflow.toml` when writable,
otherwise to `~/.oduflow/conf/oduflow.toml`. Fresh configs include generated
secrets for HTTP access: `[team.1].auth_token` for MCP clients and
`[team.1].ui_password` for the Web Dashboard. The file is created with mode
`0600`; the secrets are never printed to the log, so read them from the config
itself (`sudo grep -E 'auth_token|ui_password' /etc/oduflow/oduflow.toml`).

By default, the server starts in **stdio** mode (for local MCP clients). For remote/multi-user deployments:

```bash
oduflow --transport http
oduflow -t http
```

### 3. Connect an MCP client

**stdio (local)** — add to your MCP client config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "oduflow": {
      "command": "oduflow"
    }
  }
}
```

**HTTP (remote)** — point your MCP client to `http://<host>:8000/mcp` and send
`Authorization: Bearer <auth_token>` using the token from `oduflow.toml`.

The bundled CLI can call that remote MCP server without a separate agent or MCP
client configuration:

```bash
export ODUFLOW_MCP_URL="http://<host>:8000/mcp"
export ODUFLOW_MCP_TOKEN="<auth_token>"
oduflow client list_environments
```

The Web Dashboard is available at `http://<host>:8000/`. Sign in as `admin`
with the `ui_password` from `oduflow.toml`; this is separate from the MCP Bearer
token.

---

## Documentation

For full documentation, visit **[oduflow.dev](https://oduflow.dev)** or see the [`docs/`](docs/) folder:

- [Installation & Configuration](docs/installation.md)
- [Use Cases & Workflows](docs/use-cases.md)
- [Template Management](docs/templates.md)
- [Environment Management](docs/environments.md)
- [Coding Agent](docs/agent.md)
- [Auxiliary Services](docs/services.md)
- [Extra Addons Repositories](docs/extra-addons.md)
- [Web Dashboard & REST API](docs/web-api.md)
- [MCP Tools Reference](docs/mcp-tools.md)
- [CLI Reference](docs/cli.md)
- [Traefik Routing (Auto-HTTPS)](docs/traefik.md)
- [Multi-Instance Support](docs/multi-instance.md)
- [Authentication & Security](docs/security.md)
- [Running in Docker](docs/docker.md)
- [Internals](docs/internals.md)
- [Licensing](docs/licensing.md)

---

## Telemetry

Oduflow collects anonymous usage telemetry (first startup and environment creation events) to understand adoption. Only the event name, version, and a random instance ID are sent — no personal data or environment details. To opt out, add `disable_telemetry = true` to the `[server]` section of your `oduflow.toml`. See the [documentation](https://oduflow.dev/installation/#telemetry) for details.

## Licensing

Oduflow is source-available under [PolyForm Noncommercial License 1.0.0](LICENSE).
Noncommercial purposes and the organizations covered by that license may use it
for free. A separate [internal evaluation permission](EVALUATION-LICENSE.md)
allows companies to assess Oduflow before buying.

Use outside those permissions requires an alternative
[commercial license](COMMERCIAL-LICENSE.md): Solo, Business, Integrator, or
Enterprise. Read the [Oduflow Commercial License Agreement](https://oduflow.dev/eula)
and [compare plans](https://oduflow.dev/pricing). All plans include the same code,
features and public updates. Earlier releases retain their original licenses.
