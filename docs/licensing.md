# Licensing

Starting with v1.84.0, Oduflow is source-available under
[PolyForm Noncommercial License 1.0.0](https://github.com/oduflow/oduflow/blob/main/LICENSE)
(`PolyForm-Noncommercial-1.0.0`).

- **Free public license:** noncommercial purposes, personal uses and the
  organizations covered by the standard license, including its education,
  research, public-sector and charitable-organization permissions.
- **Free internal evaluation:** a separate
  [evaluation permission](https://github.com/oduflow/oduflow/blob/main/EVALUATION-LICENSE.md)
  lets a business assess Oduflow in isolation before buying. It does not cover
  live business operations, commercial project development or paid client work.
- **Alternative commercial license:** Solo, Business, Integrator or Custom
  covers use outside those permissions, under the agreement and purchased scope.

The standard PolyForm text is unmodified; preserve its required notice from
`NOTICE` when distributing copies. Third-party components retain their own terms.
PolyForm releases have no automatic conversion to an open-source license.
Earlier releases retain their original terms. Existing perpetual commercial
grants remain in effect.

## Annual Commercial Plans

New purchases use the [Oduflow Commercial License Agreement](https://oduflow.dev/eula).
The public license shipped with each release and prior perpetual commercial
purchases retain their existing grants. Public source access and updates are
available to everyone. All plans have the same functions, including Production.

| Plan | Annual price (EUR, excluding tax) | License holder and scope |
|---|---|---|
| Free | Free | Public-license permissions and the separate internal evaluation grant |
| Solo | 149 | One named developer, including personal client development and administration |
| Business | 499 | Named company, internal use only |
| Integrator | 999 | Named Odoo integrator's team delivering client services |
| Custom | By agreement | Hosting, white label, or custom support requirements |

Solo, Business and Integrator renew annually through Paddle until canceled.
Choose a plan on [oduflow.dev/pricing](https://oduflow.dev/pricing); secure checkout,
key delivery and subscription checks are hosted on `license.oduist.com`.
Canceling before renewal leaves the remainder of the paid year intact. Only
completed payment extends the licensed term. Custom terms are agreed individually.
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
valid-from date, paid-through timestamp and a link to the
[Oduflow Commercial License Agreement](https://oduflow.dev/eula).
An expired license is shown in red
with the text **LICENSE EXPIRED**, while all functions and running systems remain
available. The dashboard refreshes this local status once a minute.

An expired annual license offers **Update license status**. This is a manual
network action: the authenticated dashboard posts the installed signed key to
`https://license.oduist.com/oduflow/check_license`. The server verifies its signature
and checks the completed payments for that license. If a newer paid term
exists, Oduflow verifies and atomically installs the renewed key. Otherwise it
keeps the existing key. Apart from the daily check for an expired
[Custom license](#custom-license-white-label), no startup or background job
contacts this service.

The same action is available as authenticated `POST /api/license/refresh`.
Shared environment links cannot read, activate or refresh the installation license.
A renewal key also arrives by email after each paid annual transaction.

Annual keys use the existing RSA-PSS/SHA-256 signature envelope, with signed
`plan`, `license_id`, `valid_from` and `expires` fields. `expires` is an exclusive
timezone-aware timestamp. The built-in public key remains unchanged; unsigned or
altered deadlines are rejected. How a period was paid is known only to the license
server: Oduflow ignores any payment-provider fields in older keys.


## Agreed migration and manually granted periods

A commercial license can have a manually granted period before a Paddle
subscription exists. For customers who agreed to migrate, the license server
records one calendar year from the original archived purchase timestamp. The
license has a stable identity independent of its later Paddle subscription.

After updating Oduflow, click the license banner and **Update license status** to
retrieve the agreed term. Only explicitly imported legacy holders can migrate;
other perpetual keys keep their existing status. The server verifies the old
signature and matches the holder, plan and issue date when the original archive
has no key copy. No periodic network check is added.

When **Update license status** reports that no subscription is active after a
manual period ends, **Subscribe annually** opens a short-lived checkout
link with the existing holder and plan. The customer accepts the annual terms
and pays through Paddle. The completed payment attaches the subscription to the
same license. **Update license status** then installs the paid extension. An
administrator can also extend an unlinked manual period from the license server's
Oduflow admin page. Expiration never disables product features.

## Custom License (White Label)

The Custom plan is for operators who run Oduflow for their own clients on servers
they control, for example as a hosted service under their own name. Terms, price
and allowed domains are agreed individually; you then receive a personal payment
link, and the license key arrives by email after payment and after every annual
renewal.

With an active Custom license:

- The dashboard, sign-in page, browser title and MCP server show your **brand
  name** instead of "Oduflow". Tool descriptions, instructions and messages use
  it too.
- Vendor surfaces are not rendered: the "a product by Oduist" byline, **Docs**,
  **Feedback**, the release check behind the version number (the version stays
  visible), and the license banner and dialog. The `report_issue` and
  `submit_agent_feedback` MCP tools are hidden.
- Your clients see nothing about licensing, also after expiry.

Technical names on your servers keep the product name: the `oduflow` package and
CLI, `oduflow.toml`, container, volume and database names, and paths. Avoid a
dashboard hostname like `oduflow.<base_domain>`: set an explicit `hostname` in each
`[team.*]` section.

### Installing on your servers

Copy the key and, optionally, your images into the config directory
(`/etc/oduflow` or `~/.oduflow/conf`) of every server:

```text
<config-dir>/license.key
<config-dir>/branding/logo.png   # header and sign-in logo, shown about 28–36 px high
<config-dir>/branding/icon.png   # square, at least 180×180: browser tab and home-screen icon
```

`icon.png` falls back to `logo.png`; without either, the stock images are used.
A custom logo keeps its colors in the light theme. Install and check with:

```bash
oduflow license install ./license.key
oduflow license status
```

Restart the service afterwards so MCP clients see the brand; the dashboard picks
up license and image changes within a minute.

### Domains

The key lists the domains it is valid for. White label is active only when the
`hostname` of every team equals one of these domains or is a subdomain of one, so
a copied key does not brand someone else's installation. `oduflow license status`
explains why white label is off.

### Renewal and expiry

The subscription renews annually and each renewal emails a new key. When the
installed key expires, Oduflow checks the license server once a day and installs
the renewed key automatically; `oduflow license refresh` does the same on demand.
White label stays active for **30 days** after expiry. After that the product
shows stock Oduflow branding and the expired license banner until a renewed key is
installed. Nothing is ever disabled. If the subscription was canceled,
`oduflow license subscribe` prints a new payment link.

