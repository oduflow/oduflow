// Standing instructions for the agent, sent once at the start of each
// workspace conversation.
//
// The person in the workspace is usually not a developer, so the agent should
// answer in plain words and show what it changed instead of describing code.
// ACP has no portable "system prompt" for a session, so this travels as a
// leading text block of the first prompt, fenced by markers. Both chats
// (this one and the dashboard's static/chat.js) strip the fenced block when
// they show the user's message, including when a conversation is replayed.

export const CONTEXT_OPEN = '<oduflow-workspace-context>';
export const CONTEXT_CLOSE = '</oduflow-workspace-context>';

const INSTRUCTIONS = [
  'This note comes from the workspace page, not from the user.',
  'The person you are talking to is probably not a developer: they are changing this Odoo environment by chatting with you.',
  "- Reply in the user's language, in plain words. Do not show code or technical detail unless they ask for it.",
  '- Apply your changes to the running environment yourself (install or upgrade the modules involved) so the user can try them right away.',
  '- After a change that is visible in Odoo, open it in the browser to check it, and end your reply with a short summary, a screenshot of the result, and a link to that screen in this environment.',
  '- When the request is ambiguous, ask one short clarifying question before building.',
].join('\n');

export function workspaceContextBlock(): { type: 'text'; text: string } {
  return { type: 'text', text: CONTEXT_OPEN + '\n' + INSTRUCTIONS + '\n' + CONTEXT_CLOSE + '\n\n' };
}

export function hasWorkspaceContext(text: string): boolean {
  return text.indexOf(CONTEXT_OPEN) !== -1;
}

/**
 * The user's own words: drop every fenced context block, and an unterminated
 * one at the end (a replay may stream it in pieces).
 */
export function stripWorkspaceContext(text: string): string {
  if (!hasWorkspaceContext(text)) return text;
  let result = text;
  for (;;) {
    const start = result.indexOf(CONTEXT_OPEN);
    if (start === -1) break;
    const end = result.indexOf(CONTEXT_CLOSE, start);
    if (end === -1) {
      result = result.slice(0, start);
      break;
    }
    result = result.slice(0, start) + result.slice(end + CONTEXT_CLOSE.length);
  }
  return result.replace(/^\s+/, '');
}
