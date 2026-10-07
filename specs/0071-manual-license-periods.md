# Manual commercial periods and optional payment subscriptions

**Status:** Accepted · **Type:** Licensing identity · **First introduced:** 2026-09-23

**Key code:** `licensing.py`, dashboard license dialog; license-server grants and checkout

## Context

Five existing customers agreed with the owner to move from their original
perpetual purchases to annual plans. Their first annual period is one calendar
year from the original purchase, not from the migration date. These periods must
exist before the customer has a recurring Paddle subscription. See
[[0070-annual-commercial-licenses]].

## Decision

Give these licenses a stable ID independent of Paddle. Signed version 3 keys
carry that identity and a granted period; the subscription is optional. Version
2 subscription keys and unlisted perpetual keys remain supported.

## How it works

An idempotent import records original orders, holders, plans and anniversary
timestamps in the license server. Manual refresh verifies the installed legacy
signature and matches only an explicitly imported record. When the archive has
no key copy, the exact holder, plan and issue date must match uniquely. The client
verifies the returned signature and holder before installing the agreed term.

After expiry, the operator can request a short-lived checkout link. The signed
key travels in a POST body, never a URL. Checkout fixes the existing holder and
plan, records consent to annual renewal, and reuses a pending transaction to avoid
duplicate subscriptions. A verified completed payment binds the subscription to
the stable license identity. Subsequent refresh requires the same identity and
an increasing expiry. Admins can extend an unlinked manual grant without payment.

## Consequences

Migration does not charge customers or subscribe them automatically. No customer
email is sent by the import. Old clients must update to understand version 3;
updated clients can request their agreed key rather than receive it by email.
All checks remain explicit, and expiry never disables features. Records outside
the agreed import retain their perpetual behavior.

## History

- 2026-09-23: owner confirmed all five customers consented and selected one year
  from each original purchase as the initial term.
