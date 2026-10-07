# 0075 - Custom license: white-label branding for operators

**Status:** Adopted
**Type:** Product / Licensing
**First introduced:** 2026-10-07
**Key code:** `licensing.py`, `branding.py`, `web_ui.py` (`_apply_branding`), `server.py` (`_apply_branding`, `handle_errors`, `oduflow license`), `templates/dashboard.html`, `templates/login.html`; license server custom grants

## Context

The commercial plans covered individuals, companies and integrators. Operators
who want to run Oduflow for their own clients, on servers they control and under
their own name (in effect a hosted product), had only an unnamed "Enterprise, by
agreement" line on the pricing page. They need their clients to see their brand,
not Oduflow, so that end users do not go looking for the upstream product, and so
the operator can set their own prices and marketing. See
[[0070-annual-commercial-licenses]], [[0071-manual-license-periods]] and
[[0072-polyform-commercial-licensing]].

## Decision

Add a fourth license type, **custom** (plan "Custom", replacing the "Enterprise"
label everywhere), whose signed key carries a `brand_name` and the `domains` it
is valid for. While it is in effect Oduflow shows the brand instead of its name
and removes vendor surfaces. The product is not renamed: the package, CLI,
configuration file and infrastructure names stay `oduflow`. The owner chose the
smallest maintainable change over hiding every technical trace.

The license client no longer knows about the payment provider: it verifies the
signature, type and dates, and asks the license server whether a new
subscription is needed.

## How it works (macro)

- **Issuance.** The license server owns custom records (holder, brand, domains,
  agreed annual price). The admin sends a personal payment link; checkout,
  renewal and emailed keys reuse the annual flow. Custom keys are a
  provider-neutral version 4 with no subscription or provider fields.
- **When branding applies.** The key is valid, not before its period, at most
  30 days past expiry, and every team hostname is one of its domains or a
  subdomain. A copied key therefore brands nothing outside the operator's
  domains. Otherwise the product looks like stock Oduflow; nothing is disabled.
- **Dashboard.** Template blocks for vendor surfaces (byline, Docs, Feedback,
  release dialog, license banner and dialog) are removed server-side; the name,
  title and images come from the license and `<config>/branding/{logo,icon}.png`.
  The version stays visible as plain text. License endpoints answer 404.
- **MCP.** At startup the server name, instructions and tool descriptions are
  rebranded; tool results and error messages are rebranded per call in
  `handle_errors`. Vendor tools (`report_issue`, `submit_agent_feedback`) are
  hidden. Only the capitalized product name is replaced.
- **Operator tooling.** `oduflow license status|install|refresh|subscribe` is the
  license interface, since clients never see one. During the grace period the
  server checks the license server once a day and installs a paid renewal, so
  clients notice nothing when the subscription renews.

## Consequences

- Clients of an operator see the brand in the UI, sign-in page, MCP and agent
  instructions. Technical identifiers (`oduflow.toml`, container and database
  names, `.oduflow/` repo files, URL paths, cookies) may still show the product
  name in tool output and logs; this was accepted to keep the change small.
- The grace period plus the daily check replace "all checks remain explicit"
  from [[0071-manual-license-periods]] for custom licenses only.
- After the grace period, stock branding and the expired banner return, which is
  the incentive to pay; enforcement beyond that is contractual (the code is
  source-available).
- Installing or renewing a key reaches MCP clients after a service restart; the
  dashboard follows within a minute.

## History

- 2026-10-07 - owner chose the name "Custom", the website plan rename, domain
  binding, a 30-day grace period with a daily renewal check, the CLI for
  operators and hiding all license UI from operators' clients; hiding container
  and database names was dropped as too costly. License server PR
  oduist/license_server#8, website PR oduflow/oduflow-dev#31.
