// Chat module extracted from main.js - exposes a simple API on window.chatModule
(function (global) {
  const chatModule = {};

  // Object URLs behind the attachment previews in the transcript.
  //
  // They used to be revoked the moment the image finished loading. The
  // picture survives that — it is decoded by then — but the click did not:
  // window.open() got a blob: address the browser no longer resolved. They
  // now live as long as the message they belong to.
  const previewObjectUrls = new Set();

  function trackedObjectUrl(file) {
    const url = URL.createObjectURL(file);
    previewObjectUrls.add(url);
    return url;
  }

  function releasePreviewObjectUrls() {
    previewObjectUrls.forEach((url) => URL.revokeObjectURL(url));
    previewObjectUrls.clear();
  }

  /**
   * Open an attachment full size in a new tab.
   *
   * Neither address a preview carries survives window.open() directly: a
   * data: URL — what the restored history uses — is refused as a top level
   * navigation, and a blob: URL only resolves while it is alive. Both turn
   * into a fresh blob first, which the browser does allow.
   */
  async function openAttachmentInNewTab(url) {
    try {
      const response = await fetch(url);
      const objectUrl = URL.createObjectURL(await response.blob());
      previewObjectUrls.add(objectUrl);
      window.open(objectUrl, '_blank');
    } catch (e) {
      console.warn('[chat] could not open attachment, trying the raw address', e);
      window.open(url, '_blank');
    }
  }

  // Backend now handles all formatting via plugins
  // Frontend displays content as-is

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  function formatTextWithLineBreaks(text) {
    // Escape HTML first, then convert newlines to <br>
    const escaped = escapeHtml(text);
    return escaped.replace(/\n/g, '<br>');
  }

  function formatContent(content, format) {
    // If format is explicitly 'html', return as-is (already sanitized by backend)
    if (format === 'html') {
      return content;
    }
    // Otherwise, escape and convert line breaks
    return formatTextWithLineBreaks(content);
  }

  // Threshold (px) below the document end within which we still consider the
  // user "at the bottom" and therefore follow new content. Above this, the
  // user has scrolled up to read older content and we leave them alone.
  const NEAR_BOTTOM_THRESHOLD_PX = 150;

  function isNearBottom() {
    const scrolled = window.innerHeight + window.scrollY;
    return scrolled >= document.body.scrollHeight - NEAR_BOTTOM_THRESHOLD_PX;
  }

  // force=true: scroll regardless of current position (e.g. user just sent a
  // message, session just loaded - they expect to see the bottom).
  // force=false (default): only scroll if already near the bottom. This keeps
  // status events and streaming content from yanking the viewport away when
  // the user has scrolled up.
  function scrollBottom(force = false) {
    if (!force && !isNearBottom()) return;
    requestAnimationFrame(() => window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }));
  }

  /**
   * A local note: help output, an unknown command, a hint. Never sent to the
   * agent and never stored -- it belongs to the surface, not the conversation.
   */
  function addNote(chatContainer, text) {
    if (!chatContainer) return;
    const row = document.createElement('div');
    row.className = 'row';
    const msgDiv = document.createElement('div');
    msgDiv.className = 'msg note';
    const pre = document.createElement('pre');
    pre.className = 'note-text';
    pre.textContent = text;
    msgDiv.appendChild(pre);
    row.appendChild(msgDiv);
    chatContainer.appendChild(row);
    // force: a note answers something the user just typed. Honouring
    // "only scroll when already at the bottom" would hide the reply to their
    // own keystroke whenever they had scrolled up.
    scrollBottom(true);
  }

  /**
   * A note with rendered content, for /history: assistant answers reach the
   * browser as sanitized HTML from the backend's formatting hooks. Everything
   * a person or a tool wrote goes through escapeHtml on the way in.
   */
  function addRichNote(chatContainer, html) {
    if (!chatContainer) return;
    const row = document.createElement('div');
    row.className = 'row';
    const msgDiv = document.createElement('div');
    msgDiv.className = 'msg note';
    const body = document.createElement('div');
    body.innerHTML = html;
    msgDiv.appendChild(body);
    row.appendChild(msgDiv);
    chatContainer.appendChild(row);
    scrollBottom(true);
  }

  // ---------------------------------------------------------------------
  // What the commands read.
  //
  // Every one of them answers from an endpoint the rest of the UI already
  // uses -- no second source of truth, and nothing here decides WHICH
  // commands exist: that is the shared catalogue in chat_commands.py.
  // ---------------------------------------------------------------------

  function authHeaders() {
    // The browser also carries the access_token cookie; the header is what
    // makes a token kept in localStorage work the same way.
    const token = localStorage.getItem('token');
    return token ? { 'Authorization': 'Bearer ' + token } : {};
  }

  async function getJSON(url) {
    const resp = await fetch(url, { headers: authHeaders(), credentials: 'include' });
    if (!resp.ok) {
      let detail = resp.status + ' ' + resp.statusText;
      try {
        const body = await resp.json();
        if (body && body.detail) detail = body.detail;
      } catch (e) { /* not JSON -- keep the status line */ }
      const error = new Error(detail);
      error.status = resp.status;
      throw error;
    }
    return await resp.json();
  }

  async function postJSON(url, body) {
    const resp = await fetch(url, {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      credentials: 'include',
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      let detail = resp.status + ' ' + resp.statusText;
      try {
        const answer = await resp.json();
        if (answer && answer.detail) detail = answer.detail;
      } catch (e) { /* not JSON -- keep the status line */ }
      const error = new Error(detail);
      error.status = resp.status;
      throw error;
    }
    return await resp.json();
  }

  function currentAgentName() {
    return (window.selectorModule && typeof window.selectorModule.getCurrentAgent === 'function')
      ? window.selectorModule.getCurrentAgent()
      : null;
  }

  /**
   * A value as printable text, the way json.dumps does it for the terminal.
   *
   * The one accepted difference between the two renderers: JSON.stringify
   * writes {"a":1} where json.dumps writes {"a": 1, "b": [1, 2]}. Matching it
   * would need a serializer of our own, and the difference is whitespace
   * inside a truncated one-line preview -- measured across 30 cases, it is
   * the only place the two disagree.
   */
  function asText(value) {
    if (typeof value === 'string') return value;
    if (value === undefined || value === null) return '';
    try { return JSON.stringify(value); } catch (e) { return String(value); }
  }

  function oneLine(value, max) {
    const text = asText(value).replace(/\s+/g, ' ').trim();
    return text.length > max ? text.slice(0, max - 1) + '…' : text;
  }

  /** Tool arguments and results travel as a JSON string or already decoded. */
  function decodeMaybeJson(raw) {
    if (raw && typeof raw === 'object') return raw;
    if (typeof raw !== 'string') return raw;
    try { return JSON.parse(raw); } catch (e) { return raw; }
  }

  /**
   * Readable text of a message whose content may be multimodal.
   *
   * A part without text becomes "[<type>]", exactly as _message_text does in
   * the terminal. Returning '' for it instead made an image-only turn -- the
   * browser's own upload path sends one, with an empty text part in front --
   * look like no turn at all: /last then cut at the PREVIOUS turn and
   * /history dropped the question while keeping the answer.
   */
  function messageText(msg) {
    const content = msg && msg.content;
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) {
      return content.map(function (part) {
        if (typeof part === 'string') return part;
        return (part && part.text) || '[' + ((part && part.type) || 'part') + ']';
      }).filter(Boolean).join(' ');
    }
    return content === undefined || content === null ? '' : String(content);
  }

  /**
   * splitlines(), not split("\n"). Three differences, all of them visible in
   * the note: a tool result from a Windows shell carries CRLF and would keep
   * a stray \r per line; an empty result is no lines at all, not one empty
   * one; and a trailing newline does not add a blank line at the end.
   */
  function splitLines(text) {
    const value = String(text);
    if (!value) return [];
    return value.replace(/(\r\n|\r|\n)$/, '').split(/\r\n|\r|\n/);
  }

  /**
   * /tools filtering, where the terminal does it too: on the list already in
   * hand. Name or description, case-insensitive. Filtering on the server
   * would cost the count of what the agent HAS -- and with it the difference
   * between "no tool matches" and "this agent has no tools at all".
   */
  function filterToolGroups(groups, needle) {
    if (!needle) return groups;
    const wanted = needle.toLowerCase();
    return (groups || []).map(function (group) {
      return {
        server: group.server,
        tools: (group.tools || []).filter(function (tool) {
          return (tool.name || '').toLowerCase().indexOf(wanted) >= 0
            || (tool.description || '').toLowerCase().indexOf(wanted) >= 0;
        }),
      };
    }).filter(function (group) { return group.tools.length; });
  }

  /**
   * Whether a stored user message is really a slash command.
   *
   * Commands never reached the agent, so they are not part of the
   * conversation -- but unknown ones used to be passed through and sit in old
   * sessions. Mirrors chat_commands.looks_like_command, including its rule
   * that anything multiline is a message, never a command.
   *
   * Narrower than the Python side by one step: the catalogue is fetched for
   * the WEB surface, so the terminal-only spellings (/exit, /attach) are not
   * in it, and neither is anything before the catalogue has loaded. Both cases
   * only ever show a line that would have been hidden -- never the reverse.
   */
  function looksLikeCommand(text) {
    const stripped = String(text || '').trim();
    if (!stripped || stripped.indexOf('\n') >= 0) return false;
    const first = stripped.split(' ')[0].toLowerCase();
    const commands = ((window.slashCommands || {}).catalogue || {}).commands || [];
    return commands.some(function (command) {
      return (command.aliases || []).indexOf(first) >= 0;
    });
  }

  /** A user message that actually went to the agent. */
  function isRealTurn(msg) {
    if (!msg || msg.role !== 'user') return false;
    const text = messageText(msg).trim();
    return !!text && !looksLikeCommand(text);
  }

  async function sessionMessages() {
    if (!currentSessionId) return null;
    const session = await getJSON('/api/sessions/' + encodeURIComponent(currentSessionId));
    return session.messages || [];
  }

  /** One tool request: compact for /history, key-per-line for /last. */
  function toolCallLines(call, full) {
    const fn = (call && call.function) || {};
    const name = fn.name || (call && call.name) || '?';
    // `||`, not a presence check: an empty argument string falls through to
    // the flat shape in the terminal's reader, and this has to agree with it.
    const raw = fn.arguments || (call && call.arguments);
    const data = decodeMaybeJson(raw);
    // Arrays are objects in JS but not dicts in the terminal's renderer:
    // without this an array-shaped result prints as 0:, 1:, 2: instead of
    // the lines it is.
    const isObject = data && typeof data === 'object' && !Array.isArray(data);

    if (!full) {
      const inner = isObject
        ? Object.keys(data).map(function (key) {
            return key + '=' + oneLine(data[key], 40);
          }).join(', ')
        : oneLine(raw, 80);
      return ['  → ' + name + '(' + oneLine(inner, 100) + ')'];
    }

    const out = ['→ ' + name];
    if (!isObject) {
      splitLines(asText(raw)).forEach(function (line) { out.push('    ' + line); });
      return out;
    }
    Object.keys(data).forEach(function (key) {
      // Escaped newlines are what made this a wall of text -- render the
      // value as the lines it actually is.
      const lines = splitLines(asText(data[key]));
      if (lines.length <= 1) {
        out.push('    ' + key + ': ' + lines[0]);
      } else {
        out.push('    ' + key + ':');
        lines.forEach(function (line) { out.push('      ' + line); });
      }
    });
    return out;
  }

  /** One tool result, mirroring toolCallLines' two modes. */
  function toolResultLines(msg, full) {
    const raw = messageText(msg);
    const data = decodeMaybeJson(raw);
    // Arrays are objects in JS but not dicts in the terminal's renderer:
    // without this an array-shaped result prints as 0:, 1:, 2: instead of
    // the lines it is.
    const isObject = data && typeof data === 'object' && !Array.isArray(data);

    if (!full) {
      if (isObject) {
        const body = data.content || data.stdout || data.changes || '';
        // rstrip, not trim: the terminal keeps the gap a missing status
        // leaves, and this line is compared against it character for
        // character.
        return ['  ← ' + ((data.status || '') + ' ' + oneLine(body, 70))
          .replace(/\s+$/, '')];
      }
      return ['  ← ' + oneLine(raw, 90)];
    }

    const out = [];
    if (isObject) {
      Object.keys(data).forEach(function (key) {
        const lines = splitLines(asText(data[key]));
        if (lines.length <= 1) {
          out.push('    ' + key + ': ' + lines[0]);
        } else {
          out.push('    ' + key + ':');
          lines.forEach(function (line) { out.push('      ' + line); });
        }
      });
    } else {
      splitLines(raw).forEach(function (line) { out.push('    ' + line); });
    }
    return out;
  }

  // ---------------------------------------------------------------------
  // The commands themselves
  // ---------------------------------------------------------------------

  async function cmdSessions(container, payload) {
    // The count is advertised by the shared command catalogue, which both
    // surfaces render -- so it has to mean the same here as in agent-cli:
    // a number, 0 for all, anything else the usage line (as /history does).
    const raw = (payload || '').trim();
    if (raw && !/^\+?\d+$/.test(raw)) {
      addNote(container, 'Usage: /sessions [count]   (got: ' + raw + ')');
      return;
    }
    const limit = raw ? parseInt(raw, 10) : 20;
    const sessions = await getJSON('/api/sessions');
    if (!sessions.length) {
      addNote(container, 'No sessions yet.');
      return;
    }
    const shown = limit <= 0 ? sessions : sessions.slice(0, limit);
    const lines = shown.map(function (s) {
      const marker = s.session_id === currentSessionId ? '*' : ' ';
      const count = String(s.message_count || 0).padStart(4);
      return ' ' + marker + ' ' + s.session_id + '  ' + count + ' msg  ' +
        (s.agent_name || '?') + '  ' + oneLine(s.title || 'Untitled', 48);
    });
    const rest = sessions.length - shown.length;
    addNote(container, 'Sessions (' + shown.length + ' of ' + sessions.length + '):\n' +
      lines.join('\n') +
      (rest > 0 ? '\n   ... ' + rest + ' more -- /sessions <count>, /sessions 0 for all' : '') +
      '\nUse /resume <id> to continue one.');
  }

  async function cmdResume(container, payload) {
    const id = (payload || '').trim();
    if (!id) {
      addNote(container, 'Usage: /resume <session-id>   (/sessions lists them)');
      return;
    }
    if (!window.sessionManager || typeof window.sessionManager.loadSession !== 'function') {
      addNote(container, 'Session switching is not available in this window.');
      return;
    }
    // Ask first: loadSession answers an unknown id with a browser alert, which
    // is the wrong voice for something the person typed into the chat.
    await getJSON('/api/sessions/' + encodeURIComponent(id));
    await window.sessionManager.loadSession(id);
    // It returns without throwing when the person cancels the "a run is still
    // active" dialog, and when its own fetch fails -- so the switch has to be
    // confirmed, not assumed. Claiming it while the old conversation is still
    // on screen is worse than saying nothing.
    //
    // Checked against THIS module's currentSessionId, not the session
    // manager's: that is the one the other five commands read, and the one
    // the next message continues. It is set by the session:loaded handler,
    // which loadSession dispatches synchronously before it returns.
    if (currentSessionId === id) {
      addNote(container, 'Resumed session: ' + id);
    } else {
      addNote(container, 'Session ' + id + ' was not loaded -- the switch was cancelled or failed.');
    }
  }

  /**
   * List, set, unset or clear the template variables of this session.
   *
   * The line is NOT parsed here: it goes to the server as typed, so the
   * grammar stays the one the terminal uses. A read is a GET and a change is
   * a POST -- the same split the rest of the API keeps.
   */
  async function cmdVars(container, payload) {
    const rest = (payload || '').trim();
    if (!currentSessionId) {
      addNote(container, 'No session yet -- variables live on one. ' +
        (rest ? 'Send a message first, then set them.'
              : 'It is created with your first message.'));
      return;
    }
    const agent = currentAgentName();
    const data = rest
      ? await postJSON('/chat/vars', {
          session_id: currentSessionId, agent_name: agent || null, payload: rest })
      : await getJSON('/chat/vars?session_id=' + encodeURIComponent(currentSessionId) +
          (agent ? '&agent_name=' + encodeURIComponent(agent) : ''));

    if (data.errors && data.errors.length) {
      addNote(container, data.errors.map(function (e) { return '  ' + e; }).join('\n') +
        '\nNothing changed. Usage: /vars [KEY=VALUE ...] | /vars unset KEY | /vars clear');
      return;
    }
    const vars = data.vars || {};
    const names = Object.keys(vars).sort();
    const width = names.reduce(function (w, n) { return Math.max(w, n.length); }, 0);
    const lines = names.length
      ? ['Session variables (' + data.session_id + '):'].concat(
          names.map(function (n) {
            // 200, not the 60 /tools uses for descriptions: the point here is
            // to see the value. The cap only guards against a plugin parking a
            // large blob in a session variable -- and no String() around the
            // value, because oneLine JSON-stringifies non-strings exactly like
            // the terminal's _one_line. Pre-stringifying turned that blob into
            // "[object Object]", 15 characters the cap could never trim.
            return '  ' + n.padEnd(width) + '  ' + oneLine(vars[n], 200);
          }))
      : ['No session variables set.'];
    if (data.changed) lines.push('  (takes effect on the next step the agent makes)');
    addNote(container, lines.join('\n'));
  }

  async function cmdTools(container, payload) {
    const agent = currentAgentName();
    if (!agent) {
      addNote(container, 'No agent selected yet.');
      return;
    }
    const query = (payload || '').trim();
    const data = await getJSON('/agents/' + encodeURIComponent(agent) + '/tools');
    if (!data.total) {
      addNote(container, 'This agent has no tools (tools.allowed is empty = deny-all).');
      return;
    }
    const groups = filterToolGroups(data.groups, query);
    const shown = groups.reduce(function (sum, group) { return sum + group.tools.length; }, 0);
    if (!shown) {
      addNote(container, "No tool matches '" + query + "'.");
      return;
    }
    const lines = [shown + ' tool(s) available to ' + data.agent +
      (query ? " matching '" + query + "'" : '') + ':'];
    groups.forEach(function (group) {
      lines.push(group.server);
      (group.tools || []).forEach(function (tool) {
        const summary = oneLine(tool.description || '', 70);
        lines.push('  ' + tool.name + (summary ? '  -- ' + summary : ''));
      });
    });
    addNote(container, lines.join('\n'));
  }

  async function cmdCosts(container) {
    if (!currentSessionId) {
      addNote(container, 'No session yet -- nothing has been billed.');
      return;
    }
    let data;
    try {
      data = await getJSON('/plugins/context_usage_tracker/usage?session_id=' +
        encodeURIComponent(currentSessionId));
    } catch (e) {
      if (e.status === 404) {
        addNote(container, 'The context_usage_tracker plugin is not active -- ' +
          'there are no per-call records to add up.');
        return;
      }
      throw e;
    }
    const stats = data.statistics || {};
    const totals = stats.totals;
    if (!totals) {
      addNote(container, 'No LLM calls recorded for this session yet.');
      return;
    }
    const short = function (n) {
      const value = n || 0;
      // trunc, not round: int() in the terminal's formatter cuts as well.
      return value >= 1000 ? (value / 1000).toFixed(1) + 'k' : String(Math.trunc(value));
    };
    const samples = (stats.timespan || {}).sample_count || 0;
    const estimated = totals.cost_estimated_calls || 0;
    // Calls the tracker saw but could price neither way -- naming them keeps
    // the total from looking complete when it is not.
    const unpriced = Math.max(0, samples - (totals.cost_known_calls || 0));
    addNote(container,
      'Session ' + currentSessionId + ' (incl. sub-agents):\n' +
      '  calls        ' + samples + (estimated ? '  (' + estimated + ' estimated)' : '') + '\n' +
      '  tokens       ↑' + short(totals.prompt_tokens) +
      '  ↓' + short(totals.completion_tokens) +
      '  cache ' + Math.round(totals.cache_hit_rate || 0) + '%\n' +
      '  cost         ' + (estimated ? '~$' : '$') + (totals.cost || 0).toFixed(4) +
      (unpriced ? '  (' + unpriced + ' unpriced)' : '') +
      // Without this line the ~ in front of the amount is an unexplained
      // squiggle; the terminal spells it out for the same reason.
      (estimated ? '\n  ~ = estimated from config/llm_pricing.yaml, not provider billing' : ''));
  }

  async function cmdHistory(container, payload) {
    const raw = (payload || '').trim();
    // What int() accepts and nothing else: parseInt("3abc") is 3 and
    // Number("0x1f") is 31, while the terminal answers both with the usage
    // line. A count below 1 is clamped there, not refused.
    if (raw && !/^[+-]?\d+$/.test(raw)) {
      addNote(container, 'Usage: /history [count]   (got: ' + raw + ')');
      return;
    }
    const limit = raw ? Math.max(parseInt(raw, 10), 1) : 6;
    const messages = await sessionMessages();
    if (messages === null) {
      addNote(container, 'No session yet.');
      return;
    }
    if (!messages.length) {
      addNote(container, 'No messages in this session yet.');
      return;
    }

    // Count backwards in USER turns, so "6" means six exchanges rather than
    // six raw messages (a single turn can hold a dozen tool messages).
    let start = 0;
    let seen = 0;
    for (let i = messages.length - 1; i >= 0; i--) {
      if (isRealTurn(messages[i])) {
        seen++;
        if (seen >= limit) { start = i; break; }
      }
    }
    if (!seen) {
      addNote(container, 'No agent exchanges in this session yet.');
      return;
    }

    const plain = function (text) {
      return '<pre class="note-text">' + escapeHtml(text) + '</pre>';
    };
    const parts = [plain('Last ' + seen + ' exchange(s) of session ' + currentSessionId + ':')];
    messages.slice(start).forEach(function (msg) {
      const text = messageText(msg).trim();
      if (msg.role === 'user') {
        if (!text || looksLikeCommand(text)) return;
        parts.push(plain('\n› ' + text));
      } else if (msg.role === 'assistant') {
        if (text) {
          // response-text, not note-text: the answer is rendered markdown, and
          // the note's monospace pre-wrap would set it as if it were a log.
          parts.push('<div class="response-text">' +
            formatContent(msg.content, msg.content_format) + '</div>');
        }
        (msg.tool_calls || []).forEach(function (call) {
          parts.push(plain(toolCallLines(call, false).join('\n')));
        });
      } else if (msg.role === 'tool') {
        parts.push(plain(toolResultLines(msg, false).join('\n')));
      }
    });
    parts.push(plain("(/last shows the last turn's tool traffic in full)"));
    addRichNote(container, parts.join(''));
  }

  async function cmdLast(container) {
    const messages = await sessionMessages();
    if (messages === null) {
      addNote(container, 'No session yet.');
      return;
    }
    let lastUser = -1;
    for (let i = messages.length - 1; i >= 0; i--) {
      if (isRealTurn(messages[i])) { lastUser = i; break; }
    }
    if (lastUser < 0) {
      addNote(container, 'No turn to show yet.');
      return;
    }
    const lines = [];
    messages.slice(lastUser + 1).forEach(function (msg) {
      if (msg.role === 'assistant') {
        (msg.tool_calls || []).forEach(function (call) {
          lines.push.apply(lines, toolCallLines(call, true));
        });
      } else if (msg.role === 'tool') {
        lines.push.apply(lines, toolResultLines(msg, true));
      }
    });
    addNote(container, lines.length ? lines.join('\n') : 'The last turn used no tools.');
  }

  /**
   * Run a plugin command -- on the server, where the agent lives.
   *
   * The browser names the COMMAND, never a tool: the server looks it up in
   * the list this agent may dispatch and runs it through the same path the
   * terminal uses. No LLM turn, no tokens; the result is a note, like every
   * other command's output.
   */
  async function runPluginCommand(name, payload) {
    const container = chatContainer;
    try {
      const answer = await postJSON('/chat/command', {
        name: name,
        payload: payload || '',
        agent_name: currentAgentName(),
        session_id: currentSessionId,
      });
      addNote(container, answer.text || 'done');
    } catch (e) {
      addNote(container, '/' + name + ' failed: ' + ((e && e.message) || e));
    }
  }

  /**
   * Run a built-in command in the browser.
   *
   * Only the surface-specific part lives here; which commands exist comes from
   * the shared catalogue. A command this surface does not offer at all (there
   * is no terminal to leave, so no /exit) never reaches this point -- what
   * does reach it and has no handler says so out loud rather than doing
   * nothing, because silence would read as a broken command.
   */
  async function runChatCommand(name, payload) {
    const container = chatContainer;
    if (name === 'help') {
      addNote(container, window.slashCommands.helpLines().join('\n'));
      return;
    }
    if (name === 'skills') {
      const skills = (window.slashCommands.catalogue || {}).skills || [];
      addNote(container, skills.length
        ? 'Skills you can run:\n' + skills.map(function (s) {
            return '  /' + s.name + (s.summary ? '   ' + s.summary : '');
          }).join('\n')
        : 'No skills found.');
      return;
    }
    if (name === 'new') {
      currentSessionId = null;
      try { global.currentSessionId = null; } catch (e) { /* ignore */ }
      sessionStorage.removeItem('lastSessionId');
      updateHeaderSessionId();
      addNote(container, 'New session: the next message starts a fresh one.');
      return;
    }
    if (name === 'session') {
      addNote(container, currentSessionId
        ? 'Session: ' + currentSessionId
        : 'No session yet -- it is created with the first message.');
      return;
    }

    const handlers = {
      sessions: function () { return cmdSessions(container, payload); },
      resume: function () { return cmdResume(container, payload); },
      vars: function () { return cmdVars(container, payload); },
      tools: function () { return cmdTools(container, payload); },
      costs: function () { return cmdCosts(container); },
      history: function () { return cmdHistory(container, payload); },
      last: function () { return cmdLast(container); },
    };
    const handler = handlers[name];
    if (!handler) {
      addNote(container, '/' + name + ' is only available in the terminal chat (agent-cli).');
      return;
    }
    try {
      await handler();
    } catch (e) {
      addNote(container, '/' + name + ' failed: ' + ((e && e.message) || e));
    }
  }

  /**
   * Empty the input the way the send path does.
   *
   * The input event matters: the textarea shrinks back and the slash
   * suggestion list closes on it. Clearing `.value` alone left the dropdown
   * open, so the next Enter completed a stale entry instead of typing.
   * updateActionButton() then restores Stop, which an empty input during a
   * running turn is supposed to show.
   */
  function clearInput(taskInput) {
    if (!taskInput) return;
    taskInput.value = '';
    try {
      taskInput.dispatchEvent(new Event('input', { bubbles: true, cancelable: false }));
    } catch (e) {
      taskInput.dispatchEvent(document.createEvent('Event'));
    }
    updateActionButton();
  }

  function addUser(chatContainer, text, images = [], audioFiles = [], textFiles = []) {
    // Ensure text is always a string
    const displayText = typeof text === 'string' ? text : String(text);
    const row = document.createElement('div');
    row.className = 'row';
    
    const msgDiv = document.createElement('div');
    msgDiv.className = 'msg user';
    
    // Add text content
    const textSpan = document.createElement('div');
    textSpan.innerHTML = formatTextWithLineBreaks(displayText);
    msgDiv.appendChild(textSpan);
    
    // Add image previews if any
    if (images && images.length > 0) {
      const previewContainer = document.createElement('div');
      previewContainer.className = 'user-image-previews';
      
      images.forEach(file => {
        const img = document.createElement('img');
        img.src = trackedObjectUrl(file);
        img.alt = file.name;
        img.title = file.name;

        // Click to view full size
        img.onclick = () => openAttachmentInNewTab(img.src);
        
        previewContainer.appendChild(img);
      });
      
      msgDiv.appendChild(previewContainer);
    }
    
    // Add audio previews if any
    if (audioFiles && audioFiles.length > 0) {
      const audioContainer = document.createElement('div');
      audioContainer.className = 'user-audio-previews';
      audioContainer.style.cssText = 'margin-top: 10px; display: flex; flex-direction: column; gap: 8px;';
      
      audioFiles.forEach((file, index) => {
        const audioWrapper = document.createElement('div');
        audioWrapper.style.cssText = 'display: flex; align-items: center; gap: 8px;';
        
        // Create object URL for the audio file
        const audioUrl = trackedObjectUrl(file);
        
        // Create play button
        const playBtn = document.createElement('button');
        playBtn.textContent = '▶️ ' + file.name;
        playBtn.style.cssText = 'background: #444; color: #ddd; border: 1px solid #666; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 0.9em;';
        playBtn.title = 'Click to play';
        
        // Create hidden audio element
        const audioEl = document.createElement('audio');
        audioEl.src = audioUrl;
        audioEl.style.display = 'none';
        
        let isPlaying = false;
        playBtn.onclick = () => {
          if (isPlaying) {
            audioEl.pause();
            playBtn.textContent = '▶️ ' + file.name;
            isPlaying = false;
          } else {
            audioEl.play();
            playBtn.textContent = '⏸️ ' + file.name;
            isPlaying = true;
          }
        };
        
        audioEl.onended = () => {
          playBtn.textContent = '▶️ ' + file.name;
          isPlaying = false;
        };
        
        audioWrapper.appendChild(playBtn);
        audioWrapper.appendChild(audioEl);
        audioContainer.appendChild(audioWrapper);
      });
      
      msgDiv.appendChild(audioContainer);
    }
    
    // Add text file previews if any
    if (textFiles && textFiles.length > 0) {
      const textContainer = document.createElement('div');
      textContainer.className = 'user-text-file-previews';
      textContainer.style.cssText = 'margin-top: 10px; display: flex; flex-direction: column; gap: 8px;';
      
      textFiles.forEach((file, index) => {
        const textWrapper = document.createElement('div');
        textWrapper.style.cssText = 'display: flex; flex-direction: column; gap: 4px;';
        
        // Create view button
        const viewBtn = document.createElement('button');
        viewBtn.textContent = '📄 ' + file.name;
        viewBtn.style.cssText = 'background: #444; color: #ddd; border: 1px solid #666; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 0.9em; text-align: left;';
        viewBtn.title = 'Click to view';
        
        // Create hidden content div
        const contentDiv = document.createElement('pre');
        contentDiv.style.cssText = 'display: none; margin: 0; padding: 10px; background: #2a2a2a; border: 1px solid #444; border-radius: 4px; max-height: 300px; overflow: auto; font-size: 0.85em; white-space: pre-wrap; word-wrap: break-word;';
        
        viewBtn.onclick = () => {
          // Toggle content display
          if (contentDiv.style.display === 'none') {
            contentDiv.style.display = 'block';
            viewBtn.textContent = '📄 ' + file.name + ' ▼';
          } else {
            contentDiv.style.display = 'none';
            viewBtn.textContent = '📄 ' + file.name;
          }
        };
        
        // Read file content
        const reader = new FileReader();
        reader.onload = (e) => {
          contentDiv.textContent = e.target.result;
        };
        reader.readAsText(file);
        
        textWrapper.appendChild(viewBtn);
        textWrapper.appendChild(contentDiv);
        textContainer.appendChild(textWrapper);
      });
      
      msgDiv.appendChild(textContainer);
    }
    
    row.appendChild(msgDiv);
    chatContainer.appendChild(row);
    // User just sent a message - always scroll so they see what they sent.
    scrollBottom(true);
  }

  function addAssistantBlock(chatContainer) {
    const row = document.createElement('div');
    row.className = 'row';
    const box = document.createElement('div');
    box.className = 'msg assistant';
    box.style.position = 'relative'; // Enable absolute positioning for request ID
    box.innerHTML = `
      <div class="container-section">
        <div class="container-header" data-toggle="thinking">
          <span class="toggle-arrow">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M9 18l6-6-6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </span>
          <span class="type-icon">🤔</span>
          <span class="container-label">Thinking...</span>
        </div>
        <div class="container-body" id="thinking" style="display: none;">
          <pre id="thinkingContent"></pre>
        </div>
      </div>
      <div class="container-section" style="display: none;">
        <div class="container-header" data-toggle="status">
          <span class="toggle-arrow">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M9 18l6-6-6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </span>
          <span class="type-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">     
              <rect x="3" y="4" width="18" height="16" rx="2" fill="#0f172a" stroke="#58a6ff" stroke-width="0.8" />
              <path d="M7 9l2 2 4-4" stroke="#56d364" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </span>
          <span class="container-label">Status</span>
        </div>
        <div class="container-body" id="statusBody" style="display: block;"></div>
      </div>
      <div class="container-section" style="display: none;">
        <div class="container-header" data-toggle="response">
          <span class="toggle-arrow">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M9 18l6-6-6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </span>
          <span class="type-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"> 
              <rect x="2" y="3" width="20" height="14" rx="3" fill="#0f172a" stroke="#58a6ff" stroke-width="0.8" />
              <circle cx="8.5" cy="9" r="1.1" fill="#cbd5e1" />
              <circle cx="15.5" cy="9" r="1.1" fill="#cbd5e1" />
              <path d="M7 13c1 0 2 0.8 3 0.8s2-0.8 3-0.8" stroke="#9fb8d9" stroke-width="0.9" stroke-linecap="round" stroke-linejoin="round" />
              <rect x="6" y="15.5" width="6" height="3" rx="0.8" fill="#071028" />
            </svg>
          </span>
          <span class="container-label">Response</span>
        </div>
        <div class="container-body" id="assistantText" style="display: block;"></div>
      </div>
    `;
    row.appendChild(box);
    chatContainer.appendChild(row);
    scrollBottom();

    const headers = box.querySelectorAll('.container-header');
    headers.forEach(header => {
      const body = header.nextElementSibling;
      const arrow = header.querySelector('.toggle-arrow svg');
      if (body && arrow) {
        // Right (0deg) when collapsed, down (90deg) when expanded
        arrow.style.transform = (body.style.display === 'none') ? 'rotate(0deg)' : 'rotate(90deg)';
      }

      header.addEventListener('click', () => {
        const body = header.nextElementSibling;
        if (body && body.classList.contains('container-body')) {
          const isHidden = body.style.display === 'none';
          body.style.display = isHidden ? 'block' : 'none';
          const arrow = header.querySelector('.toggle-arrow svg');
          if (arrow) {
            // rotate to down when expanded
            arrow.style.transform = isHidden ? 'rotate(90deg)' : 'rotate(0deg)';
          }
        }
      });
    });

    return {
      row,
      box,
      t: box.querySelector('#assistantText'),
      think: box.querySelector('#thinkingContent'),
      status: box.querySelector('#statusBody'),
      thinkingSection: box.querySelector('[data-toggle="thinking"]').parentElement,
      statusSection: box.querySelector('[data-toggle="status"]').parentElement,
      responseSection: box.querySelector('[data-toggle="response"]').parentElement
    };
  }

  const activeOperations = new Map();
  const treeNodes = new Map(); // requestId -> { element, parentId, depth, children:Set }
  const pendingChildren = new Map(); // parentId -> [{elementInfo}]

  function toggleTreeNode(requestId) {
    const node = treeNodes.get(requestId);
    if (!node) return;
    const el = node.element;
    if (!el) return;
    const expanded = el.getAttribute('data-expanded') === 'true';
    const newState = !expanded;
    el.setAttribute('data-expanded', newState ? 'true' : 'false');
  const icon = el.querySelector('.tree-expand-btn .expand-icon');
  if (icon) icon.style.transform = newState ? 'rotate(90deg)' : 'rotate(0deg)';
    setDescendantsVisibility(requestId, newState);
  }

  function setDescendantsVisibility(rootId, rootVisible) {
    const queue = [...(treeNodes.get(rootId)?.children || [])];
    while (queue.length) {
      const cid = queue.shift();
      const cn = treeNodes.get(cid);
      if (!cn) continue;
      
      // If we're collapsing (rootVisible = false), hide all descendants
      // If we're expanding (rootVisible = true), only show if all ancestors are expanded
      const shouldBeVisible = rootVisible ? isAllAncestorsExpanded(cid) : false;
      cn.element.style.display = shouldBeVisible ? 'block' : 'none';
      
      // Always traverse deeper to hide/show all descendants
      queue.push(...cn.children);
    }
  }

  function isAllAncestorsExpanded(requestId) {
    let current = treeNodes.get(requestId);
    while (current && current.parentId) {
      const parent = treeNodes.get(current.parentId);
      // If parent doesn't exist yet, assume it's expanded (will be created later)
      if (!parent) return true;
      if (parent.element.getAttribute('data-expanded') !== 'true') return false;
      current = parent;
    }
    return true;
  }
  
  function updateParentExpandButton(requestId) {
    const node = treeNodes.get(requestId);
    if (!node) return;
    const btn = node.element.querySelector('.tree-expand-btn');
    if (!btn) return;
    if (node.children.size > 0) {
      btn.style.display = 'inline-block';
      const icon = btn.querySelector('.expand-icon');
      if (icon) icon.style.transform = node.element.getAttribute('data-expanded') === 'true' ? 'rotate(90deg)' : 'rotate(0deg)';
    } else {
      btn.style.display = 'none';
    }
  }

  function registerNode(requestId, parentId, element, depthLevel) {
    treeNodes.set(requestId, { element, parentId, depthLevel, children: new Set() });
    if (parentId) {
      const parentNode = treeNodes.get(parentId);
      if (parentNode) {
        parentNode.children.add(requestId);
        updateParentExpandButton(parentId);
        // Check full ancestor chain to determine visibility
        const shouldBeVisible = isAllAncestorsExpanded(requestId);
        element.style.display = shouldBeVisible ? 'block' : 'none';
      } else {
        // Queue child until parent arrives
        element.style.display = 'none';
        if (!pendingChildren.has(parentId)) pendingChildren.set(parentId, []);
        pendingChildren.get(parentId).push({ requestId, element, depthLevel });
      }
    }
    attachPendingChildren(requestId);
  }

  function attachPendingChildren(parentId) {
    const waiting = pendingChildren.get(parentId);
    if (!waiting) return;
    const parentNode = treeNodes.get(parentId);
    if (!parentNode) return;
    for (const child of waiting) {
      parentNode.children.add(child.requestId);
      updateParentExpandButton(parentId);
      // Check full ancestor chain to determine visibility
      const shouldBeVisible = isAllAncestorsExpanded(child.requestId);
      child.element.style.display = shouldBeVisible ? 'block' : 'none';
    }
    pendingChildren.delete(parentId);
  }

  function createTreeOperationDiv(operationKey, ev, depthLevel, parentId) {
    const operationDiv = document.createElement('div');
    operationDiv.className = 'operation-progress';
    operationDiv.setAttribute('data-operation', operationKey);
    operationDiv.setAttribute('data-request-id', ev.request_id || '');
    operationDiv.setAttribute('data-depth', depthLevel);
    operationDiv.setAttribute('data-expanded', 'true'); // Default to expanded
    
    const reqSpan = ev.request_id ? `<span class="operation-request-id">${escapeHtml(ev.request_id)}</span>` : '';
    
    // Tree connector/expand button - unified approach with same SVG arrow as section headers
    const arrowSvg = `<svg class="expand-icon" width="10" height="10" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M9 18l6-6-6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
    let treeIndicator = '';
    if (depthLevel > 0) {
      treeIndicator = `
        <span class="tree-indicator">
          <span class="tree-expand-btn" data-request-id="${ev.request_id || ''}" style="display: none;">
            ${arrowSvg}
          </span>
          <span class="tree-connector">└─</span>
        </span>`;
    } else {
      treeIndicator = `
        <span class="tree-indicator">
          <span class="tree-expand-btn" data-request-id="${ev.request_id || ''}" style="display: none;">
            ${arrowSvg}
          </span>
        </span>`;
    }
    
    operationDiv.innerHTML = `
      <div class="progress-line" style="padding-left: ${depthLevel * 16}px;">
        ${treeIndicator}
        <span class="progress-icon"><div class="spinner"></div></span>
        <span class="progress-time">${formatTime(ev.timestamp)}</span>
        ${reqSpan}
        <span class="progress-server">${escapeHtml(ev.server || 'Unknown')}</span>
        <span class="progress-message">${escapeHtml(ev.message || (ev.phase === 'start' ? 'Starting...' : 'In progress...'))}</span>
      </div>
    `;
    
    // Add click handler for expand/collapse
    const expandBtn = operationDiv.querySelector('.tree-expand-btn');
    if (expandBtn) {
      expandBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleTreeNode(ev.request_id || '');
      });
    }
    
    return operationDiv;
  }

  function insertOperationHierarchically(container, operationDiv, requestId, parentId, depthLevel) {
    if (!parentId) {
      // Root level - insert in sorted order by request ID
      if (!requestId) {
        container.appendChild(operationDiv);
        return;
      }
      
      // Find all root-level operations (those with same depth)
      const rootElements = [];
      for (const [nodeId, nodeInfo] of treeNodes.entries()) {
        if (nodeInfo.depth === depthLevel && !nodeInfo.parent && container.contains(nodeInfo.element)) {
          rootElements.push({ id: nodeId, element: nodeInfo.element });
        }
      }
      
      // Sort by request ID
      rootElements.sort((a, b) => {
        const aSeq = a.id.split('_').pop();
        const bSeq = b.id.split('_').pop();
        return aSeq.localeCompare(bSeq);
      });
      
      // Find insertion position
      const newSeq = requestId.split('_').pop();
      let insertBefore = null;
      
      for (const root of rootElements) {
        const rootSeq = root.id.split('_').pop();
        if (rootSeq.localeCompare(newSeq) > 0) {
          insertBefore = root.element;
          break;
        }
      }
      
      // Insert at correct position
      if (insertBefore) {
        container.insertBefore(operationDiv, insertBefore);
      } else {
        container.appendChild(operationDiv);
      }
      return;
    }
    
    // Find parent element
    const parentNode = treeNodes.get(parentId);
    if (parentNode && parentNode.element) {
      // Collect all children with their request IDs for sorting
      const childElements = [];
      for (const childId of parentNode.children) {
        const childNode = treeNodes.get(childId);
        if (childNode && childNode.element && container.contains(childNode.element)) {
          childElements.push({ id: childId, element: childNode.element });
        }
      }
      
      // Sort children by request ID (preserves chronological order from backend)
      childElements.sort((a, b) => {
        // Extract sequence numbers from request IDs for comparison
        // e.g., "rbwytmgxyt_006_015" -> compare last segment (015)
        const aSeq = a.id.split('_').pop();
        const bSeq = b.id.split('_').pop();
        return aSeq.localeCompare(bSeq);
      });
      
      // Find correct insertion position among sorted siblings
      let insertAfter = parentNode.element;
      const newSeq = requestId.split('_').pop();
      
      for (const child of childElements) {
        const childSeq = child.id.split('_').pop();
        if (childSeq.localeCompare(newSeq) < 0) {
          insertAfter = child.element;
        } else {
          break; // Found first sibling that should come after new element
        }
      }
      
      // Insert after the determined position
      if (insertAfter.nextSibling) {
        container.insertBefore(operationDiv, insertAfter.nextSibling);
      } else {
        container.appendChild(operationDiv);
      }
    } else {
      // Parent not found, append at end
      container.appendChild(operationDiv);
    }
  }

  function addStatusEvent(container, ev) {
    if (!container || !ev) return;
    
    const statusSection = container.closest('.container-section');
    if (statusSection && statusSection.style.display === 'none') {
      statusSection.style.display = 'block';
    }
    
    // Use request_id for hierarchical operations if available
    const requestId = ev.request_id && ev.request_id !== 'default' ? ev.request_id : null;
    const operationKey = requestId || ev.server;
    
    // Get tree hierarchy metadata from backend (already calculated correctly)
    const treeInfo = ev.tree || { parent_id: null, depth_level: 0, child_count: 0, is_leaf: true };
    const depthLevel = treeInfo.depth_level || 0;
    const parentId = treeInfo.parent_id || null;
    
    // Auto-create virtual parent if needed (parent_id given but not yet in tree)
    if (parentId && !treeNodes.has(parentId)) {
      const virtualParent = document.createElement('div');
      virtualParent.className = 'operation-progress virtual-parent';
      virtualParent.setAttribute('data-operation', parentId);
      virtualParent.setAttribute('data-request-id', parentId);
      virtualParent.setAttribute('data-depth', depthLevel - 1);
      virtualParent.setAttribute('data-expanded', 'true');
      virtualParent.style.display = 'block'; // Visible so children can be displayed
      
      container.appendChild(virtualParent);
      // Register without a grandparent - will be filled in when parent's parent arrives
      registerNode(parentId, null, virtualParent, depthLevel - 1);
    }
    
    // Prefer server-provided sequence number for ordering when available
    const seq = ev.meta && ev.meta._seq ? ev.meta._seq : null;
    
    if (ev.phase === 'start') {
      if (activeOperations.has(operationKey)) {
        const existing = activeOperations.get(operationKey);
        const msg = existing.querySelector('.progress-message');
        const time = existing.querySelector('.progress-time');
        if (msg) msg.textContent = ev.message || 'Starting...';
        if (time) time.textContent = formatTime(ev.timestamp);
      } else {
        const operationDiv = createTreeOperationDiv(operationKey, ev, depthLevel, parentId);
        
        // Insert at correct hierarchical position
        insertOperationHierarchically(container, operationDiv, requestId, parentId, depthLevel);
        
        activeOperations.set(operationKey, operationDiv);
        // Register node with requestId (if available) for tree structure
        if (requestId) {
          registerNode(requestId, parentId, operationDiv, depthLevel);
        }
      }
    } else if (ev.phase === 'progress') {
      let operationDiv = activeOperations.get(operationKey);
      // If progress arrives before start, create a row from this progress event
      if (!operationDiv) {
        operationDiv = createTreeOperationDiv(operationKey, ev, depthLevel, parentId);
        
        // Insert at correct hierarchical position
        insertOperationHierarchically(container, operationDiv, requestId, parentId, depthLevel);
        
        activeOperations.set(operationKey, operationDiv);
        // Register node with requestId (if available) for tree structure
        if (requestId) {
          registerNode(requestId, parentId, operationDiv, depthLevel);
        }
      } else {
        const messageSpan = operationDiv.querySelector('.progress-message');
        const timeSpan = operationDiv.querySelector('.progress-time');
        if (messageSpan) messageSpan.textContent = ev.message || 'In progress...';
        if (timeSpan) timeSpan.textContent = formatTime(ev.timestamp);
        // Update request id if present
        if (ev.request_id) {
          let req = operationDiv.querySelector('.operation-request-id');
          if (!req) {
            const span = document.createElement('span');
            span.className = 'operation-request-id';
            span.textContent = ev.request_id;
            const timeSpan = operationDiv.querySelector('.progress-time');
            if (timeSpan && timeSpan.parentNode) timeSpan.parentNode.insertBefore(span, timeSpan.nextSibling);
          } else {
            req.textContent = ev.request_id;
          }
        }
      }
    } else if (ev.phase === 'end') {
      let operationDiv = activeOperations.get(operationKey);
      // If END arrives before START was processed, create the operation now
      if (!operationDiv) {
        operationDiv = createTreeOperationDiv(operationKey, ev, depthLevel, parentId);
        insertOperationHierarchically(container, operationDiv, requestId, parentId, depthLevel);
        if (requestId) {
          registerNode(requestId, parentId, operationDiv, depthLevel);
        }
      }
      
      const iconSpan = operationDiv.querySelector('.progress-icon');
      const messageSpan = operationDiv.querySelector('.progress-message');
      const timeSpan = operationDiv.querySelector('.progress-time');
      // Respect backend hint to suppress the completion icon for internal helpers
      const suppressIcon = ev.meta && ev.meta.suppress_completion_icon;
      if (iconSpan) iconSpan.innerHTML = suppressIcon ? '' : '<div class="checkmark">✓</div>';
      if (messageSpan) messageSpan.textContent = ev.message || 'Completed';
      if (timeSpan) timeSpan.textContent = formatTime(ev.timestamp);
      operationDiv.classList.add('completed');
      activeOperations.delete(operationKey);
      // Keep tree structure intact for folding - don't clean up completed operations
    } else if (ev.phase === 'error') {
      let operationDiv = activeOperations.get(operationKey);
      // If ERROR arrives before START was processed, create the operation now
      if (!operationDiv) {
        operationDiv = createTreeOperationDiv(operationKey, ev, depthLevel, parentId);
        insertOperationHierarchically(container, operationDiv, requestId, parentId, depthLevel);
        if (requestId) {
          registerNode(requestId, parentId, operationDiv, depthLevel);
        }
      }
      
      const iconSpan = operationDiv.querySelector('.progress-icon');
      const messageSpan = operationDiv.querySelector('.progress-message');
      const timeSpan = operationDiv.querySelector('.progress-time');
      if (iconSpan) iconSpan.innerHTML = '<div class="error-mark">✕</div>';
      if (messageSpan) messageSpan.textContent = ev.message || 'Error occurred';
      if (timeSpan) timeSpan.textContent = formatTime(ev.timestamp);
      operationDiv.classList.add('error');
      activeOperations.delete(operationKey);
      // Keep tree structure intact for folding - don't clean up errored operations
    }
  }

  function formatTime(ts) {
    try {
      return new Date(ts).toLocaleTimeString();
    } catch (e) {
      return ts;
    }
  }

  // Event source tracking (shared across init calls and cleanup)
  let currentEventSource = null;
  let currentStatusEventSource = null;
  // True while a fetch()-based SSE stream (POST /events or POST /run) is live.
  // currentEventSource is ONLY set on the page-refresh EventSource reconnect
  // path, never for the normal fetch+getReader() streams - so it cannot be
  // used to detect an active request. This flag closes that gap: it gates
  // hasActiveRequest(), the session:loaded clobber guard, and the
  // append-to-running-request branch.
  let streamActive = false;
  // Block object the live stream consumer renders into (same object identity
  // as the blk passed to handleSSEEvent). Mid-run appends rebind its fields to
  // a fresh block so the agent's reaction renders below the injected message.
  let activeStreamBlk = null;
  let closeEventSourceTimer = null; // Timer to delay closing EventSource after final/end
  
  // SSE Reconnection state for long-running requests
  let sseReconnectAttempts = 0;
  const SSE_MAX_RECONNECT_ATTEMPTS = 5;
  const SSE_BASE_RECONNECT_DELAY_MS = 1000; // Start with 1s, doubles each retry
  let sseReconnectTimer = null;
  let sseReceivedFinalOrEnd = false; // Track if we've completed normally
  
  // Streaming state tracking
  let currentStreamingContent = '';
  let currentStreamingStep = null;

  // Set when a user message was appended to the RUNNING request. The stream's
  // block is rebound to a fresh one only when the NEXT step actually starts —
  // rebinding at append time would hijack the still-streaming current step
  // (thinking_delta re-renders the full accumulated text into whatever block
  // blk points at), teleporting the in-flight answer below the injected
  // message and letting the post-drain step overwrite it.
  let pendingAppendRebind = false;

  // Move the live stream to a fresh assistant block (appended at the end of the
  // chat, i.e. below any injected user message) by mutating the SAME blk object
  // the stream handlers hold — object identity is what makes the in-place
  // rebind work (see the 'continuation' handler and the mid-run append flow).
  function rebindLiveBlock(blk) {
    const newBlk = addAssistantBlock(chatContainer);
    blk.row = newBlk.row;
    blk.box = newBlk.box;
    blk.t = newBlk.t;
    blk.think = newBlk.think;
    blk.status = newBlk.status;
    blk.thinkingSection = newBlk.thinkingSection;
    blk.statusSection = newBlk.statusSection;
    blk.responseSection = newBlk.responseSection;
    scrollBottom();
  }
  
  // DOM elements (set in init, shared across handlers)
  let runBtn = null;
  let stopBtn = null;
  let chatContainer = null;

  // One action-button slot, driven by (run active? typed anything?):
  //   idle                      -> Run
  //   running, input empty      -> Stop
  //   running, input has text   -> Send  (injects into the RUNNING agent; the
  //                                submit handler POSTs it to
  //                                /events/{id}/append — see
  //                                docs/mid_run_message_injection.md)
  // Clearing the input flips Send back to Stop. Without this, a run only ever
  // showed Stop and mid-run injection was unreachable by mouse.
  let runActive = false;

  function updateActionButton() {
    if (!runBtn || !stopBtn) return;
    const ti = document.getElementById('task');
    const hasText = !!(ti && ti.value.trim());
    const showSend = !runActive || hasText;
    runBtn.style.display = showSend ? 'block' : 'none';
    stopBtn.style.display = showSend ? 'none' : 'block';
    runBtn.setAttribute(
      'title', runActive ? 'Send to running agent (Ctrl+Enter)' : 'Run (Ctrl+Enter)');
    runBtn.setAttribute('aria-label', runActive ? 'Send to running agent' : 'Run');
  }
  
  // Session and request tracking (shared across init and event listeners)
  let currentRequestId = null;
  // Initialize from sessionStorage to handle page refresh before session:loaded event fires
  let currentSessionId = sessionStorage.getItem('lastSessionId') || null;
  
  // Store/retrieve active request ID for reconnect after browser refresh
  // Using sessionStorage (not localStorage) so each tab has its own request ID
  const ACTIVE_REQUEST_KEY = 'activeRequestId';
  
  function storeActiveRequest(requestId) {
    if (requestId) {
      sessionStorage.setItem(ACTIVE_REQUEST_KEY, requestId);
    } else {
      sessionStorage.removeItem(ACTIVE_REQUEST_KEY);
    }
  }
  
  function getStoredActiveRequest() {
    return sessionStorage.getItem(ACTIVE_REQUEST_KEY);
  }

  // Helper functions to update UI displays (module-level for handleSSEEvent access)
  function updateHeaderSessionId() {
    const sessionElement = document.getElementById('headerSessionId');
    if (sessionElement) {
      const sessionId = currentSessionId || '';
      // No 'Session:' prefix per design; leave empty when no session
      sessionElement.textContent = sessionId;
      const container = document.querySelector('.session-id-bottom');
      if (container) {
        container.style.display = currentSessionId ? 'block' : 'none';
        // set title to full id so users can hover to see it
        container.title = sessionId || '';
      }
    } else {
      console.warn('headerSessionId element not found');
    }
  }
  
  function updateRequestId() {
    if (currentRequestId) {
      // Find the latest assistant message
      const latestAssistant = document.querySelector('.chat .row:last-child .msg.assistant');
      if (latestAssistant) {
        // Avoid inserting duplicate request id elements
        let existing = latestAssistant.querySelector('.message-request-id');
        if (!existing) {
          const requestIdElement = document.createElement('div');
          requestIdElement.className = 'message-request-id';
          requestIdElement.innerHTML = `Request: <span>${currentRequestId}</span>`;
          requestIdElement.title = `Request ID: ${currentRequestId}`;
          latestAssistant.appendChild(requestIdElement);
        } else {
          existing.innerHTML = `Request: <span>${currentRequestId}</span>`;
          existing.title = `Request ID: ${currentRequestId}`;
        }
      }
    }

    // Also update the global request display (keep for compatibility)
    const requestElement = document.getElementById('currentRequestId');
    const requestContainer = document.getElementById('requestIdDisplay');
    if (requestElement && requestContainer) {
      requestElement.textContent = currentRequestId || '--';
      requestContainer.style.display = 'none'; // Hide the global one, we use per-message now
    }
  }

  // Shared SSE event handler for both EventSource and manual fetch() parsing
  // Module-level so it can be used by both normal requests and reconnect logic
  function handleSSEEvent(data, blk) {
    switch (data.type) {
      case 'start':
        currentRequestId = data.request_id;
        currentSessionId = data.session_id;
        // Store for reconnect after browser refresh
        storeActiveRequest(currentRequestId);
        // update exported values
        try { global.currentSessionId = currentSessionId; } catch (e) {}
        
        // Notify session manager about new/updated session
        if (window.sessionManager && typeof window.sessionManager.onSessionUpdated === 'function') {
          window.sessionManager.onSessionUpdated(currentSessionId);
        }
        
        // Update header session ID display
        updateHeaderSessionId();
        
        // Update request ID display  
        updateRequestId();
        break;
      case 'reconnect':
        // Reconnected to existing running job (after browser refresh)
        currentRequestId = data.request_id;
        currentSessionId = data.session_id;
        // update exported values
        try { global.currentSessionId = currentSessionId; } catch (e) {}
        
        // Update agent selector to match the job's agent
        if (data.agent_name && window.selectorModule && typeof window.selectorModule.setAgent === 'function') {
          window.selectorModule.setAgent(data.agent_name);
        }
        
        // Update LLM profile selector to match the job's profile
        if (data.llm_profile && window.selectorModule && typeof window.selectorModule.setLLMProfile === 'function') {
          window.selectorModule.setLLMProfile(data.llm_profile);
        }
        
        // Notify session manager about reconnected session
        if (window.sessionManager && typeof window.sessionManager.onSessionUpdated === 'function') {
          window.sessionManager.onSessionUpdated(currentSessionId);
        }
        
        // Update header session ID display
        updateHeaderSessionId();
        
        // Update request ID display  
        updateRequestId();
        
        // Show reconnect info in response area
        showSection(blk.t);
        blk.t.innerHTML = `<div class="response-text reconnect-info">🔄 ${data.message}${data.last_status ? '<br><em>Last status: ' + data.last_status + '</em>' : ''}</div>`;
        break;
      case 'heartbeat':
        // Keep-alive heartbeat during long LLM calls - ignore but log in debug mode
        if (window.DEBUG_MODE) {
          console.log('Heartbeat received (step', data.step, ')');
        }
        break;
      case 'thinking_delta':
        // Real-time token streaming from LLM - stream directly to response box
        if (data.step !== currentStreamingStep) {
          // New step - reset accumulator
          currentStreamingContent = '';
          currentStreamingStep = data.step;
          // A message was appended mid-run: THIS step is the agent's reaction
          // to it — stream it into a fresh block below the injected message.
          if (pendingAppendRebind) {
            pendingAppendRebind = false;
            rebindLiveBlock(blk);
          }
        }
        
        // Update with accumulated content + cursor directly in response box
        currentStreamingContent = data.accumulated || '';
        showSection(blk.t);
        blk.t.innerHTML = `<div class="response-text streaming">${formatTextWithLineBreaks(currentStreamingContent)}<span class="typing-cursor">|</span></div>`;
        
        // Auto-scroll to keep cursor visible
        blk.t.scrollIntoView({ behavior: 'smooth', block: 'end' });
        break;
      case 'thinking_complete':
        // Final thinking event from streaming - remove cursor, keep content
        currentStreamingContent = '';
        currentStreamingStep = null;
        
        if (data.assistant && data.assistant.content) {
          // Content was already displayed via thinking_delta
          // Now show final formatted content (HTML from format_output hook)
          const contentFormat = data.content_format || 'text';
          showSection(blk.t);
          blk.t.innerHTML = `<div class="response-text">${formatContent(data.assistant.content, contentFormat)}</div>`;
          
          // Apply Prism.js syntax highlighting if available and content is HTML
          if (contentFormat === 'html' && typeof Prism !== 'undefined') {
            Prism.highlightAllUnder(blk.t);
          }
        }
        
        // Note: Tool calls display is handled by the 'thinking' event to avoid duplicates
        break;
      case 'thinking':
        // Complete thinking event (also handles backward compatibility)
        // Only clear streaming state if this has actual content (final thinking event)
        if (data.assistant) {
          // Final thinking event with content - clear streaming state
          currentStreamingContent = '';
          currentStreamingStep = null;
          if (blk.think) {
            blk.think.classList.remove('streaming');
            // Remove typing cursor if present
            const cursor = blk.think.querySelector('.typing-cursor');
            if (cursor) cursor.remove();
          }
          
          // Create think section if not exists
          if (!blk.think) {
            blk.think = document.createElement('pre');
            blk.think.className = 'think-section';
            blk.r.appendChild(blk.think);
          }
          
          if (data.assistant.content) {
            blk.think.textContent += `💭 Step ${data.step}: ${data.assistant.content}\n\n`;
          }
          if (data.assistant.tool_calls && data.assistant.tool_calls.length > 0) {
            blk.think.textContent += `🧠 Step ${data.step}: Planning to call ${data.assistant.tool_calls.length} tool(s):\n`;
            data.assistant.tool_calls.forEach((tc, i) => {
              const func = tc.function || {};
              blk.think.textContent += `  ${i + 1}. ${func.name || 'unknown'}\n`;
            });
            blk.think.textContent += '\n';
          }
          showSection(blk.think);
        } else {
          // Step marker event (before LLM call) - don't interfere with streaming
          // Next step starting: apply a deferred mid-run-append rebind so the
          // step renders below the injected user message. Guard on !content:
          // the pure pre-LLM marker is {type, step}, while the "simplified
          // thinking" event emitted at the END of a text-only step carries
          // `content` — rebinding on that one would strand an empty block
          // when a continuation hook fires right after.
          if (pendingAppendRebind && !data.content) {
            pendingAppendRebind = false;
            rebindLiveBlock(blk);
          }
          // Just ensure think section exists
          if (!blk.think) {
            blk.think = document.createElement('pre');
            blk.think.className = 'think-section';
            blk.r.appendChild(blk.think);
          }
        }
        break;
      case 'status':
        // Status events are now delivered through /events stream
        // Show status events for this request AND all hierarchical children (sub-agents)
        // e.g., if currentRequestId is "abc123", also show "abc123_sub_001", "abc123_001_sub_002", etc.
        if (blk && blk.status) {
          const eventRequestId = data.request_id || '';
          // Check if this event belongs to current request hierarchy
          // Either exact match OR starts with current request_id followed by underscore (child operation)
          const matches = eventRequestId === currentRequestId || 
              (eventRequestId && currentRequestId && eventRequestId.startsWith(currentRequestId + '_'));
          
          if (matches) {
            addStatusEvent(blk.status, data);
          }
          // Otherwise silently ignore status from other requests/sessions
        }
        break;
      case 'status_batch':
        // Batched status events for efficiency (multiple events in one SSE message)
        if (blk && blk.status && data.events && Array.isArray(data.events)) {
          data.events.forEach(statusEvent => {
            const eventRequestId = statusEvent.request_id || '';
            const matches = eventRequestId === currentRequestId || 
                (eventRequestId && currentRequestId && eventRequestId.startsWith(currentRequestId + '_'));
            
            if (matches) {
              addStatusEvent(blk.status, statusEvent);
            }
          });
        }
        break;
      case 'continuation':
        // Auto-continuation: system injected a user message to keep the agent working
        // Display it in the chat as a system-injected user message
        {
          const contRow = document.createElement('div');
          contRow.className = 'row';
          const contMsg = document.createElement('div');
          contMsg.className = 'msg user continuation-msg';
          contMsg.innerHTML = `<div class="continuation-badge">🔄 Auto-Continue #${data.count || '?'}</div><div class="continuation-reason">${escapeHtml(data.reason || '')}</div><div class="continuation-text">${formatTextWithLineBreaks(data.message || '')}</div>`;
          contRow.appendChild(contMsg);
          chatContainer.appendChild(contRow);
          // Create a new assistant block for the next response and update blk
          // in-place; this supersedes any deferred mid-run-append rebind.
          pendingAppendRebind = false;
          rebindLiveBlock(blk);
        }
        break;
      case 'final':
        // Mark completion for reconnect logic
        sseReceivedFinalOrEnd = true;
        sseReconnectAttempts = 0;
        if (pendingAppendRebind) {
          // Edge (e.g. max-steps): the run finalizes without another step. The
          // final would be suppressed against the old block's non-empty content
          // — render it into a fresh block below the injected message instead.
          pendingAppendRebind = false;
          rebindLiveBlock(blk);
        }
        // Clear stored request (job finished)
        storeActiveRequest(null);
        
        // Only show final if content box is still empty (no streaming happened)
        // or if it's a different format
        const finalContent = data.summary || data.content || '';
        const finalContentFormat = data.content_format || 'text';
        
        if (!blk.t.innerHTML || blk.t.innerHTML.trim() === '') {
          // No streaming happened, show final content
          showSection(blk.t);
          blk.t.innerHTML = `<div class="response-text">${formatContent(finalContent, finalContentFormat)}</div>`;
          
          // Apply Prism.js syntax highlighting if available and content is HTML
          if (finalContentFormat === 'html' && typeof Prism !== 'undefined') {
            Prism.highlightAllUnder(blk.t);
          }
        }
        // If streaming already filled the content, skip this (content already there)
        break;
      case 'end':
        // Mark completion for reconnect logic
        sseReceivedFinalOrEnd = true;
        sseReconnectAttempts = 0;
        pendingAppendRebind = false;
        // Clear stored request (job finished)
        storeActiveRequest(null);
        
        // Close EventSource immediately to prevent auto-reconnect attempts
        // EventSource will try to reconnect if the server closes the connection,
        // which causes spurious "Connection failed" errors in the onerror handler
        if (currentEventSource) {
          currentEventSource.close();
          currentEventSource = null;
        }
        if (closeEventSourceTimer) {
          clearTimeout(closeEventSourceTimer);
          closeEventSourceTimer = null;
        }
        
        // Clear any pending reconnect timer
        if (sseReconnectTimer) {
          clearTimeout(sseReconnectTimer);
          sseReconnectTimer = null;
        }
        
        runActive = false; updateActionButton(); // back to idle 'Run'
        stopBtn.style.display = 'none'; // Hide stop button
        // Reset stop button state
        stopBtn.setAttribute('title', 'Stop');
        stopBtn.setAttribute('aria-label', 'Stop');
        stopBtn.disabled = false;
        stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
        
        // Reload sessions after conversation completes
        if (window.sessionManager && typeof window.sessionManager.loadSessions === 'function') {
          window.sessionManager.loadSessions();
        }
        break;
      case 'error':
        showSection(blk.t);
        blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks(data.message)}</div>`;
        // Clear any pending close timer
        if (closeEventSourceTimer) {
          clearTimeout(closeEventSourceTimer);
          closeEventSourceTimer = null;
        }
        if (currentEventSource) {
          currentEventSource.close();
          currentEventSource = null;
        }
        if (currentStatusEventSource) {
          currentStatusEventSource.close();
          currentStatusEventSource = null;
        }
        runActive = false; updateActionButton(); // back to idle 'Run'
        stopBtn.style.display = 'none'; // Hide stop button
        // Reset stop button state
        stopBtn.setAttribute('title', 'Stop');
        stopBtn.setAttribute('aria-label', 'Stop');
        stopBtn.disabled = false;
        stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
        break;
      case 'cancelled':
        // Request was cancelled - clean up and reset UI
        console.log('Request cancelled:', data.request_id, 'at step', data.step);
        // Clear any pending close timer
        if (closeEventSourceTimer) {
          clearTimeout(closeEventSourceTimer);
          closeEventSourceTimer = null;
        }
        if (currentEventSource) {
          currentEventSource.close();
          currentEventSource = null;
        }
        if (currentStatusEventSource) {
          currentStatusEventSource.close();
          currentStatusEventSource = null;
        }
        // Show cancelled status with step number
        showSection(blk.t);
        const stepInfo = data.step ? ` at step ${data.step}` : '';
        blk.t.innerHTML = `<div class="response-text" style="opacity: 0.6;">Request cancelled${stepInfo}</div>`;
        runActive = false; updateActionButton(); // back to idle 'Run'
        stopBtn.style.display = 'none'; // Hide stop button
        // Reset stop button state
        stopBtn.setAttribute('title', 'Stop');
        stopBtn.setAttribute('aria-label', 'Stop');
        stopBtn.disabled = false;
        stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
        break;
    }
    scrollBottom();
  }

  // Public init function that wires the chat form behavior
  chatModule.init = function (opts) {
    const chatForm = document.getElementById('f');
    const taskInput = document.getElementById('task');
    runBtn = document.getElementById('runBtn');
    stopBtn = document.getElementById('stopBtn');
    chatContainer = document.getElementById('chat');
    
    // Expose current session id for other modules (fallback for UI)
    chatModule.getCurrentSessionId = function() { return currentSessionId; };
    Object.defineProperty(chatModule, 'currentSessionId', {
      get: function() { return currentSessionId; }
    });
    // Also export to global window for older modules
    try { global.currentSessionId = currentSessionId; } catch (e) { /* ignore */ }
    
    // Clear session function (called on logout)
    chatModule.clearSession = function() {
      currentSessionId = null;
      try { global.currentSessionId = null; } catch (e) { /* ignore */ }
      sessionStorage.removeItem('lastSessionId');
      updateHeaderSessionId();
    };
    
    // Event sources are now declared at module level (above init function)

    if (!chatForm || !taskInput || !runBtn || !stopBtn || !chatContainer) {
      console.warn('Chat form elements not found');
      return;
    }

    // Slash commands + skills: catalogue and parsing come from the server, so
    // the browser offers exactly what the terminal offers.
    if (window.slashCommands) {
      window.slashCommands.attach(taskInput);
    }

    // Initialize UI displays
    updateHeaderSessionId();
    updateRequestId();

    // While a run is active the action button is Stop — but as soon as the user
    // types something it becomes Send, so the text can be injected into the
    // running agent. Clearing the input flips it back to Stop.
    taskInput.addEventListener('input', updateActionButton);
    updateActionButton();

    // Stop button event listener
    stopBtn.addEventListener('click', async function() {
      if (currentRequestId) {
        // Sofortiges Feedback geben: preserve icon, update accessible label and tooltip
        stopBtn.setAttribute('title', 'Canceling');
        stopBtn.setAttribute('aria-label', 'Canceling');
        stopBtn.disabled = true;
        stopBtn.classList.add('cancelling');

        // Timeout: Nach 60 Sekunden automatisch zurücksetzen falls Backend nicht antwortet
        const timeoutId = setTimeout(() => {
          console.warn('Cancel request timeout after 60 seconds');
          stopBtn.setAttribute('title', 'Timeout');
          stopBtn.setAttribute('aria-label', 'Timeout');
          stopBtn.classList.remove('cancelling');
          stopBtn.classList.add('cancel-failed');
          
          // Nach weiteren 2 Sekunden komplett zurücksetzen und UI wiederherstellen
          setTimeout(() => {
            stopBtn.setAttribute('title', 'Stop');
            stopBtn.setAttribute('aria-label', 'Stop');
            stopBtn.disabled = false;
            stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
            
            // Clear any pending close timer
            if (closeEventSourceTimer) {
              clearTimeout(closeEventSourceTimer);
              closeEventSourceTimer = null;
            }
            
            // UI zurücksetzen: Run-Button anzeigen, Stop-Button verstecken
            runActive = false; updateActionButton();
            stopBtn.style.display = 'none';
            currentRequestId = null;
            currentEventSource = null;
          }, 2000);
        }, 60000); // 60 Sekunden

        try {
          const response = await fetch(`/api/requests/${currentRequestId}/cancel`, { method: 'POST' });
          const result = await response.json();
          console.log('Cancel request result:', result);
          
          // Timeout abbrechen da Antwort erhalten
          clearTimeout(timeoutId);

          // Wenn Request nicht gefunden wurde (z.B. nach Server-Neustart), State clearen
          if (result.status === 'not_found') {
            console.warn('Request not found - clearing stale state (possible server restart)');
            // Clear any pending close timer
            if (closeEventSourceTimer) {
              clearTimeout(closeEventSourceTimer);
              closeEventSourceTimer = null;
            }
            currentRequestId = null;
            currentEventSource = null;
            // UI zurücksetzen
            runActive = false; updateActionButton();
            stopBtn.style.display = 'none';
            stopBtn.classList.remove('cancelling');
            stopBtn.disabled = false;
            return; // Frühzeitig beenden
          }

          // Kurze Verzögerung für besseres UX-Feedback
          setTimeout(() => {
            if (result.status === 'cancelled') {
              stopBtn.setAttribute('title', 'Done');
              stopBtn.setAttribute('aria-label', 'Done');
              stopBtn.classList.remove('cancelling');
              stopBtn.classList.add('cancelled');
            } else {
              stopBtn.setAttribute('title', 'Failed');
              stopBtn.setAttribute('aria-label', 'Failed');
              stopBtn.classList.remove('cancelling');
              stopBtn.classList.add('cancel-failed');
            }
          }, 500);

        } catch (error) {
          console.error('Failed to cancel request:', error);
          // Timeout abbrechen da Fehler erhalten
          clearTimeout(timeoutId);
          
          stopBtn.setAttribute('title', 'Failed');
          stopBtn.setAttribute('aria-label', 'Failed');
          stopBtn.classList.remove('cancelling');
          stopBtn.classList.add('cancel-failed');
        }

        // Nach 2 Sekunden wieder zurücksetzen (falls Anfrage noch läuft)
        setTimeout(() => {
          if (stopBtn.style.display !== 'none') { // Nur zurücksetzen wenn Button noch sichtbar
            stopBtn.setAttribute('title', 'Stop');
            stopBtn.setAttribute('aria-label', 'Stop');
            stopBtn.disabled = false;
            stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
          }
        }, 2000);
      }
    });

    // Guards ONLY the window between reading the input and clearing it. The
    // slash resolve is an await, so two quick Ctrl+Enters (or a double-clicked
    // Run) both saw the same text and started the turn twice.
    //
    // It must be released the moment the input is consumed, NOT when the
    // handler returns: handleSubmit reads the SSE stream to its end
    // (`await reader.read()` in a loop), so holding the guard that long
    // swallowed every mid-run injection for the whole run -- the one thing you
    // reach for when a turn is taking too long.
    let submitting = false;

    chatForm.addEventListener('submit', async function(e) {
      e.preventDefault();
      if (submitting) return;
      submitting = true;
      try {
        await handleSubmit();
      } finally {
        submitting = false;  // safety net; the handler releases it far earlier
      }
    });

    async function handleSubmit() {
      let task = taskInput.value.trim();

      // Slash commands and skills. The server resolves them with the same
      // parser the terminal chat uses, so "/writer x" means the same thing on
      // both surfaces. `typed` keeps what the user wrote: the chat shows
      // "/writer x", not the skill's whole body.
      let typed = null;
      if (task.startsWith('/') && !task.startsWith('//') && window.slashCommands) {
        const resolved = await window.slashCommands.resolve(task);
        if (!resolved) {
          addNote(chatContainer, 'Could not reach the server to resolve "' + task + '".');
          submitting = false;
          return;
        }
        if (resolved.kind === 'command') {
          // Awaited: the commands that ask the server for their answer take a
          // round trip, and letting the caller finish first re-armed the input
          // before the note appeared.
          clearInput(taskInput);
          await runChatCommand(resolved.name, resolved.payload);
          submitting = false;
          return;
        }
        if (resolved.kind === 'plugin') {
          clearInput(taskInput);
          await runPluginCommand(resolved.name, resolved.payload);
          submitting = false;
          return;
        }
        if (resolved.kind === 'unknown') {
          const hint = resolved.suggestion ? '  Did you mean ' + resolved.suggestion + '?' : '';
          addNote(chatContainer, 'Unknown command: ' + resolved.payload + hint +
            '\n/help lists the commands; //' + resolved.payload.slice(1) + ' sends it as a message.');
          // The text stays so the typo can be corrected, but the dropdown must
          // not keep hold of the next Enter.
          if (window.slashCommands) window.slashCommands.close();
          submitting = false;
          return;
        }
        if (resolved.kind === 'skill') {
          typed = task;
          task = resolved.text || task;
        } else if (resolved.text) {
          task = resolved.text;
        }
      }

      // Check if we have files to upload
      const hasFiles = window.fileUploadModule && window.fileUploadModule.hasValidFiles();
      const files = hasFiles ? window.fileUploadModule.getFiles() : [];
      
      // Require either task text or files
      if (!task && !hasFiles) return;
      
      // Add user message to chat. For a skill it is what the user TYPED --
      // pasting the expanded body back at them would bury the conversation.
      let displayText = typed || task || '';
      // Get file breakdown by type from file upload module
      const filesByType = window.fileUploadModule ? window.fileUploadModule.getFilesByType() : { images: [], audio: [], text: [] };
      
      // Pass images, audio and text files separately to addUser
      addUser(chatContainer, displayText, filesByType.images, filesByType.audio, filesByType.text);
      taskInput.value = '';
      // The input is consumed -- everything below is the run itself, during
      // which the user must be able to type the next message.
      submitting = false;
      // Trigger input event so auto-resize logic recalculates height immediately
      try {
        const ev = new Event('input', { bubbles: true, cancelable: false });
        taskInput.dispatchEvent(ev);
      } catch (e) {
        // Older browsers fallback
        taskInput.dispatchEvent(document.createEvent('Event'));
      }

      // If there's an active request, append the user message to it
      // Note: Multimodal append not yet supported, only text append
      // (streamActive, not currentEventSource: fetch streams never set the latter)
      if (currentRequestId && streamActive && !hasFiles) {
        let appended = false;
        try {
          const appendHeaders = { 'Content-Type': 'application/json' };
          const appendToken = localStorage.getItem('token');
          if (appendToken) {
            appendHeaders['Authorization'] = `Bearer ${appendToken}`;
          }
          // fallback=none: if the run just finished, start a new request below
          // instead of parking the message unanswered in the session.
          const resp = await fetch(`/events/${encodeURIComponent(currentRequestId)}/append?fallback=none`, {
            method: 'POST',
            headers: appendHeaders,
            body: JSON.stringify({ content: task })
          });
          appended = resp.ok;
          if (!appended) {
            console.warn(`Append rejected (${resp.status}), starting a new request instead`);
          }
        } catch (err) {
          console.error('Failed to append to active request:', err);
          // fall through to starting a new request
        }

        if (appended) {
          // The running agent picks the message up at its NEXT step. Do NOT
          // rebind the stream block yet — the current step is usually still
          // streaming into it, and thinking_delta re-renders the full
          // accumulated text into whatever block blk points at, which would
          // teleport the in-flight answer below the injected message.
          // handleSSEEvent performs the rebind when the next step starts.
          pendingAppendRebind = true;
          if (activeStreamBlk && activeStreamBlk.status) {
            // Visible confirmation — without it the UI looks stalled until the
            // agent's current step finishes and the reaction starts.
            // phase 'end' renders a persistent completed (✓) row; the synthetic
            // unique request_id keeps it from mutating the agent's own
            // operation row (operationKey = request_id in addStatusEvent).
            addStatusEvent(activeStreamBlk.status, {
              type: 'status',
              phase: 'end',
              message: 'Message delivered to the running agent — it reacts at its next step',
              request_id: `${currentRequestId}_user_append_${Date.now()}`,
              timestamp: new Date().toISOString()
            });
            scrollBottom();
          }
          runActive = true; updateActionButton();
          stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
          stopBtn.disabled = false;
          stopBtn.setAttribute('title', 'Stop');
          stopBtn.setAttribute('aria-label', 'Stop');
          return;
        }
        // Run no longer active: fall through to starting a new request with
        // this message as the task (the message was NOT stored server-side).
      }

      // No active request or has files: start a new request
      const blk = addAssistantBlock(chatContainer);
      activeStreamBlk = blk;
      runActive = true; updateActionButton();  // -> Stop (empty input)
      stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
      stopBtn.disabled = false;
      stopBtn.setAttribute('title', 'Stop');
      stopBtn.setAttribute('aria-label', 'Stop');
      currentRequestId = null; // Will be set when SSE 'start' event arrives
      pendingAppendRebind = false; // stale flag from a previous run must not leak

      // Use FormData for all requests (supports both text-only and multimodal)
      if (hasFiles) {
        // Build FormData for multimodal request
        const formData = new FormData();
        formData.append('task', task);
        files.forEach(file => {
          formData.append('files', file);
        });

        // Add agent and LLM profile selections if available
        const selectedAgent = window.selectorModule && window.selectorModule.getCurrentAgent ? window.selectorModule.getCurrentAgent() : null;
        const selectedLLMProfile = window.selectorModule && window.selectorModule.getCurrentLLMProfile ? window.selectorModule.getCurrentLLMProfile() : null;
        
        if (selectedAgent) {
          formData.append('agent_name', selectedAgent);
        }
        if (selectedLLMProfile) {
          formData.append('llm_profile', selectedLLMProfile);
        }
        
        // Add current session ID if exists (to continue existing session)
        // Fallback to sessionStorage if currentSessionId not yet set (race condition on page load)
        const effectiveSessionId = currentSessionId || sessionStorage.getItem('lastSessionId');
        if (effectiveSessionId) {
          formData.append('session_id', effectiveSessionId);
        }

        try {
          showSection(blk.t);
          blk.t.innerHTML = '<div class="response-text">Processing images...</div>';

          // Close any existing status event source before starting a new one
          if (currentStatusEventSource) {
            currentStatusEventSource.close();
            currentStatusEventSource = null;
          }
          
          // Status events now come through /events SSE stream - no separate connection needed

          // Mark a live stream so hasActiveRequest()/guards work (fetch streams
          // never set currentEventSource).
          streamActive = true;

          // Stream SSE response from /run endpoint
          const response = await fetch('/run', {
            method: 'POST',
            body: formData
          });

          if (!response.ok) {
            const errorText = await response.text();
            let errorMsg = 'Request failed';
            try {
              const errorJson = JSON.parse(errorText);
              errorMsg = errorJson.detail || errorMsg;
            } catch (e) {
              errorMsg = errorText || errorMsg;
            }
            showSection(blk.t);
            blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks(errorMsg)}</div>`;
            runActive = false; updateActionButton();
            stopBtn.style.display = 'none';
            // Reset stop button state
            stopBtn.setAttribute('title', 'Stop');
            stopBtn.setAttribute('aria-label', 'Stop');
            stopBtn.disabled = false;
            stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
            return;
          }

                    // Response is SSE stream - parse it manually
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = '';
          let sseOk = false;

          while (true) {
            const {done, value} = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, {stream: true});
            const lines = buffer.split('\n');
            buffer = lines.pop(); // Keep incomplete line in buffer

            for (const line of lines) {
              if (line.startsWith(':')) {
                sseOk = true;
                continue;
              }
              if (line.startsWith('event:')) {
                continue;
              }
              if (line.startsWith('data:')) {
                const jsonStr = line.substring(5).trim();
                if (!jsonStr) continue;
                try {
                  const ev = JSON.parse(jsonStr);
                  handleSSEEvent(ev, blk);
                } catch (e) {
                  console.error('Failed to parse SSE data:', e);
                }
              }
            }
          }

          if (!sseOk) {
            showSection(blk.t);
            blk.t.innerHTML = '<div class="response-text error">SSE connection failed</div>';
          }

          // Clear files after successful send
          if (window.fileUploadModule) {
            window.fileUploadModule.clearFiles();
          }

        } catch (err) {
          showSection(blk.t);
          blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks('Request failed: ' + String(err))}</div>`;
          // Clear any pending close timer
          if (closeEventSourceTimer) {
            clearTimeout(closeEventSourceTimer);
            closeEventSourceTimer = null;
          }
          if (currentStatusEventSource) {
            currentStatusEventSource.close();
            currentStatusEventSource = null;
          }
        } finally {
          // Clear any pending close timer
          if (closeEventSourceTimer) {
            clearTimeout(closeEventSourceTimer);
            closeEventSourceTimer = null;
          }
          runActive = false; updateActionButton();
          stopBtn.style.display = 'none';
          // Reset stop button state
          stopBtn.setAttribute('title', 'Stop');
          stopBtn.setAttribute('aria-label', 'Stop');
          stopBtn.disabled = false;
          stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
          currentRequestId = null;
          currentEventSource = null;
          streamActive = false;
        }
        return;
      }

      // Text-only SSE-based request via POST fetch (avoids URL length limits)
      
      // Reset reconnect state for new request
      sseReceivedFinalOrEnd = false;
      sseReconnectAttempts = 0;
      if (sseReconnectTimer) {
        clearTimeout(sseReconnectTimer);
        sseReconnectTimer = null;
      }
      
      // Get current agent and LLM profile selections
      const selectedAgent = window.selectorModule && window.selectorModule.getCurrentAgent ? window.selectorModule.getCurrentAgent() : null;
      const selectedLLMProfile = window.selectorModule && window.selectorModule.getCurrentLLMProfile ? window.selectorModule.getCurrentLLMProfile() : null;
      
      // Fallback to sessionStorage if currentSessionId not yet set (race condition on page load)
      const effectiveSessionId = currentSessionId || sessionStorage.getItem('lastSessionId');
      
      // Build POST body
      const postBody = { task: task };
      if (effectiveSessionId) postBody.session_id = effectiveSessionId;
      if (selectedAgent) postBody.agent_name = selectedAgent;
      if (selectedLLMProfile) postBody.llm_profile = selectedLLMProfile;
      
      // Build headers
      const postHeaders = { 'Content-Type': 'application/json' };
      const token = localStorage.getItem('token');
      if (token) {
        postHeaders['Authorization'] = `Bearer ${token}`;
      }
      
      // Close any existing status event source before starting a new one
      if (currentStatusEventSource) {
        currentStatusEventSource.close();
        currentStatusEventSource = null;
      }

      try {
        // Mark a live stream so hasActiveRequest()/guards work (fetch streams
        // never set currentEventSource).
        streamActive = true;
        const response = await fetch('/events', {
          method: 'POST',
          headers: postHeaders,
          body: JSON.stringify(postBody)
        });

        if (!response.ok) {
          const errorText = await response.text();
          let errorMsg = 'Request failed';
          try {
            const errorJson = JSON.parse(errorText);
            if (errorJson.status_code === 401 || response.status === 401) {
              errorMsg = '🔒 ' + (errorJson.detail || 'Authentication required') + ' - Please log in';
            } else if (errorJson.status_code === 403 || response.status === 403) {
              errorMsg = '🚫 ' + (errorJson.detail || 'Access denied');
            } else {
              errorMsg = errorJson.detail || errorJson.error || errorMsg;
            }
          } catch (e) {
            errorMsg = errorText || errorMsg;
          }
          showSection(blk.t);
          blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks(errorMsg)}</div>`;
          runActive = false; updateActionButton();
          stopBtn.style.display = 'none';
          stopBtn.setAttribute('title', 'Stop');
          stopBtn.setAttribute('aria-label', 'Stop');
          stopBtn.disabled = false;
          stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
          return;
        }

        // Parse SSE stream manually (same approach as file upload path)
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const {done, value} = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, {stream: true});
          const lines = buffer.split('\n');
          buffer = lines.pop(); // Keep incomplete line in buffer

          for (const line of lines) {
            if (line.startsWith(':')) {
              continue; // SSE comment / keepalive
            }
            if (line.startsWith('event:')) {
              continue;
            }
            if (line.startsWith('data:')) {
              const jsonStr = line.substring(5).trim();
              if (!jsonStr) continue;
              try {
                const ev = JSON.parse(jsonStr);
                handleSSEEvent(ev, blk);
              } catch (e) {
                console.error('Failed to parse SSE data:', e);
              }
            }
          }
        }

      } catch (err) {
        // Connection error - attempt reconnect if we have a request ID
        if (currentRequestId && sseReconnectAttempts < SSE_MAX_RECONNECT_ATTEMPTS && !sseReceivedFinalOrEnd) {
          sseReconnectAttempts++;
          const delay = SSE_BASE_RECONNECT_DELAY_MS * Math.pow(2, sseReconnectAttempts - 1);
          console.debug(`[SSE] Connection error, will attempt reconnect #${sseReconnectAttempts} in ${delay}ms`);
          
          if (blk && blk.status) {
            addStatusEvent(blk.status, {
              type: 'status',
              message: `Connection lost, reconnecting (attempt ${sseReconnectAttempts}/${SSE_MAX_RECONNECT_ATTEMPTS})...`,
              request_id: currentRequestId,
              timestamp: new Date().toISOString()
            });
          }
          
          // Poll for completion
          sseReconnectTimer = setTimeout(async function pollStatus() {
            if (!currentRequestId || sseReceivedFinalOrEnd) return;
            try {
              const statusUrl = `/api/requests/${currentRequestId}/status`;
              const r = await fetch(statusUrl);
              const status = await r.json();
              if (status.completed) {
                sseReceivedFinalOrEnd = true;
                if (status.result) {
                  showSection(blk.t);
                  const content = status.result.summary || status.result.content || JSON.stringify(status.result);
                  const contentFormat = status.result.content_format || 'text';
                  blk.t.innerHTML = `<div class="response-text">${formatContent(content, contentFormat)}</div>`;
                }
                runActive = false; updateActionButton();
                stopBtn.style.display = 'none';
                sseReconnectAttempts = 0;
              } else if (status.error) {
                showSection(blk.t);
                blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks(status.error)}</div>`;
                runActive = false; updateActionButton();
                stopBtn.style.display = 'none';
                sseReconnectAttempts = 0;
              } else if (sseReconnectAttempts < SSE_MAX_RECONNECT_ATTEMPTS) {
                sseReconnectAttempts++;
                sseReconnectTimer = setTimeout(pollStatus, SSE_BASE_RECONNECT_DELAY_MS * 2);
              } else {
                showSection(blk.t);
                const errorNotice = document.createElement('div');
                errorNotice.className = 'response-text error';
                errorNotice.innerHTML = formatTextWithLineBreaks('\n\n⚠️ Connection lost after ' + SSE_MAX_RECONNECT_ATTEMPTS + ' reconnect attempts');
                blk.t.appendChild(errorNotice);
                runActive = false; updateActionButton();
                stopBtn.style.display = 'none';
                sseReconnectAttempts = 0;
              }
            } catch (pollErr) {
              console.warn('[SSE] Status poll failed:', pollErr);
              if (sseReconnectAttempts < SSE_MAX_RECONNECT_ATTEMPTS) {
                sseReconnectAttempts++;
                sseReconnectTimer = setTimeout(pollStatus, SSE_BASE_RECONNECT_DELAY_MS * 2);
              }
            }
          }, delay);
          return; // Don't reset UI yet, reconnecting
        }
        
        // No reconnect possible
        if (!sseReceivedFinalOrEnd) {
          const errorMessage = currentRequestId ? 'Connection lost - possible timeout or network issue' : 'Connection failed - server may be unreachable';
          showSection(blk.t);
          const currentContent = blk.t.textContent || '';
          if (!currentContent.trim() || currentContent.includes('Thinking')) {
            blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks(errorMessage)}</div>`;
          } else {
            const errorNotice = document.createElement('div');
            errorNotice.className = 'response-text error';
            errorNotice.innerHTML = formatTextWithLineBreaks('\n\n⚠️ ' + errorMessage);
            blk.t.appendChild(errorNotice);
          }
        }
      } finally {
        // Clear any pending close timer
        if (closeEventSourceTimer) {
          clearTimeout(closeEventSourceTimer);
          closeEventSourceTimer = null;
        }
        if (currentStatusEventSource) {
          currentStatusEventSource.close();
          currentStatusEventSource = null;
        }
        runActive = false; updateActionButton();
        stopBtn.style.display = 'none';
        stopBtn.setAttribute('title', 'Stop');
        stopBtn.setAttribute('aria-label', 'Stop');
        stopBtn.disabled = false;
        stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
        currentEventSource = null;
        streamActive = false;
        // Clear stale request ID unless actively reconnecting
        // Without this, currentRequestId stays set after a completed request,
        // which can interfere with subsequent submissions
        if (!sseReconnectTimer) {
          currentRequestId = null;
          storeActiveRequest(null);
        }
        sseReconnectAttempts = 0;
      }
    }
    
    // Check for active request to reconnect after page refresh
    (async function reconnectToActiveJob() {
      const storedRequestId = getStoredActiveRequest();
      if (!storedRequestId) {
        return;
      }
      
      try {
        // Check if job is still running
        const response = await fetch(`/api/requests/${storedRequestId}/status`);
        const status = await response.json();
        
        if (status.status === 'running') {
          // Use the SAME setup as normal request - addAssistantBlock, etc.
          const blk = addAssistantBlock(chatContainer);
          activeStreamBlk = blk;
          
          runActive = true; updateActionButton();
          stopBtn.disabled = false;
          stopBtn.setAttribute('title', 'Stop');
          stopBtn.setAttribute('aria-label', 'Stop');
          stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
          
          // Build SSE URL - only pass request_id, backend uses job's agent
          const session = sessionStorage.getItem('lastSessionId') || '';
          let sseUrl = `/events?task=&request_id=${encodeURIComponent(storedRequestId)}&session_id=${encodeURIComponent(session)}`;
          
          const token = localStorage.getItem('token');
          if (token) {
            sseUrl += `&token=${encodeURIComponent(token)}`;
          }
          
          // Create EventSource and use THE SAME handleSSEEvent as normal flow
          const es = new EventSource(sseUrl, { withCredentials: true });
          currentEventSource = es;
          currentRequestId = storedRequestId;
          
          es.onmessage = (ev) => {
            try {
              const data = JSON.parse(ev.data);
              handleSSEEvent(data, blk);
            } catch (err) {
              console.warn('[chat_module] SSE reconnect parse error:', err);
            }
          };
          
          es.onerror = () => {
            es.close();
            currentEventSource = null;
            runActive = false; updateActionButton();
            stopBtn.style.display = 'none';
            stopBtn.disabled = false;
            stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
            storeActiveRequest(null);
          };
          
        } else {
          storeActiveRequest(null);
        }
      } catch (error) {
        console.warn('[chat_module] Failed to check active job status:', error);
        storeActiveRequest(null);
      }
    })();
  };
  
  function showSection(element) {
    const section = element.closest('.container-section');
    if (section && section.style.display === 'none') {
      section.style.display = 'block';
    }
  }

  // Cleanup function to close all event sources
  function cleanup() {
    if (currentEventSource) {
      currentEventSource.close();
      currentEventSource = null;
    }
    if (currentStatusEventSource) {
      currentStatusEventSource.close();
      currentStatusEventSource = null;
    }
    streamActive = false;
  }
  
  // Expose functions for testing
  chatModule.addStatusEvent = addStatusEvent;
  chatModule.toggleTreeNode = toggleTreeNode;
  chatModule.cleanup = cleanup;
  // The command grammar is shared with the terminal (chat_commands.py), and
  // this surface renders what that catalogue advertises -- so what the browser
  // DOES with an argument has to be measurable from outside. Without this the
  // only way in is a submit event, which needs the whole page.
  chatModule.runCommand = runChatCommand;
  
  // attach to global
  global.chatModule = chatModule;
  
  // Also attach to AgentSystem namespace for consistency with other modules
  global.AgentSystem = global.AgentSystem || {};
  global.AgentSystem.ChatModule = chatModule;
  
  // Cleanup on page unload
  window.addEventListener('beforeunload', cleanup);
  
  // Listen for new conversation events
  window.addEventListener('session:new', () => {
    console.log('New conversation event received - clearing session');
    // Clear current session ID
    currentSessionId = null;
    try { global.currentSessionId = null; } catch (e) {}
    
    // Update header to clear session ID display
    if (typeof updateHeaderSessionId === 'function') {
      updateHeaderSessionId();
    }
  });
  
  // Listen for session load events
  window.addEventListener('session:loaded', (event) => {
    const { session, readOnly, reason } = event.detail;
    
    // CRITICAL: Don't override chat if an SSE request is currently streaming!
    // This prevents race condition where session restore overwrites live streaming output.
    // streamActive covers fetch streams; currentEventSource covers the refresh-reconnect path.
    if (streamActive || currentEventSource) {
      console.warn('[session:loaded] Ignoring session load - SSE stream is active');
      return;
    }
    
    if (session && session.messages) {
      // Clear current chat
      const chatEl = document.getElementById('chat');
      if (chatEl) {
        chatEl.innerHTML = '';
        releasePreviewObjectUrls();
      }
      
      // Restore messages
      // First pass: find the last assistant message to determine if we need a placeholder
      let lastAssistantMsg = null;
      for (let i = session.messages.length - 1; i >= 0; i--) {
        if (session.messages[i].role === 'assistant') {
          lastAssistantMsg = session.messages[i];
          break;
        }
      }
      
      session.messages.forEach((msg, index) => {
        // Skip system messages and tool-related messages
        if (msg.role === 'system' || msg.role === 'tool') {
          return;
        }
        
        if (msg.role === 'user') {
          // Add user message
          const row = document.createElement('div');
          row.className = 'row';
          const msgDiv = document.createElement('div');
          msgDiv.className = 'msg user';
          
          // Handle multimodal content (array) or simple string content
          let displayText = '';
          let images = [];
          let audioFiles = [];
          let textFiles = [];
          
          if (Array.isArray(msg.content)) {
            // Parse multimodal content array
            const textParts = [];
            msg.content.forEach(item => {
              if (item.type === 'text') {
                textParts.push(item.text);
              } else if (item.type === 'image_url' || item.type === 'image') {
                images.push(item);
              } else if (item.type === 'audio') {
                audioFiles.push(item);
              } else if (item.type === 'text_file') {
                textFiles.push(item);
              }
            });
            displayText = textParts.join(' ') || '(File upload)';
          } else {
            displayText = msg.content || '';
          }
          
          const textSpan = document.createElement('div');
          textSpan.innerHTML = formatTextWithLineBreaks(displayText);
          msgDiv.appendChild(textSpan);
          
          // Add image previews if any
          if (images.length > 0) {
            const previewContainer = document.createElement('div');
            previewContainer.className = 'user-image-previews';
            
            images.forEach(item => {
              const img = document.createElement('img');
              const imageUrl = item.image_url?.url || item.url;
              img.src = imageUrl;
              img.alt = 'Uploaded image';
              img.title = 'Click to view full size';
              
              // Click to view full size
              img.onclick = () => openAttachmentInNewTab(imageUrl);
              
              previewContainer.appendChild(img);
            });
            
            msgDiv.appendChild(previewContainer);
          }
          
          // Add audio previews if any
          if (audioFiles.length > 0) {
            const audioContainer = document.createElement('div');
            audioContainer.className = 'user-audio-previews';
            audioContainer.style.cssText = 'margin-top: 10px; display: flex; flex-direction: column; gap: 8px;';
            
            audioFiles.forEach((item, index) => {
              const audioWrapper = document.createElement('div');
              audioWrapper.style.cssText = 'display: flex; align-items: center; gap: 8px;';
              
              // Extract audio data URL - handle different formats
              // Format 1: item.audio_url (from session storage)
              // Format 2: item.audio.data (alternative format)
              // Format 3: item.data (fallback)
              const audioData = item.audio_url || item.audio?.data || item.data;
              const mediaType = item.audio?.media_type || item.media_type || 'audio/flac';
              const audioName = item.name || `Audio ${index + 1}`;
              
              if (audioData) {
                // Create play button that plays audio directly
                const playBtn = document.createElement('button');
                playBtn.textContent = '▶️ ' + audioName;
                playBtn.style.cssText = 'background: #444; color: #ddd; border: 1px solid #666; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 0.9em;';
                playBtn.title = 'Click to play';
                
                // Create hidden audio element
                const audioEl = document.createElement('audio');
                audioEl.src = audioData;
                audioEl.style.display = 'none';
                
                let isPlaying = false;
                playBtn.onclick = () => {
                  if (isPlaying) {
                    audioEl.pause();
                    playBtn.textContent = '▶️ ' + audioName;
                    isPlaying = false;
                  } else {
                    audioEl.play();
                    playBtn.textContent = '⏸️ ' + audioName;
                    isPlaying = true;
                  }
                };
                
                audioEl.onended = () => {
                  playBtn.textContent = '▶️ ' + audioName;
                  isPlaying = false;
                };
                
                audioWrapper.appendChild(playBtn);
                audioWrapper.appendChild(audioEl);
              } else {
                // Fallback: Show audio indicator if data not found
                const indicator = document.createElement('span');
                indicator.textContent = `🔊 Audio ${index + 1}`;
                indicator.style.cssText = 'color: #888; font-size: 0.95em;';
                audioWrapper.appendChild(indicator);
              }
              
              audioContainer.appendChild(audioWrapper);
            });
            
            msgDiv.appendChild(audioContainer);
          }
          
          // Add text file previews if any
          if (textFiles.length > 0) {
            const textContainer = document.createElement('div');
            textContainer.className = 'user-text-file-previews';
            textContainer.style.cssText = 'margin-top: 10px; display: flex; flex-direction: column; gap: 8px;';
            
            textFiles.forEach((item, index) => {
              const textWrapper = document.createElement('div');
              textWrapper.style.cssText = 'display: flex; flex-direction: column; gap: 4px;';
              
              const fileName = item.name || `File ${index + 1}`;
              const fileContent = item.content || '';
              
              // Create view button
              const viewBtn = document.createElement('button');
              viewBtn.textContent = '📄 ' + fileName;
              viewBtn.style.cssText = 'background: #444; color: #ddd; border: 1px solid #666; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 0.9em; width: fit-content;';
              viewBtn.title = 'Click to view';
              
              // Create hidden content div
              const contentDiv = document.createElement('pre');
              contentDiv.style.cssText = 'display: none; margin: 0; padding: 10px; background: #2a2a2a; border: 1px solid #444; border-radius: 4px; max-height: 300px; overflow: auto; font-size: 0.85em; white-space: pre-wrap;';
              contentDiv.textContent = fileContent;
              
              viewBtn.onclick = () => {
                // Toggle content display
                if (contentDiv.style.display === 'none') {
                  contentDiv.style.display = 'block';
                  viewBtn.textContent = '📄 ' + fileName + ' ▼';
                } else {
                  contentDiv.style.display = 'none';
                  viewBtn.textContent = '📄 ' + fileName;
                }
              };
              
              textWrapper.appendChild(viewBtn);
              textWrapper.appendChild(contentDiv);
              textContainer.appendChild(textWrapper);
            });
            
            msgDiv.appendChild(textContainer);
          }
          
          row.appendChild(msgDiv);
          chatEl.appendChild(row);
        } else if (msg.role === 'assistant' && msg.tool_calls && msg.tool_calls.length > 0) {
          // Only show placeholder for the LAST assistant message with tool_calls
          if (msg === lastAssistantMsg && !msg.content) {
            const blk = addAssistantBlock(chatEl);
            showSection(blk.t);
            blk.t.innerHTML = `<div class="response-text"><em style="color: #888;">⚙️ Tool calls in progress...</em></div>`;
          }
          // Skip all other tool-call-only messages (they're intermediate steps)
        } else if (msg.role === 'assistant' && msg.content) {
          const blk = addAssistantBlock(chatEl);
          showSection(blk.t);
          // Use formatContent to detect HTML vs plain text
          blk.t.innerHTML = `<div class="response-text">${formatContent(msg.content, msg.content_format)}</div>`;
          if (msg.content_format === 'html' && typeof Prism !== 'undefined') {
            Prism.highlightAllUnder(blk.t);
          }
        }
      });
      
      // Session just loaded - always scroll to the latest content.
      scrollBottom(true);

      // Set current session ID for continuation
      currentSessionId = session.session_id;
      
      // Restore agent and LLM profile selectors (selector_module handles fallback to defaults)
      if (session.agent_name && window.selectorModule) {
        window.selectorModule.setAgent(session.agent_name);
      }
      if (session.llm_profile && window.selectorModule) {
        window.selectorModule.setLLMProfile(session.llm_profile);
      }
      
      // Update session ID in header
      if (typeof updateHeaderSessionId === 'function') {
        updateHeaderSessionId(session.session_id);
      }
    }
    
    // Handle read-only mode AFTER rendering messages
    if (readOnly) {
      showReadOnlyBanner(reason);
      disableInput();
    } else {
      removeReadOnlyBanner();
      enableInput();
    }
  });
  
  // Helper functions for read-only mode
  function showReadOnlyBanner(reason) {
    removeReadOnlyBanner(); // Remove existing banner if any
    
    const banner = document.createElement('div');
    banner.id = 'readOnlyBanner';
    banner.className = 'read-only-banner';
    banner.innerHTML = `
      <div class="banner-content">
        <span class="banner-icon">🔒</span>
        <div class="banner-text">
          <strong>Read-Only Session</strong>
          <p>${reason || 'This session cannot be edited.'}</p>
        </div>
      </div>
    `;
    
    const chatEl = document.getElementById('chat');
    if (chatEl) {
      // Insert as first child of chat element (not before it)
      chatEl.insertBefore(banner, chatEl.firstChild);
    }
  }
  
  function removeReadOnlyBanner() {
    const banner = document.getElementById('readOnlyBanner');
    if (banner) {
      banner.remove();
    }
  }
  
  function disableInput() {
    const taskInput = document.getElementById('task');
    const runBtn = document.getElementById('runBtn');
    const fileInput = document.getElementById('fileInput');
    const fileUploadBtn = document.querySelector('.file-upload-btn');
    
    if (taskInput) {
      taskInput.disabled = true;
      taskInput.placeholder = 'This session is read-only';
      taskInput.style.opacity = '0.5';
    }
    if (runBtn) {
      runBtn.disabled = true;
      runBtn.style.opacity = '0.5';
    }
    if (fileInput) {
      fileInput.disabled = true;
    }
    if (fileUploadBtn) {
      fileUploadBtn.style.opacity = '0.5';
      fileUploadBtn.style.pointerEvents = 'none';
    }
  }
  
  function enableInput() {
    const taskInput = document.getElementById('task');
    const runBtn = document.getElementById('runBtn');
    const fileInput = document.getElementById('fileInput');
    const fileUploadBtn = document.querySelector('.file-upload-btn');
    
    if (taskInput) {
      taskInput.disabled = false;
      taskInput.placeholder = 'Ask the agent…';
      taskInput.style.opacity = '1';
    }
    if (runBtn) {
      runBtn.disabled = false;
      runBtn.style.opacity = '1';
    }
    if (fileInput) {
      fileInput.disabled = false;
    }
    if (fileUploadBtn) {
      fileUploadBtn.style.opacity = '1';
      fileUploadBtn.style.pointerEvents = 'auto';
    }
  }

  // Public method to check if a request is active
  chatModule.hasActiveRequest = function() {
    // streamActive covers the normal fetch-based streams; the EventSource refs
    // cover the page-refresh reconnect path.
    return streamActive || currentEventSource !== null || currentStatusEventSource !== null;
  };

  // Export chatModule to window
  global.chatModule = chatModule;

})(window);

