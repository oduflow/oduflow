# 0008 — Licensing: Polyform Noncommercial + in-app license activation

**Status:** Adopted; current license terms are described in [[0072-polyform-commercial-licensing]] — the in-app activation model remains in force
**Type:** Product / Architecture
**First introduced:** `317b816` "add Polyform Noncommercial 1.0.0 license" (2026-02-12), `00936c9` "license activation via dashboard UI" (2026-02-13)
**Key code today:** `licensing.py` (RSA verification, license types, install), `web_ui.py` (`/api/license`, `/api/license/activate`), `templates/dashboard.html` (activation form + banner), `LICENSE`, `docs/licensing.md`

## Context

Oduflow is a real product, not just an open-source side project: it needs a
commercial model that funds development while staying source-available and
friendly to individual experimentation. Two questions had to be answered
together:

1. **Under what terms is the source available?** Fully permissive (MIT/Apache)
   gives competitors and integrators the right to build a paid business on the
   code for free; fully closed kills the trust and inspectability that a
   developer tool needs.
2. **How does a paying customer prove they hold a commercial license?** The
   product runs on the customer's own Docker host, often air-gapped or
   self-hosted, so licensing cannot depend on a phone-home check or a hosted
   control plane being reachable at runtime.

The forces: keep the code open and auditable (it orchestrates the user's
private repos and databases — trust matters), but reserve **commercial use** as
the thing you pay for, and make activation work **offline** on a box the project
operator does not control.

## Decision

Adopt the **PolyForm Noncommercial License 1.0.0** for the source, and gate
*commercial* use behind a **signed license key activated in-app**.

- **License terms.** The source is published under PolyForm Noncommercial 1.0.0
  (`LICENSE`, `pyproject.toml` `PolyForm-Noncommercial-1.0.0`). Anyone may read,
  run, and modify it for noncommercial purposes; commercial use requires a paid
  license. This makes "noncommercial-source, paid-commercial" the explicit
  business model rather than an informal convention.
- **License key = identity of commercial use.** A license is a short,
  **RSA-signed** token: a base64 signature plus a JSON payload carrying a
  `type` (`individual`, `business`, `integrator`) and the licensee `name` /
  `email`. The signature is verified **offline** against a public key compiled
  into the package, so no key server is contacted at runtime and tampering is
  rejected.
- **In-app activation.** The key is pasted into the dashboard (or installed via
  file / REST), verified, and stored locally; the running instance then reports
  itself as licensed and shows who it is licensed to.

## How it works (macro)

- **Verify, then store.** Activation verifies the key's signature and that its
  `type` is known *before* persisting it. A valid key is written to
  `license.key` in the instance config dir; an invalid or tampered key is
  rejected and the instance stays `unlicensed`.
- **Three entry points, one path.** The dashboard activation form posts the key
  text to `POST /api/license/activate`; the same effect is available by dropping
  a `license.key` file in the config dir, or via the REST endpoint directly. All
  funnel through the same verify-and-install routine in `licensing.py`.
- **Status is read on demand.** `GET /api/license` re-reads and re-verifies the
  stored key and returns a `LicenseInfo` (type, name, email, human label). An
  absent or unverifiable key yields the `unlicensed` state — there is no cached
  "trusted" flag that could survive a tampered file.
- **Banner / branding.** The dashboard surfaces license state prominently: an
  unlicensed instance shows a "NON-COMMERCIAL USE ONLY" banner; a licensed one
  shows whom it is licensed to. This is the product's honest, visible nudge
  toward purchasing rather than a hard runtime lock — the tool keeps working,
  but commercial users are reminded they need a license.

The activation UI is part of the dashboard surface described in
[[0005-web-dashboard-and-rest-api]]; licensing reuses its Starlette routes and
Basic-auth boundary rather than adding a separate control plane.

## Consequences

- Established Oduflow's **commercial model** as a first-class, documented thing:
  source-available for noncommercial use, paid for commercial use, with the
  license type baked into the key.
- Offline RSA verification means licensing **never blocks startup** and needs no
  network — important for self-hosted/air-gapped installs and consistent with
  the project's bias against runtime dependencies on project-controlled
  services.
- Activation deliberately **identifies** rather than **enforces**: the banner and
  license label inform; the product does not cripple unlicensed use. This keeps
  the tool trustworthy and the open source honest while still making commercial
  obligations visible.
- The public key is shipped in the package, so license *issuance* (signing)
  stays a private, off-repo capability; only verification is open.

## History

- `317b816` (2026-02-12) — adopt PolyForm Noncommercial 1.0.0 as the source
  license (`LICENSE`).
- `00936c9` (2026-02-13) — in-app license activation: paste-a-key dashboard form,
  signature/type validation, `install_license_from_text`, and the
  `/api/license` + `/api/license/activate` endpoints.
- `8e66e8f` (2026-02-17) — fix license load/display in the dashboard.
- `fe142ec` (2026-06-13) — store/read the key under the resolved instance config
  dir (config-path fix) so activation and startup agree on `license.key`'s
  location.
- `9d532f8` (2026-06-14) — polish the license banner branding; drop redundant
  footer product attribution.
