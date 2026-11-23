// Agent output is markdown rendered to HTML, and it can echo prompt-injected
// content, so marked's raw-HTML output MUST go through DOMPurify before it
// reaches the DOM. Same policy as the dashboard chat (static/chat.js).
import DOMPurify from 'dompurify';
import { marked } from 'marked';

let hooked = false;

function ensureHook(): void {
  if (hooked) return;
  // Every link the agent writes opens outside the workspace.
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A') {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
    }
  });
  hooked = true;
}

function escapeText(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function renderMarkdown(text: string): string {
  let html: string;
  try {
    html = marked.parse(text, { async: false, breaks: true });
  } catch {
    return escapeText(text);
  }
  ensureHook();
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true }, // HTML only: no SVG/MathML (kills xlink:href)
    ADD_ATTR: ['target', 'rel'],
    FORBID_TAGS: ['style', 'form'],
  });
}
