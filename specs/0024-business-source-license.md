# 0024 — Relicense to Business Source License 1.1 with three commercial tiers

**Status:** Superseded for new releases by [[0072-polyform-commercial-licensing]]; historical BSL grants remain in effect
**Type:** Product / Legal
**First introduced:** 2026-07-02 (this change)
**Key code today:** `LICENSE`, `pyproject.toml` (`BUSL-1.1`), `docs/licensing.md`, `README.md`; activation machinery unchanged in `licensing.py`

## Context

[[0008-licensing]] adopted PolyForm Noncommercial 1.0.0: source-available,
free for noncommercial use, paid for commercial use, with RSA-signed keys
carrying a `type` of `individual` / `business` / `integrator`. Two gaps grew
over time:

1. **The tiers had no legal counterpart.** The three license types existed in
   the activation machinery and on the pricing page, but the license text
   itself knew only "noncommercial vs commercial". A prospective customer
   reading `LICENSE` couldn't tell *which* license they needed — in
   particular, the business-vs-integrator boundary (using Oduflow for your
   own Odoo vs using it to serve clients) was undefined anywhere binding.
2. **PolyForm is a niche license.** Business Source License 1.1 (MariaDB,
   HashiCorp, and others) is the recognized industry standard for
   source-available-with-paid-production-use, with well-understood mechanics
   and an SPDX identifier — less friction for legal review at customers.

## Decision

Replace PolyForm Noncommercial 1.0.0 with **Business Source License 1.1**,
using the canonical MariaDB text and encoding the commercial model in the
parameters:

- **Additional Use Grant** permits production use for *Non-Commercial
  Purposes* — evaluation/trial, education and academic research, personal or
  hobby projects, and non-profit/government use — perpetually and free of
  charge. (BUSL's base grant already covers copying, modification,
  redistribution, and non-production use.)
- Any other production use is a *Commercial Purpose* and requires one of
  **three commercial tiers**, defined in the grant text and matching the key
  types verified by `licensing.py`:
  - **Individual** — one natural person (freelancer, sole developer) using
    Oduflow commercially on their own account;
  - **Business** — a legal entity using Oduflow internally, for Odoo systems
    used by the entity itself and its affiliates;
  - **Integrator** — a person or entity using Oduflow to provide Odoo
    services (implementation, integration, development, migration,
    maintenance, support, hosting) to third parties. The business/integrator
    test is *whose Odoo systems Oduflow is used for*: your own → Business;
    your clients' → Integrator, regardless of organization size.
- **Change Date / Change License:** four years after each version is
  published, that version converts to **MPL 2.0**. A change date and a
  GPL-compatible change license are mandatory under MariaDB's Covenants of
  Licensor — eventual open-sourcing of each release is inherent to adopting
  BSL, not an optional extra.

## How it works (macro)

- BUSL applies separately to each publicly distributed version; its own
  four-year clock starts on that version's first public distribution.
- Non-production and the enumerated non-commercial production uses remain
  available under the Additional Use Grant. Other production use is licensed
  through the Individual, Business, or Integrator agreement matching who
  operates the Odoo systems and for whose benefit.
- At the end of a version's four-year period, that version automatically
  transitions to MPL 2.0; newer versions continue on their independent clocks.

## Consequences

- The commercial taxonomy is now part of the legal terms rather than only
  activation UX; `LICENSE`, `pyproject.toml`, `README.md`, and
  `docs/licensing.md` all state BUSL-1.1 consistently.
- Each release becomes open source (MPL 2.0) four years after publication —
  the deliberate trade-off of using standard BSL instead of a bespoke
  perpetual-proprietary license. The current release is always under the
  commercial terms.
- The activation model from [[0008-licensing]] is unchanged: offline RSA
  verification of signed keys typed `individual` / `business` / `integrator`,
  banner-based nudging, no runtime enforcement.

## History

- 2026-07-02 — relicense `LICENSE` to BUSL-1.1; update `pyproject.toml`,
  `README.md`, `docs/licensing.md`; supersedes the license-terms half of
  [[0008-licensing]].
