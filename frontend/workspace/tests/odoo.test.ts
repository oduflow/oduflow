import { describe, expect, it } from 'vitest';
import { connectUrl, odooPath } from '../src/lib/odoo';

describe('odoo links', () => {
  it('routes links into the environment through Connect As', () => {
    expect(connectUrl('feature/x')).toBe('/api/environments/feature%2Fx/connect-open');
    expect(connectUrl('main', '/odoo/action-12?view=list#x')).toBe(
      '/api/environments/main/connect-open?next=%2Fodoo%2Faction-12%3Fview%3Dlist%23x'
    );
  });

  it('recognizes only links to the environment itself', () => {
    const env = 'https://main.acme.example.com';
    expect(odooPath('https://main.acme.example.com/odoo/contacts/7', env)).toBe('/odoo/contacts/7');
    expect(odooPath('/web#action=1', env)).toBe('/web#action=1');
    expect(odooPath('https://other.acme.example.com/odoo', env)).toBeNull();
    expect(odooPath('https://docs.odoo.com', env)).toBeNull();
    expect(odooPath('https://main.acme.example.com/odoo', '')).toBeNull();
  });

  it('tells environments on one host apart by port', () => {
    expect(odooPath('http://localhost:8069/odoo', 'http://localhost:8070')).toBeNull();
    expect(odooPath('http://localhost:8070/odoo', 'http://localhost:8070')).toBe('/odoo');
  });
});
