// Opening Odoo from the workspace, already signed in.
//
// The visitor of a share link has no Odoo password, so every way into Odoo goes
// through the environment's Connect As bridge (`.../connect-open`): it mints a
// session server-side and lands the browser in the environment logged in (see
// specs/0036-cross-subdomain-connect-as-landing.md). `next` keeps the screen a
// link pointed at.

import { envApiPath } from './api';

export function connectUrl(envName: string, next = ''): string {
  const url = envApiPath(envName) + '/connect-open';
  return next ? url + '?next=' + encodeURIComponent(next) : url;
}

/**
 * The in-environment path of a link that points into this environment's Odoo,
 * or null for any other link.
 */
export function odooPath(href: string, envUrl: string): string | null {
  if (!envUrl) return null;
  let target: URL;
  let base: URL;
  try {
    base = new URL(envUrl);
    target = new URL(href, base);
  } catch {
    return null;
  }
  if (target.origin !== base.origin) return null;
  return target.pathname + target.search + target.hash;
}

export function openOdoo(envName: string, next = ''): void {
  window.open(connectUrl(envName, next), '_blank', 'noopener');
}
