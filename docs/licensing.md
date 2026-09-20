# Licensing

Oduflow is source-available under the [Business Source License 1.1](https://github.com/oduflow/oduflow/blob/main/LICENSE) (BUSL-1.1).

- **Free forever for non-commercial use**: evaluation, education, academic research, personal and hobby projects, non-profits.
- **Commercial use requires a paid license** in one of three tiers (below).
- Standard BUSL mechanics: each release converts to the open-source **MPL 2.0** four years after publication.

## License Types

| Type | Label | Who needs it |
|---|---|---|
| `unlicensed` | UNLICENSED — NON-COMMERCIAL USE ONLY | Default when no license key is installed; fine for evaluation, education, and other non-commercial use |
| `individual` | Licensed to individual | One natural person (freelancer, sole developer) using Oduflow commercially on their own account |
| `business` | Licensed to company (internal use only) | A company using Oduflow internally, for its own Odoo systems |
| `integrator` | Licensed to Odoo integrator | A person or company using Oduflow to deliver Odoo services (implementation, development, support, hosting) to clients |

### Business vs. Integrator

The test is whose Odoo systems you point Oduflow at. If the environments you develop, test, and operate serve your own organization, a Business license covers you. If they belong to, or are used by, your clients — you are an integrator and need an Integrator license, regardless of company size.

## Installing a License

**Via CLI:**

Copy the license file to `<config-dir>/license.key`. The config directory is usually `/etc/oduflow`; when that path is not writable, Oduflow uses `~/.oduflow/conf`. Oduflow reads the license automatically on startup.

**Via Web Dashboard:**

Navigate to the dashboard and use the license activation form. The license key text can be pasted directly.

**Via REST API:**

```bash
curl -X POST http://localhost:8000/api/license/activate \
  -H "Content-Type: application/json" \
  -d '{"key": "<license-key-text>"}'
```

## Checking License Status

```bash
# Via REST API
curl http://localhost:8000/api/license

# Via Web Dashboard — license info is displayed in the dashboard header
```

License keys are RSA-signed and verified against a built-in public key. Invalid or tampered keys are rejected.

---

For business use or integrator licenses, visit [oduflow.dev](https://oduflow.dev).
