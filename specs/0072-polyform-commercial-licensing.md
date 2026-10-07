# 0072 - PolyForm source license and alternative commercial agreements

**Status:** Adopted
**Type:** Product / Legal
**First introduced:** 2026-09-23, v1.84.0
**Key code:** `LICENSE`, `NOTICE`, `COMMERCIAL-LICENSE.md`, `EVALUATION-LICENSE.md`, `pyproject.toml`, dashboard license dialog

## Context

The annual model in [[0070-annual-commercial-licenses]] now has a separate
commercial agreement on oduflow.dev/eula. The owner explicitly chose PolyForm
Noncommercial for public source and alternative commercial agreements for paid
use. Source access, features and updates remain shared by every plan.

## Decision

Use the unmodified PolyForm Noncommercial License 1.0.0 for new releases, with
the copyright required notice shipped alongside it. Use the website's Oduflow
Commercial License Agreement and each accepted order for paid commercial rights.
Provide a separate, limited internal evaluation permission so businesses can
assess the product before buying without altering the standard PolyForm text.

## How it works

The source license covers its standard noncommercial and organizational
permissions. Commercial use outside those permissions or isolated evaluation
requires Solo, Business, Integrator or Enterprise rights. The package includes
all license notices and a guide to the commercial agreement. The dashboard's
license dialog links to that agreement beside the validity and renewal controls.

## Consequences

- The public source license and commercial agreement define separate grants.
- Previously published releases keep their original terms; earlier
  perpetual commercial agreements also remain effective.
- Standard PolyForm organizational permissions are not narrowed by marketing
  summaries or the commercial agreement.
- Key verification, manual renewal and the absence of feature restrictions stay
  as described in [[0071-manual-license-periods]].

## History

- 2026-09-23 - owner confirms the PolyForm/commercial split and requests an
  agreement link in the registered product's license dialog.
