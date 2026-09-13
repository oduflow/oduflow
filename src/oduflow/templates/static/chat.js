// ACP chat UI + window manager for the dashboard. Framework-free, vendored
// under /static and lazy-loaded on first "Chat" open (like xterm.js), so the
// dashboard stays a single no-build page. Depends on acp-client.js (protocol)
// and marked.min.js (markdown). See specs/0029-agent-console-and-chat.md.
//
// Model: one maximized chat + a bottom dock of minimized ones. Each chat is a
// live instance owning its own DOM subtree, AcpClient and WebSocket. Minimizing
// parks the subtree (WS stays alive → the agent keeps working); restoring moves
// it back into the modal; closing (x) tears it down. Sessions are durable per
// (branch, agent): opening resumes the stored session via ACP session/load.
(function () {
  'use strict';

  var AGENT_LABELS = { claude: 'Claude', codex: 'Codex', opencode: 'OpenCode' };
  var STATUS = {
    connecting: { label: 'Connecting…', cls: 'is-dim' },
    ready: { label: 'Ready', cls: 'is-green' },
    thinking: { label: 'Working…', cls: 'is-accent' },
    permission: { label: 'Needs approval', cls: 'is-yellow' },
    error: { label: 'Error', cls: 'is-red' },
    closed: { label: 'Disconnected', cls: 'is-red' }
  };

  var instances = {};      // key -> instance
  var activeKey = null;
  var nextDomId = 0;
  var FOLLOW_BOTTOM_THRESHOLD = 48;
  var DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024;
  var DEFAULT_MAX_FILES = 5;
  var INLINE_IMAGE_MAX_BYTES = 5 * 1024 * 1024;

  function keyOf(branch, type) { return branch + ' ' + type; }
  function labelOf(type) { return AGENT_LABELS[type] || type; }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // -- markdown (agent output) sanitized with DOMPurify -----------------------
  // marked emits raw HTML (agent output can echo prompt-injected content), so
  // its output MUST be sanitized before it reaches innerHTML. DOMPurify (vendored
  // under /static/purify.min.js, loaded before this file) is a vetted sanitizer:
  // a hand-rolled pass here was bypassable via entity-encoded scheme whitespace
  // (java&Tab;script:) and SVG xlink:href.
  var _purifyHookAdded = false;
  function _ensurePurifyHook() {
    if (_purifyHookAdded || !window.DOMPurify) return;
    // Force external-safe link attributes on every anchor DOMPurify keeps.
    window.DOMPurify.addHook('afterSanitizeAttributes', function(node) {
      if (node.tagName === 'A') {
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', 'noopener noreferrer');
      }
    });
    _purifyHookAdded = true;
  }

  function renderMarkdown(text) {
    var html;
    try {
      html = window.marked ? window.marked.parse(text, { breaks: true }) : null;
    } catch (e) { html = null; }
    // Never inject unsanitized HTML: if marked failed or DOMPurify is unavailable,
    // fall back to plain escaped text.
    if (html == null || !window.DOMPurify) {
      var d = el('div');
      d.textContent = text;
      return d.innerHTML;
    }
    _ensurePurifyHook();
    return window.DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true },   // HTML only — no SVG/MathML (kills xlink:href)
      ADD_ATTR: ['target', 'rel'],
      FORBID_TAGS: ['style', 'form']
    });
  }

  function contentText(content) {
    if (!content) return '';
    if (typeof content === 'string') return content;
    if (content.type === 'text') return content.text || '';
    return '';
  }

  function fileNameFromUri(uri) {
    var tail = String(uri || '').split('/').pop() || 'Attachment';
    try { return decodeURIComponent(tail); } catch (e) { return tail; }
  }

  function attachmentFromContent(content) {
    if (!content || typeof content !== 'object') return null;
    var meta = content._meta && content._meta.oduflow;
    if (content.type === 'resource_link') {
      return {
        name: content.name || fileNameFromUri(content.uri),
        uri: content.uri || '',
        path: (meta && meta.path) || '',
        mimeType: content.mimeType || '',
        size: content.size
      };
    }
    if (content.type === 'image' || content.type === 'audio') {
      return {
        name: (meta && meta.name) || fileNameFromUri(content.uri),
        uri: content.uri || '',
        path: (meta && meta.path) || '',
        mimeType: content.mimeType || '',
        size: meta && meta.size
      };
    }
    if (content.type === 'resource' && content.resource) {
      return {
        name: (meta && meta.name) || fileNameFromUri(content.resource.uri),
        uri: content.resource.uri || '',
        path: (meta && meta.path) || '',
        mimeType: content.resource.mimeType || '',
        size: meta && meta.size
      };
    }
    return null;
  }

  function formatBytes(size) {
    if (typeof size !== 'number' || !isFinite(size) || size < 0) return '';
    if (size < 1024) return size + ' B';
    if (size < 1024 * 1024) return Math.round(size / 1024) + ' KiB';
    return (size / (1024 * 1024)).toFixed(size < 10 * 1024 * 1024 ? 1 : 0) + ' MiB';
  }

  // -- DOM helpers scoped to an instance -------------------------------------
  function q(inst, sel) { return inst.el.querySelector(sel); }

  function updateFollowOutput(inst) {
    var m = q(inst, '.chat-messages');
    if (!m) return;
    var distance = m.scrollHeight - m.scrollTop - m.clientHeight;
    inst.followOutput = distance <= FOLLOW_BOTTOM_THRESHOLD;
  }

  function scrollToBottom(inst, force) {
    var m = q(inst, '.chat-messages');
    if (!m) return;
    if (force) inst.followOutput = true;
    if (inst.followOutput) m.scrollTop = m.scrollHeight;
  }

  function newTurn() {
    return {
      activity: null,
      candidateWrap: null,
      candidateBubble: null,
      candidateBuffer: '',
      streamType: null,
      thoughtBox: null,
      thoughtBuffer: '',
      planBox: null
    };
  }

  function currentTurn(inst) {
    if (!inst.turn) inst.turn = newTurn();
    return inst.turn;
  }

  function finishUserChunks(inst) {
    inst.userChunkBubble = null;
    inst.userChunkBuffer = '';
    inst.userChunkText = null;
    inst.userChunkAttachments = null;
    inst.userChunkAttachmentKeys = Object.create(null);
  }

  function pluralCount(count, singular, plural) {
    return count + ' ' + (count === 1 ? singular : plural);
  }

  function updateActivitySummary(activity) {
    var parts = [];
    if (activity.toolCount) parts.push(pluralCount(activity.toolCount, 'tool call', 'tool calls'));
    if (activity.messageCount) parts.push(pluralCount(activity.messageCount, 'message', 'messages'));
    activity.label.textContent = parts.length ? parts.join(', ') : 'Details';

    var failed = false;
    var running = false;
    for (var i = 0; i < activity.tools.length; i++) {
      var status = activity.tools[i].status;
      if (status === 'failed') failed = true;
      if (status === 'pending' || status === 'in_progress' || !status) running = true;
    }
    var statusText = failed ? 'failed' : (running ? 'running' : '');
    activity.status.textContent = statusText ? ' · ' + statusText : '';
    activity.status.setAttribute('data-status', statusText || 'completed');
  }

  function ensureActivity(inst) {
    var turn = currentTurn(inst);
    if (turn.activity) return turn.activity;

    var details = el('details', 'chat-activity');
    var summary = el('summary', 'chat-activity-summary');
    var label = el('span', 'chat-activity-label', 'Details');
    var status = el('span', 'chat-activity-state');
    summary.appendChild(label);
    summary.appendChild(status);
    details.appendChild(summary);
    var body = el('div', 'chat-activity-body');
    details.appendChild(body);

    var messages = q(inst, '.chat-messages');
    if (turn.candidateWrap && turn.candidateWrap.parentNode === messages) {
      messages.insertBefore(details, turn.candidateWrap);
    } else {
      messages.appendChild(details);
    }
    turn.activity = {
      el: details,
      label: label,
      status: status,
      body: body,
      toolCount: 0,
      messageCount: 0,
      messages: [],
      tools: []
    };
    return turn.activity;
  }

  function archiveCandidate(inst) {
    var turn = currentTurn(inst);
    if (!turn.candidateWrap) return;
    var activity = ensureActivity(inst);
    activity.body.appendChild(turn.candidateWrap);
    activity.messages.push(turn.candidateWrap);
    activity.messageCount += 1;
    updateActivitySummary(activity);
    turn.candidateWrap = null;
    turn.candidateBubble = null;
    turn.candidateBuffer = '';
  }

  function finalizeTurn(inst) {
    if (!inst.turn) return;
    var turn = inst.turn;
    var activity = turn.activity;
    if (activity && !turn.candidateWrap && activity.messages.length) {
      var finalWrap = activity.messages.pop();
      var messages = q(inst, '.chat-messages');
      messages.insertBefore(finalWrap, activity.el.nextSibling);
      activity.messageCount -= 1;
    }
    if (activity) updateActivitySummary(activity);
    inst.turn = null;
    finishUserChunks(inst);
    scrollToBottom(inst);
  }

  function renderAttachmentRow(attachment) {
    var row = el('div', 'chat-attachment');
    row.appendChild(el('span', 'chat-attachment-name', attachment.name || 'Attachment'));
    var meta = [];
    var size = formatBytes(attachment.size);
    if (size) meta.push(size);
    if (attachment.mimeType) meta.push(attachment.mimeType);
    if (meta.length) row.appendChild(el('span', 'chat-attachment-meta', meta.join(' · ')));
    return row;
  }

  function appendAttachments(container, attachments) {
    if (!attachments || !attachments.length) return;
    var list = el('div', 'chat-attachments');
    for (var i = 0; i < attachments.length; i++) {
      list.appendChild(renderAttachmentRow(attachments[i]));
    }
    container.appendChild(list);
  }

  function addUser(inst, text, attachments) {
    finalizeTurn(inst);
    var wrap = el('div', 'chat-msg chat-msg-user');
    var bubble = el('div', 'chat-bubble');
    bubble.appendChild(el('div', 'chat-user-text', text));
    appendAttachments(bubble, attachments);
    wrap.appendChild(bubble);
    q(inst, '.chat-messages').appendChild(wrap);
    finishUserChunks(inst);
    scrollToBottom(inst, true);
  }

  function ensureUserChunk(inst) {
    if (!inst.userChunkBubble) {
      finalizeTurn(inst);
      var wrap = el('div', 'chat-msg chat-msg-user');
      inst.userChunkBubble = el('div', 'chat-bubble');
      inst.userChunkBuffer = '';
      inst.userChunkText = null;
      inst.userChunkAttachments = null;
      inst.userChunkAttachmentKeys = Object.create(null);
      wrap.appendChild(inst.userChunkBubble);
      q(inst, '.chat-messages').appendChild(wrap);
    }
  }

  function appendUserText(inst, text) {
    if (!text) return;
    ensureUserChunk(inst);
    if (!inst.userChunkText) {
      inst.userChunkText = el('div', 'chat-user-text');
      inst.userChunkBubble.insertBefore(inst.userChunkText, inst.userChunkBubble.firstChild);
    }
    inst.userChunkBuffer += text;
    inst.userChunkText.textContent = inst.userChunkBuffer;
    scrollToBottom(inst);
  }

  function appendUserAttachment(inst, attachment) {
    if (!attachment) return;
    ensureUserChunk(inst);
    var key = attachment.uri || (attachment.name + ' ' + attachment.mimeType);
    if (inst.userChunkAttachmentKeys[key]) return;
    inst.userChunkAttachmentKeys[key] = true;
    if (!inst.userChunkAttachments) {
      inst.userChunkAttachments = el('div', 'chat-attachments');
      inst.userChunkBubble.appendChild(inst.userChunkAttachments);
    }
    inst.userChunkAttachments.appendChild(renderAttachmentRow(attachment));
    scrollToBottom(inst);
  }

  function appendUserContent(inst, content) {
    var text = contentText(content);
    if (text) appendUserText(inst, text);
    else appendUserAttachment(inst, attachmentFromContent(content));
  }

  function appendAgent(inst, text) {
    if (!text) return;
    finishUserChunks(inst);
    var turn = currentTurn(inst);
    if (turn.streamType !== 'agent' || !turn.candidateBubble) {
      var wrap = el('div', 'chat-msg chat-msg-agent');
      var bubble = el('div', 'chat-bubble chat-md');
      wrap.appendChild(bubble);
      q(inst, '.chat-messages').appendChild(wrap);
      turn.candidateWrap = wrap;
      turn.candidateBubble = bubble;
      turn.candidateBuffer = '';
    }
    turn.streamType = 'agent';
    turn.candidateBuffer += text;
    turn.candidateBubble.innerHTML = renderMarkdown(turn.candidateBuffer);
    scrollToBottom(inst);
  }

  function appendThought(inst, text) {
    if (!text) return;
    finishUserChunks(inst);
    var turn = currentTurn(inst);
    archiveCandidate(inst);
    if (turn.streamType !== 'thought' || !turn.thoughtBox) {
      var activity = ensureActivity(inst);
      var det = el('details', 'chat-thinking');
      det.appendChild(el('summary', null, 'Reasoning'));
      var body = el('div', 'chat-thinking-body');
      det.appendChild(body);
      activity.body.appendChild(det);
      turn.thoughtBox = body;
      turn.thoughtBuffer = '';
    }
    turn.streamType = 'thought';
    turn.thoughtBuffer += text;
    turn.thoughtBox.textContent = turn.thoughtBuffer;
    scrollToBottom(inst);
  }

  function toolStatusText(s) {
    if (s === 'completed') return 'done';
    if (s === 'failed') return 'failed';
    if (s === 'in_progress') return 'running';
    return 'pending';
  }

  function hasOwn(obj, name) {
    return Object.prototype.hasOwnProperty.call(obj, name);
  }

  function formatToolValue(value) {
    if (typeof value === 'string') return value;
    try {
      var json = JSON.stringify(value, null, 2);
      return json === undefined ? String(value) : json;
    } catch (e) {
      return String(value);
    }
  }

  function toolOutputValue(tool) {
    if (tool.hasRawOutput) return tool.rawOutput;
    if (!tool.hasContent) return undefined;
    if (Array.isArray(tool.content) && tool.content.length === 1) {
      var item = tool.content[0];
      if (item && item.type === 'content' && item.content && item.content.type === 'text') {
        return item.content.text || '';
      }
    }
    return tool.content;
  }

  function renderToolPayload(tool) {
    tool.input.textContent = tool.hasRawInput
      ? formatToolValue(tool.rawInput)
      : 'Not provided by agent';
    var output = toolOutputValue(tool);
    tool.output.textContent = output === undefined
      ? 'Not provided by agent'
      : formatToolValue(output);
  }

  function patchTool(tool, u) {
    var wasFailed = tool.status === 'failed';
    if (hasOwn(u, 'title') && u.title != null) tool.title = u.title;
    if (hasOwn(u, 'kind') && u.kind != null) tool.kind = u.kind;
    if (hasOwn(u, 'status') && u.status != null) tool.status = u.status;
    if (hasOwn(u, 'rawInput')) {
      tool.hasRawInput = true;
      tool.rawInput = u.rawInput;
    }
    if (hasOwn(u, 'rawOutput')) {
      tool.hasRawOutput = true;
      tool.rawOutput = u.rawOutput;
    }
    if (hasOwn(u, 'content')) {
      tool.hasContent = true;
      tool.content = u.content;
    }

    tool.titleEl.textContent = tool.title || tool.kind || 'Tool';
    tool.statusEl.textContent = toolStatusText(tool.status);
    tool.statusEl.setAttribute('data-status', tool.status || 'pending');
    renderToolPayload(tool);
    updateActivitySummary(tool.activity);
    if (tool.status === 'failed' && !wasFailed) {
      tool.activity.el.open = true;
      tool.el.open = true;
    }
  }

  function createTool(inst, u) {
    finishUserChunks(inst);
    var turn = currentTurn(inst);
    archiveCandidate(inst);
    turn.streamType = 'tool';
    turn.thoughtBox = null;
    turn.thoughtBuffer = '';
    var activity = ensureActivity(inst);

    var details = el('details', 'chat-tool');
    var head = el('summary', 'chat-tool-head');
    var title = el('span', 'chat-tool-title');
    var status = el('span', 'chat-tool-status');
    head.appendChild(title);
    head.appendChild(status);
    details.appendChild(head);

    var payload = el('div', 'chat-tool-payload');
    var inputSection = el('div', 'chat-tool-payload-section');
    inputSection.appendChild(el('div', 'chat-tool-payload-label', 'Input'));
    var input = el('pre', 'chat-tool-payload-value');
    inputSection.appendChild(input);
    payload.appendChild(inputSection);
    var outputSection = el('div', 'chat-tool-payload-section');
    outputSection.appendChild(el('div', 'chat-tool-payload-label', 'Output'));
    var output = el('pre', 'chat-tool-payload-value');
    outputSection.appendChild(output);
    payload.appendChild(outputSection);
    details.appendChild(payload);
    activity.body.appendChild(details);

    var tool = {
      el: details,
      titleEl: title,
      statusEl: status,
      input: input,
      output: output,
      activity: activity,
      title: null,
      kind: null,
      status: null,
      hasRawInput: false,
      rawInput: undefined,
      hasRawOutput: false,
      rawOutput: undefined,
      hasContent: false,
      content: undefined
    };
    activity.tools.push(tool);
    activity.toolCount += 1;
    inst.tools[u.toolCallId] = tool;
    patchTool(tool, u);
    scrollToBottom(inst);
    return tool;
  }

  function renderToolCall(inst, u) {
    var tool = inst.tools[u.toolCallId];
    if (tool) {
      patchTool(tool, u);
      scrollToBottom(inst);
    } else {
      createTool(inst, u);
    }
  }

  function updateToolCall(inst, u) {
    var tool = inst.tools[u.toolCallId];
    if (!tool) { createTool(inst, u); return; }
    patchTool(tool, u);
    scrollToBottom(inst);
  }

  function renderPlan(inst, u) {
    finishUserChunks(inst);
    var turn = currentTurn(inst);
    archiveCandidate(inst);
    var entries = u.entries || u.plan || [];
    var box = turn.planBox;
    if (!box) {
      box = el('div', 'chat-plan');
      ensureActivity(inst).body.appendChild(box);
      turn.planBox = box;
    }
    box.innerHTML = '';
    box.appendChild(el('div', 'chat-plan-title', 'Plan'));
    var list = el('ul', 'chat-plan-list');
    for (var i = 0; i < entries.length; i++) {
      var li = el('li');
      li.setAttribute('data-status', entries[i].status || 'pending');
      li.textContent = contentText(entries[i].content) || entries[i].content || '';
      list.appendChild(li);
    }
    box.appendChild(list);
    turn.streamType = 'plan';
    turn.thoughtBox = null;
    turn.thoughtBuffer = '';
    scrollToBottom(inst);
  }

  function addSystemLine(inst, text) {
    q(inst, '.chat-messages').appendChild(el('div', 'chat-system', text));
    scrollToBottom(inst);
  }

  function addErrorLine(inst, text) {
    q(inst, '.chat-messages').appendChild(el('div', 'chat-system chat-error', text));
    scrollToBottom(inst);
  }

  // -- status + dock ----------------------------------------------------------
  function setStatus(inst, status) {
    inst.status = status;
    var meta = STATUS[status] || STATUS.ready;
    var badge = q(inst, '.chat-status');
    if (badge) {
      badge.textContent = meta.label;
      badge.className = 'chat-status ' + meta.cls;
    }
    updatePill(inst);
    updateComposerState(inst);
  }

  function setBusy(inst, busy) {
    inst.busy = busy;
    var stop = q(inst, '.chat-stop');
    if (stop) stop.hidden = !busy;
    updateComposerState(inst);
  }

  function updatePill(inst) {
    if (!inst.pill) return;
    var meta = STATUS[inst.status] || STATUS.ready;
    inst.pill.querySelector('.chat-pill-dot').className = 'chat-pill-dot ' + meta.cls;
    inst.pill.querySelector('.chat-pill-state').textContent = meta.label;
    inst.pill.classList.toggle('has-unread', !!inst.unread);
  }

  function addPill(inst) {
    var dock = document.getElementById('chat-dock');
    var pill = el('button', 'chat-pill');
    pill.type = 'button';
    pill.title = 'Restore chat — ' + inst.branch;
    pill.appendChild(el('span', 'chat-pill-dot'));
    var body = el('span', 'chat-pill-body');
    body.appendChild(el('span', 'chat-pill-name', inst.branch));
    var meta = el('span', 'chat-pill-meta');
    meta.appendChild(el('span', 'chat-pill-agent', labelOf(inst.type)));
    meta.appendChild(el('span', 'chat-pill-state', (STATUS[inst.status] || STATUS.ready).label));
    body.appendChild(meta);
    pill.appendChild(body);
    var close = el('span', 'chat-pill-close', '×');
    close.setAttribute('role', 'button');
    close.setAttribute('aria-label', 'Close chat');
    close.title = 'Close chat';
    close.onclick = function (ev) { ev.stopPropagation(); closeChat(inst.key); };
    pill.appendChild(close);
    pill.onclick = function () { restoreChat(inst.key); };
    dock.appendChild(pill);
    inst.pill = pill;
    updatePill(inst);
  }

  function removePill(inst) {
    if (inst.pill && inst.pill.parentNode) inst.pill.parentNode.removeChild(inst.pill);
    inst.pill = null;
  }

  // -- permission -------------------------------------------------------------
  function renderPermission(inst, requestId, params) {
    var area = q(inst, '.chat-permission');
    area.innerHTML = '';
    var tc = params.toolCall || {};
    area.appendChild(el('div', 'chat-perm-title', 'Approve: ' + (tc.title || 'action') + '?'));
    var opts = params.options || [];
    var row = el('div', 'chat-perm-actions');
    for (var i = 0; i < opts.length; i++) {
      (function (opt) {
        var b = el('button', 'chat-perm-btn' + (opt.kind && opt.kind.indexOf('allow') === 0 ? ' primary' : ''));
        b.type = 'button';
        b.textContent = opt.name || opt.optionId;
        b.onclick = function () {
          inst.client.respondPermission(requestId, opt.optionId);
          area.hidden = true;
          area.innerHTML = '';
          if (inst.busy) setStatus(inst, 'thinking');
        };
        row.appendChild(b);
      })(opts[i]);
    }
    area.appendChild(row);
    area.hidden = false;
    setStatus(inst, 'permission');
    if (inst.key !== activeKey) { inst.unread = true; updatePill(inst); }
    scrollToBottom(inst);
  }

  // -- update dispatch --------------------------------------------------------
  function handleUpdate(inst, params) {
    var u = params && params.update;
    if (!u) return;
    switch (u.sessionUpdate) {
      case 'user_message_chunk': appendUserContent(inst, u.content); break;
      case 'agent_message_chunk': appendAgent(inst, contentText(u.content)); break;
      case 'agent_thought_chunk': appendThought(inst, contentText(u.content)); break;
      case 'tool_call': renderToolCall(inst, u); break;
      case 'tool_call_update': updateToolCall(inst, u); break;
      case 'plan': renderPlan(inst, u); break;
      default: break; // available_commands_update, current_mode_update, ...
    }
  }

  // -- models -----------------------------------------------------------------
  function applyModels(inst, models) {
    if (!models || !models.availableModels || !models.availableModels.length) return;
    var sel = q(inst, '.chat-model');
    sel.innerHTML = '';
    for (var i = 0; i < models.availableModels.length; i++) {
      var m = models.availableModels[i];
      var opt = el('option', null, m.name || m.modelId);
      opt.value = m.modelId;
      if (m.modelId === models.currentModelId) opt.selected = true;
      sel.appendChild(opt);
    }
    sel.hidden = false;
    sel.onchange = function () {
      if (inst.sessionId) {
        inst.client.setModel(inst.sessionId, sel.value)
          .then(function (res) { applySessionModels(inst, res); })
          .catch(function () {});
      }
    };
  }

  function flattenConfigOptions(options, result) {
    if (!Array.isArray(options)) return result;
    for (var i = 0; i < options.length; i++) {
      var option = options[i];
      if (!option) continue;
      if (Array.isArray(option.options)) {
        flattenConfigOptions(option.options, result);
      } else if (option.value != null) {
        result.push({ modelId: String(option.value), name: option.name || String(option.value) });
      }
    }
    return result;
  }

  function sessionModels(res) {
    if (!res) return null;
    if (res.models) return res.models;
    var configOptions = res.configOptions;
    if (!Array.isArray(configOptions)) return null;
    for (var i = 0; i < configOptions.length; i++) {
      var option = configOptions[i];
      if (!option || (option.id !== 'model' && option.category !== 'model')) continue;
      var available = flattenConfigOptions(option.options || [], []);
      if (!available.length) return null;
      return {
        availableModels: available,
        currentModelId: String(option.currentValue || '')
      };
    }
    return null;
  }

  function applySessionModels(inst, res) {
    var models = sessionModels(res);
    if (models) applyModels(inst, models);
  }

  function applyPromptCapabilities(inst, initializeResult) {
    var capabilities = initializeResult && initializeResult.agentCapabilities;
    inst.promptCapabilities = (capabilities && capabilities.promptCapabilities) || {};
  }

  // -- REST helpers -----------------------------------------------------------
  function apiBase(branch) {
    return '/api/environments/' + encodeURIComponent(branch) + '/agent-acp';
  }
  function wsUrlFor(branch, type) {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + location.host + apiBase(branch) + '?type=' + encodeURIComponent(type);
  }
  function fetchInfo(branch, type) {
    return fetch(apiBase(branch) + '/info?type=' + encodeURIComponent(type), { credentials: 'same-origin' })
      .then(function (r) { return r.json(); });
  }
  function postSession(branch, type, sessionId, title) {
    var body = { type: type, session_id: sessionId || '' };
    if (title !== undefined) body.title = title;
    return fetch(apiBase(branch) + '/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); })
      .catch(function () { return null; });
  }

  function setComposerError(inst, message) {
    var error = q(inst, '.chat-composer-error');
    var ta = q(inst, '.chat-input');
    if (error) {
      error.textContent = message || '';
      error.hidden = !message;
    }
    if (ta) ta.setAttribute('aria-invalid', message ? 'true' : 'false');
  }

  function attachmentStateText(attachment) {
    if (attachment.state === 'queued') return 'queued';
    if (attachment.state === 'uploading') return 'uploading ' + (attachment.progress || 0) + '%';
    if (attachment.state === 'ready') return 'ready';
    return 'failed' + (attachment.error ? ': ' + attachment.error : '');
  }

  function renderPendingAttachments(inst) {
    var list = q(inst, '.chat-composer-attachments');
    if (!list) return;
    list.innerHTML = '';
    list.hidden = !inst.attachments.length;
    for (var i = 0; i < inst.attachments.length; i++) {
      (function (attachment) {
        var row = el('div', 'chat-pending-attachment');
        row.setAttribute('data-status', attachment.state);
        var body = el('div', 'chat-pending-body');
        body.appendChild(el('span', 'chat-attachment-name', attachment.name));
        var details = [];
        var size = formatBytes(attachment.size);
        if (size) details.push(size);
        details.push(attachmentStateText(attachment));
        body.appendChild(el('span', 'chat-pending-meta', details.join(' · ')));
        row.appendChild(body);
        var remove = el('button', 'chat-attachment-remove', 'Remove');
        remove.type = 'button';
        remove.onclick = function () { removePendingAttachment(inst, attachment); };
        row.appendChild(remove);
        list.appendChild(row);
      })(inst.attachments[i]);
    }
    updateComposerState(inst);
  }

  function updateComposerState(inst) {
    if (!inst.el) return;
    var ta = q(inst, '.chat-input');
    var send = q(inst, '.chat-send');
    var attach = q(inst, '.chat-attach');
    var hasText = !!(ta && (ta.value || '').trim());
    var blocked = false;
    for (var i = 0; i < inst.attachments.length; i++) {
      if (inst.attachments[i].state !== 'ready') blocked = true;
    }
    if (send) send.disabled = inst.busy || !inst.sessionId || !hasText || blocked;
    if (attach) attach.disabled = !inst.cwd || !inst.sessionId ||
      inst.status === 'connecting' || inst.status === 'closed' ||
      inst.attachments.length >= inst.maxFiles;
  }

  function deletePendingUpload(inst, attachment) {
    if (!attachment.id) return;
    fetch(apiBase(inst.branch) + '/attachments/' + encodeURIComponent(attachment.id), {
      method: 'DELETE',
      credentials: 'same-origin'
    }).catch(function () {});
  }

  function removePendingAttachment(inst, attachment) {
    attachment.removed = true;
    var idx = inst.attachments.indexOf(attachment);
    if (idx !== -1) inst.attachments.splice(idx, 1);
    if (attachment.xhr) {
      try { attachment.xhr.abort(); } catch (e) { /* already complete */ }
    }
    if (attachment.state === 'ready') deletePendingUpload(inst, attachment);
    setComposerError(inst, '');
    renderPendingAttachments(inst);
    processUploadQueue(inst);
  }

  function clearPendingAttachments(inst, removeRemote) {
    var pending = inst.attachments.slice();
    inst.attachments = [];
    for (var i = 0; i < pending.length; i++) {
      pending[i].removed = true;
      if (pending[i].xhr) {
        try { pending[i].xhr.abort(); } catch (e) { /* already complete */ }
      }
      if (removeRemote && pending[i].state === 'ready') deletePendingUpload(inst, pending[i]);
    }
    inst.uploadActive = null;
    setComposerError(inst, '');
    renderPendingAttachments(inst);
  }

  function finishUpload(inst, attachment) {
    attachment.xhr = null;
    if (inst.uploadActive === attachment) inst.uploadActive = null;
    renderPendingAttachments(inst);
    processUploadQueue(inst);
  }

  function processUploadQueue(inst) {
    if (inst.uploadActive) return;
    var attachment = null;
    for (var i = 0; i < inst.attachments.length; i++) {
      if (inst.attachments[i].state === 'queued') {
        attachment = inst.attachments[i];
        break;
      }
    }
    if (!attachment) { updateComposerState(inst); return; }

    inst.uploadActive = attachment;
    attachment.state = 'uploading';
    attachment.progress = 0;
    var xhr = new XMLHttpRequest();
    attachment.xhr = xhr;
    xhr.open('POST', apiBase(inst.branch) + '/attachments?name=' + encodeURIComponent(attachment.name));
    xhr.setRequestHeader('Content-Type', attachment.mimeType || 'application/octet-stream');
    xhr.upload.onprogress = function (event) {
      if (!event.lengthComputable || attachment.removed) return;
      attachment.progress = Math.min(99, Math.round(event.loaded * 100 / event.total));
      renderPendingAttachments(inst);
    };
    xhr.onload = function () {
      var payload = null;
      try { payload = JSON.parse(xhr.responseText || '{}'); } catch (e) { /* invalid response */ }
      if (xhr.status >= 200 && xhr.status < 300 && payload && payload.attachment) {
        var uploaded = payload.attachment;
        attachment.id = uploaded.id;
        attachment.path = uploaded.path;
        attachment.uri = uploaded.uri;
        attachment.mimeType = uploaded.mimeType || attachment.mimeType;
        attachment.size = uploaded.size;
        attachment.state = 'ready';
        attachment.progress = 100;
        if (attachment.removed) deletePendingUpload(inst, attachment);
      } else {
        attachment.state = 'failed';
        attachment.error = (payload && payload.error) || 'Upload failed';
      }
      finishUpload(inst, attachment);
    };
    xhr.onerror = function () {
      attachment.state = 'failed';
      attachment.error = 'Network error';
      finishUpload(inst, attachment);
    };
    xhr.onabort = function () { finishUpload(inst, attachment); };
    renderPendingAttachments(inst);
    xhr.send(attachment.file);
  }

  function addFiles(inst, files) {
    var available = inst.maxFiles - inst.attachments.length;
    if (available <= 0) {
      setComposerError(inst, 'You can attach up to ' + inst.maxFiles + ' files.');
      return;
    }
    var count = Math.min(files.length, available);
    for (var i = 0; i < count; i++) {
      var file = files[i];
      var attachment = {
        name: file.name || 'attachment',
        mimeType: file.type || 'application/octet-stream',
        size: file.size,
        file: file,
        state: file.size > inst.maxFileBytes ? 'failed' : 'queued',
        error: file.size > inst.maxFileBytes
          ? 'Larger than ' + formatBytes(inst.maxFileBytes) + ' limit'
          : ''
      };
      inst.attachments.push(attachment);
    }
    if (files.length > count) {
      setComposerError(inst, 'You can attach up to ' + inst.maxFiles + ' files.');
    } else {
      setComposerError(inst, '');
    }
    renderPendingAttachments(inst);
    processUploadQueue(inst);
  }

  function resourceLinkBlock(attachment) {
    return {
      type: 'resource_link',
      name: attachment.name,
      uri: attachment.uri,
      mimeType: attachment.mimeType,
      size: attachment.size,
      _meta: { oduflow: { id: attachment.id, path: attachment.path } }
    };
  }

  function attachmentPromptBlocks(attachment, canInlineImage) {
    var resourceLink = resourceLinkBlock(attachment);
    if (!canInlineImage) return Promise.resolve([resourceLink]);
    // Keep the link even when image bytes are inlined: ACP adapters commonly
    // discard image URI/_meta fields, but preserve resource links as text.
    return new Promise(function (resolve) {
      var reader = new FileReader();
      reader.onload = function () {
        var dataUrl = String(reader.result || '');
        var comma = dataUrl.indexOf(',');
        if (comma === -1) { resolve([resourceLink]); return; }
        resolve([
          resourceLink,
          {
            type: 'image',
            data: dataUrl.slice(comma + 1),
            mimeType: attachment.mimeType,
            uri: attachment.uri,
            _meta: {
              oduflow: {
                id: attachment.id,
                name: attachment.name,
                path: attachment.path,
                size: attachment.size
              }
            }
          }
        ]);
      };
      reader.onerror = function () { resolve([resourceLink]); };
      reader.readAsDataURL(attachment.file);
    });
  }

  function buildPromptBlocks(inst, text, attachments) {
    var groups = [Promise.resolve([{ type: 'text', text: text }])];
    var inlineImageBytes = 0;
    for (var i = 0; i < attachments.length; i++) {
      var attachment = attachments[i];
      var canInlineImage = inst.promptCapabilities.image &&
        attachment.mimeType.indexOf('image/') === 0 && attachment.file &&
        inlineImageBytes + attachment.size <= INLINE_IMAGE_MAX_BYTES;
      if (canInlineImage) inlineImageBytes += attachment.size;
      groups.push(attachmentPromptBlocks(attachment, canInlineImage));
    }
    return Promise.all(groups).then(function (resolvedGroups) {
      var blocks = [];
      for (var groupIndex = 0; groupIndex < resolvedGroups.length; groupIndex++) {
        var group = resolvedGroups[groupIndex];
        for (var blockIndex = 0; blockIndex < group.length; blockIndex++) {
          blocks.push(group[blockIndex]);
        }
      }
      return blocks;
    });
  }

  function refreshHistory(inst, res) {
    if (res && Array.isArray(res.history)) inst.history = res.history;
    var current = null;
    for (var i = 0; i < inst.history.length; i++) {
      if (inst.history[i].session_id === inst.sessionId) {
        current = inst.history[i];
        break;
      }
    }
    inst.titled = !!(current && current.title);
    renderHistoryMenu(inst);
  }

  function titleFrom(text) {
    return (text.split(/\r?\n/, 1)[0] || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  }

  // -- instance construction --------------------------------------------------
  function buildInstanceDom(inst) {
    var root = el('div', 'chat-instance');

    var sub = el('div', 'chat-subheader');
    var left = el('div', 'chat-sub-left');
    var status = el('span', 'chat-status is-dim', 'Connecting…');
    left.appendChild(status);
    var model = el('select', 'chat-model');
    model.hidden = true;
    model.setAttribute('aria-label', 'Model');
    left.appendChild(model);
    sub.appendChild(left);

    var historyWrap = el('div', 'menu-wrap chat-history-wrap');
    var historyBtn = el('button', 'chat-new chat-history-toggle', 'History');
    historyBtn.type = 'button';
    historyBtn.setAttribute('aria-haspopup', 'menu');
    historyBtn.setAttribute('aria-expanded', 'false');
    historyBtn.onclick = function (event) { renderHistoryMenu(inst); toggleMenu(event); };
    historyWrap.appendChild(historyBtn);
    var historyMenu = el('div', 'menu chat-history-menu');
    historyMenu.setAttribute('role', 'menu');
    historyWrap.appendChild(historyMenu);
    sub.appendChild(historyWrap);

    var newBtn = el('button', 'chat-new', 'New conversation');
    newBtn.type = 'button';
    newBtn.title = 'End this conversation and start a fresh one';
    newBtn.onclick = function () { startNewConversation(inst); };
    sub.appendChild(newBtn);
    root.appendChild(sub);

    var messages = el('div', 'chat-messages');
    messages.setAttribute('role', 'log');
    messages.setAttribute('aria-live', 'polite');
    messages.addEventListener('scroll', function () { updateFollowOutput(inst); }, { passive: true });
    root.appendChild(messages);

    var perm = el('div', 'chat-permission');
    perm.hidden = true;
    root.appendChild(perm);

    var form = el('form', 'chat-composer');
    var pending = el('div', 'chat-composer-attachments');
    pending.hidden = true;
    pending.setAttribute('aria-live', 'polite');
    form.appendChild(pending);
    var composerMain = el('div', 'chat-composer-main');
    var ta = el('textarea', 'chat-input');
    ta.rows = 2;
    ta.placeholder = 'Message the agent…  (Enter to send, Shift+Enter for a new line)';
    ta.setAttribute('aria-describedby', 'chat-composer-help-' + inst.domId);
    composerMain.appendChild(ta);
    var actions = el('div', 'chat-composer-actions');
    var fileInput = el('input', 'chat-file-input');
    fileInput.type = 'file';
    fileInput.multiple = true;
    fileInput.hidden = true;
    fileInput.setAttribute('aria-label', 'Choose files to attach');
    actions.appendChild(fileInput);
    var attach = el('button', 'btn chat-attach', 'Attach');
    attach.type = 'button';
    attach.onclick = function () { fileInput.click(); };
    actions.appendChild(attach);
    var send = el('button', 'btn chat-send', 'Send');
    send.type = 'submit';
    var stop = el('button', 'btn chat-stop', 'Stop');
    stop.type = 'button';
    stop.hidden = true;
    stop.onclick = function () { if (inst.sessionId) inst.client.cancel(inst.sessionId); setBusy(inst, false); setStatus(inst, 'ready'); };
    actions.appendChild(stop);
    actions.appendChild(send);
    composerMain.appendChild(actions);
    form.appendChild(composerMain);
    var error = el('div', 'chat-composer-error');
    error.id = 'chat-composer-help-' + inst.domId;
    error.hidden = true;
    error.setAttribute('aria-live', 'polite');
    form.appendChild(error);
    form.onsubmit = function (e) { e.preventDefault(); sendPrompt(inst); };
    fileInput.onchange = function () {
      addFiles(inst, fileInput.files || []);
      fileInput.value = '';
    };
    ta.addEventListener('input', function () {
      if ((ta.value || '').trim()) setComposerError(inst, '');
      updateComposerState(inst);
    });
    ta.addEventListener('paste', function (event) {
      var files = event.clipboardData && event.clipboardData.files;
      if (files && files.length) addFiles(inst, files);
    });
    ta.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendPrompt(inst); }
    });
    form.addEventListener('dragover', function (event) {
      event.preventDefault();
      form.classList.add('is-dragover');
    });
    form.addEventListener('dragleave', function (event) {
      if (!form.contains(event.relatedTarget)) form.classList.remove('is-dragover');
    });
    form.addEventListener('drop', function (event) {
      event.preventDefault();
      form.classList.remove('is-dragover');
      var files = event.dataTransfer && event.dataTransfer.files;
      if (files && files.length) addFiles(inst, files);
    });
    root.appendChild(form);

    inst.el = root;
    renderHistoryMenu(inst);
    updateComposerState(inst);
  }

  function renderHistoryMenu(inst) {
    if (!inst.el) return;
    var menu = q(inst, '.chat-history-menu');
    if (!menu) return;
    menu.innerHTML = '';
    if (!inst.history.length) {
      var empty = el('button', 'chat-history-item', 'No previous conversations');
      empty.type = 'button';
      empty.disabled = true;
      empty.setAttribute('role', 'menuitem');
      menu.appendChild(empty);
      return;
    }
    for (var i = 0; i < inst.history.length; i++) {
      (function (entry) {
        var current = entry.session_id === inst.sessionId;
        var item = el('button', 'chat-history-item');
        item.type = 'button';
        item.setAttribute('role', 'menuitem');
        item.disabled = current;
        item.appendChild(el('span', 'chat-history-title', entry.title || 'Untitled conversation'));
        var meta = el('span', 'chat-history-meta');
        var timestamp = entry.last_used_at || entry.created_at;
        meta.appendChild(el('span', null, timestamp ? relTime(timestamp) : 'Time unavailable'));
        if (current) meta.appendChild(el('span', 'chat-history-current', 'Current'));
        item.appendChild(meta);
        item.onclick = function () {
          closeMenus();
          switchToSession(inst, entry.session_id);
        };
        menu.appendChild(item);
      })(inst.history[i]);
    }
  }

  function resetTranscript(inst) {
    clearPendingAttachments(inst, true);
    inst.turn = null;
    finishUserChunks(inst);
    inst.tools = Object.create(null);
    inst.followOutput = true;
    q(inst, '.chat-messages').innerHTML = '';
  }

  function sendPrompt(inst) {
    var ta = q(inst, '.chat-input');
    var text = (ta.value || '').trim();
    if (inst.busy || !inst.sessionId) return;
    if (!text) {
      setComposerError(inst, inst.attachments.length
        ? 'Describe what the agent should do with the attached files.'
        : 'Enter a message for the agent.');
      ta.focus();
      return;
    }
    for (var i = 0; i < inst.attachments.length; i++) {
      if (inst.attachments[i].state === 'failed') {
        setComposerError(inst, 'Remove failed attachments before sending.');
        return;
      }
      if (inst.attachments[i].state !== 'ready') {
        setComposerError(inst, 'Wait for attachments to finish uploading.');
        return;
      }
    }

    var attachments = inst.attachments.slice();
    addUser(inst, text, attachments);
    ta.value = '';
    inst.attachments = [];
    renderPendingAttachments(inst);
    setComposerError(inst, '');
    setBusy(inst, true);
    setStatus(inst, 'thinking');
    var prompt = buildPromptBlocks(inst, text, attachments).then(function (blocks) {
      return inst.client.prompt(inst.sessionId, blocks);
    });
    if (!inst.titled && inst.sessionId) {
      inst.titled = true;
      postSession(inst.branch, inst.type, inst.sessionId, titleFrom(text))
        .then(function (res) { refreshHistory(inst, res); });
    }
    prompt
      .catch(function (e) { addErrorLine(inst, e.message || String(e)); })
      .then(function () {
        finalizeTurn(inst);
        setBusy(inst, false);
        if (inst.status !== 'closed') setStatus(inst, 'ready');
      });
    q(inst, '.chat-input').focus();
  }

  function startNewConversation(inst) {
    if (!inst.client || inst.client.closed) {
      addErrorLine(inst, 'The chat connection is closed. Reopen the chat to start a conversation.');
      return;
    }
    if (inst.busy) {
      addSystemLine(inst, 'Stop the current response before starting a new conversation.');
      return;
    }
    setStatus(inst, 'connecting');
    inst.client.newSession(inst.cwd).then(function (res) {
      inst.sessionId = res.sessionId;
      inst.titled = false;
      resetTranscript(inst);
      applySessionModels(inst, res);
      postSession(inst.branch, inst.type, res.sessionId)
        .then(function (saved) { refreshHistory(inst, saved); });
      setStatus(inst, 'ready');
      addSystemLine(inst, 'Started a new conversation.');
    }).catch(function (e) { addErrorLine(inst, e.message || String(e)); setStatus(inst, 'error'); });
  }

  function switchToSession(inst, sid) {
    if (sid === inst.sessionId) return;
    if (!inst.client || inst.client.closed) {
      addErrorLine(inst, 'The chat connection is closed. Reopen the chat to switch conversations.');
      return;
    }
    if (inst.busy) {
      addSystemLine(inst, 'Stop the current response before switching.');
      return;
    }

    var previousId = inst.sessionId;
    setStatus(inst, 'connecting');
    resetTranscript(inst);
    inst.client.loadSession(sid, inst.cwd).then(function (res) {
      finalizeTurn(inst);
      inst.sessionId = sid;
      applySessionModels(inst, res);
      addSystemLine(inst, 'Switched to a previous conversation.');
      setStatus(inst, 'ready');
      return postSession(inst.branch, inst.type, sid)
        .then(function (saved) { refreshHistory(inst, saved); });
    }).catch(function () {
      if (!previousId) return startFallbackSession(inst);
      resetTranscript(inst);
      return inst.client.loadSession(previousId, inst.cwd).then(function (res) {
        finalizeTurn(inst);
        inst.sessionId = previousId;
        applySessionModels(inst, res);
        addSystemLine(inst, 'Could not open that conversation. Restored the current conversation.');
        setStatus(inst, 'ready');
        return postSession(inst.branch, inst.type, previousId)
          .then(function (saved) { refreshHistory(inst, saved); });
      }).catch(function () {
        return startFallbackSession(inst);
      });
    }).catch(function (e) {
      addErrorLine(inst, e.message || String(e));
      setStatus(inst, 'error');
    });
  }

  function startFallbackSession(inst) {
    resetTranscript(inst);
    return inst.client.newSession(inst.cwd).then(function (res) {
      inst.sessionId = res.sessionId;
      inst.titled = false;
      applySessionModels(inst, res);
      addSystemLine(inst, 'Could not restore the conversation. Started a new conversation.');
      setStatus(inst, 'ready');
      return postSession(inst.branch, inst.type, res.sessionId)
        .then(function (saved) { refreshHistory(inst, saved); });
    });
  }

  function bootstrap(inst) {
    setStatus(inst, 'connecting');
    fetchInfo(inst.branch, inst.type).then(function (info) {
      if (!info || !info.ok) throw new Error((info && info.error) || 'Failed to load chat info');
      inst.cwd = info.cwd;
      var limits = info.attachment_limits || {};
      inst.maxFileBytes = limits.max_file_bytes || DEFAULT_MAX_FILE_BYTES;
      inst.maxFiles = limits.max_files || DEFAULT_MAX_FILES;
      inst.history = Array.isArray(info.history) ? info.history : [];
      inst.titled = false;
      for (var i = 0; i < inst.history.length; i++) {
        if (inst.history[i].session_id === info.session_id) {
          inst.titled = !!inst.history[i].title;
          break;
        }
      }
      renderHistoryMenu(inst);
      inst.client.on('update', function (p) { handleUpdate(inst, p); });
      inst.client.on('permission', function (p) { renderPermission(inst, p.requestId, p.params); });
      inst.client.on('error', function (msg) { addErrorLine(inst, msg); });
      inst.client.on('notice', function (msg) { if (msg) addSystemLine(inst, msg); });
      inst.client.on('close', function () { setStatus(inst, 'closed'); setBusy(inst, false); });
      return inst.client.connect().then(function () {
        return inst.client.initialize();
      }).then(function (initializeResult) {
        applyPromptCapabilities(inst, initializeResult);
        if (info.session_id) {
          return inst.client.loadSession(info.session_id, inst.cwd).then(function (res) {
            finalizeTurn(inst);
            inst.sessionId = info.session_id;
            applySessionModels(inst, res);
            renderHistoryMenu(inst);
            addSystemLine(inst, 'Resumed your previous conversation.');
          }).catch(function () {
            // Stale/invalid stored session — start fresh.
            resetTranscript(inst);
            return inst.client.newSession(inst.cwd).then(function (res) {
              inst.sessionId = res.sessionId;
              inst.titled = false;
              applySessionModels(inst, res);
              return postSession(inst.branch, inst.type, res.sessionId)
                .then(function (saved) { refreshHistory(inst, saved); });
            });
          });
        }
        return inst.client.newSession(inst.cwd).then(function (res) {
          inst.sessionId = res.sessionId;
          inst.titled = false;
          applySessionModels(inst, res);
          return postSession(inst.branch, inst.type, res.sessionId)
            .then(function (saved) { refreshHistory(inst, saved); });
        });
      });
    }).then(function () {
      setStatus(inst, 'ready');
      q(inst, '.chat-input').focus();
    }).catch(function (e) {
      addErrorLine(inst, e.message || String(e));
      setStatus(inst, 'error');
    });
  }

  // -- window manager ---------------------------------------------------------
  function open(branch, type) {
    type = (type || window.ODU_AGENT_DEFAULT || 'claude').toLowerCase();
    if (type !== 'claude' && type !== 'codex' && type !== 'opencode') type = 'claude';
    var key = keyOf(branch, type);
    if (instances[key]) { activate(instances[key]); return; }
    var inst = {
      key: key, domId: ++nextDomId, branch: branch, type: type, status: 'connecting', busy: false,
      client: new window.AcpClient(wsUrlFor(branch, type)),
      sessionId: null, cwd: null, turn: null, tools: Object.create(null), pill: null, unread: false,
      userChunkBubble: null, userChunkBuffer: '', followOutput: true,
      userChunkText: null, userChunkAttachments: null,
      userChunkAttachmentKeys: Object.create(null),
      history: [], titled: false, attachments: [], uploadActive: null,
      maxFileBytes: DEFAULT_MAX_FILE_BYTES, maxFiles: DEFAULT_MAX_FILES,
      promptCapabilities: {}
    };
    buildInstanceDom(inst);
    instances[key] = inst;
    activate(inst);
    bootstrap(inst);
  }

  function activate(inst) {
    if (activeKey && activeKey !== inst.key) minimize();
    var host = document.getElementById('chat-active');
    host.appendChild(inst.el);
    document.getElementById('chat-title').textContent = 'Chat · ' + labelOf(inst.type) + ' — ' + inst.branch;
    removePill(inst);
    inst.unread = false;
    activeKey = inst.key;
    showModal('chat-modal');
    var ta = q(inst, '.chat-input');
    if (ta) ta.focus();
  }

  function minimize() {
    if (!activeKey) { hideModal('chat-modal'); return; }
    var inst = instances[activeKey];
    document.getElementById('chat-parking').appendChild(inst.el);
    addPill(inst);
    hideModal('chat-modal');
    activeKey = null;
  }

  function restoreChat(key) {
    var inst = instances[key];
    if (inst) activate(inst);
  }

  function closeChat(key) {
    var inst = instances[key];
    if (!inst) return;
    clearPendingAttachments(inst, true);
    try { inst.client.close(); } catch (e) { /* ignore */ }
    removePill(inst);
    if (inst.el && inst.el.parentNode) inst.el.parentNode.removeChild(inst.el);
    delete instances[key];
    if (activeKey === key) { activeKey = null; hideModal('chat-modal'); }
  }

  function closeActive() { if (activeKey) closeChat(activeKey); }

  function overlayClick(event) {
    if (event && event.target !== event.currentTarget) return;
    minimize();
  }

  // Exposed API + globals for the static modal's inline handlers.
  window.ChatManager = { open: open, minimize: minimize, restore: restoreChat, close: closeChat, closeActive: closeActive };
  window.minimizeActiveChat = minimize;
  window.closeActiveChat = closeActive;
  window.chatOverlayClick = overlayClick;
})();
