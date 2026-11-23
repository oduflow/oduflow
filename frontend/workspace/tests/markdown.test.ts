// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { renderMarkdown } from '../src/lib/markdown';

describe('markdown', () => {
  it('renders markdown', () => {
    expect(renderMarkdown('**done**')).toContain('<strong>done</strong>');
  });

  it('removes scripts, event handlers and script URLs', () => {
    const html = renderMarkdown(
      '<script>alert(1)</script><img src=x onerror="alert(1)">[x](javascript:alert(1)) <a href="java&Tab;script:alert(1)">y</a>'
    );
    expect(html).not.toContain('<script');
    expect(html).not.toContain('onerror');
    expect(html).not.toMatch(/href="\s*java/i);
  });

  it('drops SVG and forms', () => {
    const html = renderMarkdown('<svg><a xlink:href="javascript:alert(1)">x</a></svg><form action="/x"></form>');
    expect(html).not.toContain('<svg');
    expect(html).not.toContain('<form');
  });

  it('opens links outside the workspace', () => {
    expect(renderMarkdown('[docs](https://docs.oduflow.dev)')).toContain(
      'target="_blank" rel="noopener noreferrer"'
    );
  });
});
