# Licensing

Oduflow is source-available under the [Business Source License 1.1](https://github.com/oduflow/oduflow/blob/main/LICENSE) (BUSL-1.1).

- **Free forever for non-commercial use**: evaluation, education, academic research, personal and hobby projects, non-profits.
- **Commercial use requires a paid license** in one of three tiers (below).
- Standard BUSL mechanics: each release converts to the open-source **MPL 2.0** four years after publication.

## Annual Commercial Plans

New purchases use the [Oduflow Commercial License Agreement](https://oduflow.dev/eula).
The public license shipped with each release and prior perpetual commercial
purchases retain their existing grants. Public source access and updates are
available to everyone. All plans have the same functions, including Production.

| Plan | Annual price (EUR, excluding tax) | License holder and scope |
|---|---|---|
| Free | Free | Evaluation and non-commercial use under the release license |
| Solo | 149 | One named developer, including personal client development and administration |
| Business | 499 | Named company, internal use only |
| Integrator | 999 | Named Odoo integrator's team delivering client services |
| Enterprise | By agreement | Hosting, white label, or custom support requirements |

Solo, Business and Integrator renew annually through Paddle until canceled.
Choose a plan on [oduflow.dev/pricing](https://oduflow.dev/pricing); secure checkout,
key delivery and subscription checks are hosted on `license.oduist.com`.
Canceling before renewal leaves the remainder of the paid year intact. Only
completed payment extends the licensed term. Enterprise terms are agreed individually.
Existing perpetual keys have no expiration and remain recognized as perpetual.

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

## License Details and Renewal

Click **Licensed to** in the dashboard header to see the holder, plan, status,
valid-from date and paid-through timestamp. An expired license is shown in red
with the text **LICENSE EXPIRED**, while all functions and running systems remain
available. The dashboard refreshes this local status once a minute.

An expired annual license offers **Update license status**. This is a manual
network action: the authenticated dashboard posts the installed signed key to
`https://license.oduist.com/oduflow/check_license`. The server verifies its signature
and checks completed Paddle payments for that subscription. If a newer paid term
exists, Oduflow verifies and atomically installs the renewed key. Otherwise it
keeps the existing key. No startup or background job contacts this service.

The same action is available as authenticated `POST /api/license/refresh`.
Shared environment links cannot read, activate or refresh the installation license.
A renewal key also arrives by email after each paid annual transaction.

Annual keys use the existing RSA-PSS/SHA-256 signature envelope, with signed
`version: 2`, `plan`, `scope`, `subscription_id`, `paddle_environment`, `valid_from`
and `expires` fields. `expires` is an exclusive timezone-aware timestamp. The
built-in public key remains unchanged; unsigned or altered deadlines are rejected.
