# Annual commercial licenses and manual subscription renewal

**Status:** Accepted · **Type:** Licensing / runtime UX · **First introduced:** 2026-09-23

**Key code:** `licensing.py`, dashboard license dialog, `/api/license/refresh`

## Context

Oduflow moves new commercial purchases to annual Solo, Business and Integrator
subscriptions while keeping the full codebase and features publicly available.
Operators must be able to see the paid term and pick up a successful renewal
without a technical dependency on the payment provider for normal operation.
Legacy perpetual purchases must remain valid. See [[0008-licensing]] and
[[0024-business-source-license]]. The public source-license choice is separate.

## Decision

Keep the existing RSA-PSS license envelope and introduce a versioned annual
payload with the license holder, plan, scope, Paddle subscription and environment,
and exact paid-period timestamps. Expiration changes the displayed status only.
It never blocks development, production, backup, deployment or recovery.

## How it works

The marketing website links to checkout on license.oduist.com. The license server
owns orders, Paddle webhooks, the D1 payment ledger, issuance and delivery email.
It issues a signed key after a completed annual payment, using Paddle's paid
billing period. The local dashboard reads that key without external calls.
Clicking its badge opens license details. For an expired subscription, an operator
can explicitly request an update through the authenticated dashboard endpoint.

The license server verifies the installed key, looks up its Paddle subscription
and completed payments, and signs an extension only when a matching newer paid
period exists. A canceled subscription keeps its already paid term. Pending,
failed, refunded or unrelated payments do not authorize a new term. The client
verifies signature, identity, plan, subscription, environment and increasing
expiry before atomically replacing the local file.

For orders created on the license server, manual renewal recovers missed webhook
payments into the same ledger and uses the same issuer as the initial purchase.
The website has no payment-provider or signing credentials.

## Consequences

Normal operation remains offline-capable. Network or payment-provider failures
leave the installed key untouched. A key is a credential and is never put in a
URL or request trace. Existing keys without an expiry remain perpetual. No new
instance-registration database or automatic telemetry is introduced.

## History

- 2026-09-23: annual plans and explicit manual renewal agreed with the product owner.
