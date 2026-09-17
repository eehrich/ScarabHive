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

  function kitIcon(name) {
    return `<svg class="pk-icon" aria-hidden="true"><use href="/static/kit/icons.svg#${name}"/></svg>`;
  }

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

  // The chat scrolls inside the shell's #chatScroll, not the page.
  function scroller() {
    return document.getElementById('chatScroll');
  }

  function isNearBottom() {
    const el = scroller();
    return el.scrollTop + el.clientHeight >= el.scrollHeight - NEAR_BOTTOM_THRESHOLD_PX;
  }

  // force=true: scroll regardless of current position (e.g. user just sent a
  // message, session just loaded - they expect to see the bottom).
  // force=false (default): only scroll if already near the bottom. This keeps
  // status events and streaming content from yanking the viewport away when
  // the user has scrolled up.
  function scrollBottom(force = false) {
    if (!force && !isNearBottom()) return;
    requestAnimationFrame(() => {
      const el = scroller();
      el.scrollTop = el.scrollHeight;
    });
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

  async function getJSON(url) {
    const resp = await fetch(url, { credentials: 'include' });
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
      headers: { 'Content-Type': 'application/json' },
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
   * A user message with something in it. Every stored user message went to
   * the agent -- one that opens with a command word was sent escaped ("//"),
   * so it is shown like any other. Mirrors chat._is_real_turn.
   */
  function isRealTurn(msg) {
    if (!msg || msg.role !== 'user') return false;
    return !!messageText(msg).trim();
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

  /**
   * `/rename <title>` -- the title the session list shows.
   *
   * Through the session pane, not with a PATCH of its own: the pencil in
   * that list does the same write, and the list and the header have to end
   * up in the same state whichever one did it.
   */
  async function cmdRename(container, payload) {
    const title = (payload || '').trim();
    if (!title) {
      addNote(container, 'Usage: /rename <title>');
      return;
    }
    if (!currentSessionId) {
      addNote(container, 'No session yet -- it is created with your first message, ' +
        'and can be named after that.');
      return;
    }
    if (!window.sessionManager || typeof window.sessionManager.renameTo !== 'function') {
      addNote(container, 'Renaming is not available in this window.');
      return;
    }
    const done = await window.sessionManager.renameTo(currentSessionId, title);
    addNote(container, done ? 'Title: ' + title : 'The session was not renamed.');
  }

  /**
   * `/agent [name]` -- what this chat talks to.
   *
   * The selector is the source: it holds the list the whole window agrees
   * on, and a command that set something else would disagree with the
   * dropdown next to it. The switch starts a new session, exactly as in the
   * terminal -- a session carries the agent that ran it, and continuing one
   * under another agent would run it with foreign tools and a foreign
   * prompt, with the next save writing that agent into its record.
   */
  async function cmdAgent(container, payload) {
    const selector = window.selectorModule;
    if (!selector || typeof selector.setAgent !== 'function') {
      addNote(container, 'The agent selector is not available in this window.');
      return;
    }
    const wanted = (payload || '').trim();
    const names = (selector.agents() || []).map(function (a) {
      return typeof a === 'string' ? a : (a && (a.name || a.id)) || '';
    }).filter(Boolean);
    const current = selector.getCurrentAgent();

    if (!wanted) {
      addNote(container, 'Agent: ' + (current || '?') + '\n' + (names.length
        ? names.map(function (n) {
            return ' ' + (n === current ? '*' : ' ') + ' ' + n;
          }).join('\n') + '\n  /agent <name> switches; the chat starts a new session for it.'
        : '  (no agents listed -- the selector could not read them)'));
      return;
    }
    if (wanted === current) {
      addNote(container, 'Already on ' + current + '.');
      return;
    }
    if (names.length && names.indexOf(wanted) === -1) {
      addNote(container, 'Unknown agent: ' + wanted + '\n  /agent lists them.');
      return;
    }
    if (!selector.setAgent(wanted)) {
      addNote(container, 'The selector did not take "' + wanted + '".');
      return;
    }
    // The new session AFTER the switch -- and it has to be CONFIRMED.
    // newConversation() returns nothing whether it started one or the viewer
    // kept a running request, and the session id is how it shows: a fresh one
    // has none until the first message. Claiming a new session that never
    // happened would leave the next message running the OLD session under the
    // NEW agent, and the save after it writes that agent into its record --
    // exactly the foreign tools and foreign prompt this switch avoids.
    const before = currentSessionId;
    await window.sessionManager.newConversation();
    if (currentSessionId === before && before !== null) {
      addNote(container, 'Agent: ' + wanted +
        '   -- but the session stayed: the next message would run ' + before +
        ' under ' + wanted + '. /new once the running request is done.');
      return;
    }
    addNote(container, 'Agent: ' + wanted + '   (new session)');
  }

  /**
   * `/undo` and `/retry` -- the last question and everything that answered it.
   *
   * The server cuts, because the conversation the browser shows is the
   * RECORD: the agent's copy in memory is shortened with it, or the next
   * save puts the dropped turn straight back. Reloading afterwards is not
   * cosmetic -- the messages on screen are what the viewer would otherwise
   * keep reading as still being there.
   */
  async function cmdUndo(container, payload, retry) {
    if (!currentSessionId) {
      addNote(container, 'Nothing to take back -- this chat has no session yet.');
      return;
    }
    // "force" the way agent-cli's --force means it: for a lock a crashed
    // process left behind. The server refuses a session that is running, and
    // without this there would be no way past a leftover.
    const forced = (payload || '').trim().toLowerCase() === 'force';
    let answer;
    try {
      answer = await postJSON('/chat/undo' + (forced ? '?force=true' : ''), {
        session_id: currentSessionId,
        // The session's own agent wins on the server; this is the fallback
        // for one that has no record yet.
        agent_name: currentAgentName() || null,
      });
    } catch (e) {
      if (e && e.status === 409 && !forced) {
        addNote(container, e.message +
          '\n  /undo force takes it anyway -- for a lock a crashed process left behind.');
        return;
      }
      throw e;
    }
    if (!answer.dropped) {
      addNote(container, 'Nothing to take back in this session yet.');
      return;
    }
    await window.sessionManager.loadSession(currentSessionId);
    const asked = answer.dropped.text || '';
    addNote(container, 'Dropped: ' + oneLine(asked, 70));
    if (!retry) return;
    // The text goes back into the input rather than being sent: a file that
    // came with it lives on the viewer's disk, and only they can attach it
    // again. Sending silently without it would ask a different question.
    if (taskInputEl) {
      taskInputEl.value = asked;
      // The input event is what the textarea grows on and what turns the
      // action button back into Send -- setting .value alone leaves a box
      // that looks empty and a button that still says Stop.
      taskInputEl.dispatchEvent(new Event('input', { bubbles: true }));
      taskInputEl.focus();
    }
    addNote(container, answer.dropped.had_attachments
      ? 'Ask it again with Enter -- the file it carried has to be attached again.'
      : 'Ask it again with Enter.');
  }

  /**
   * `/export [ignored]` -- the conversation as markdown, saved by the browser.
   *
   * No path argument: the terminal writes on the machine it runs on, and a
   * browser cannot. What it gets is the same markdown the terminal writes,
   * rendered by the server from the record.
   */
  async function cmdExport(container, payload) {
    if (!currentSessionId) {
      addNote(container, 'Nothing to export -- this chat has no session yet.');
      return;
    }
    if ((payload || '').trim()) {
      addNote(container, 'A path only means something in the terminal -- ' +
        'the browser saves it where downloads go.');
    }
    const url = '/chat/transcript?session_id=' + encodeURIComponent(currentSessionId);
    const resp = await fetch(url, { credentials: 'include' });
    if (!resp.ok) {
      let detail = resp.status + ' ' + resp.statusText;
      try {
        const body = await resp.json();
        if (body && body.detail) detail = body.detail;
      } catch (e) { /* not JSON -- keep the status line */ }
      addNote(container, 'Could not export: ' + detail);
      return;
    }
    const blob = await resp.blob();
    // Tracked like a preview: this one is revoked a tick from now, but an
    // object URL that nobody can reach holds its whole blob until the tab
    // closes -- and a download cancelled mid-flight is exactly the path that
    // skips the revoke below.
    const href = trackedObjectUrl(blob);
    const link = document.createElement('a');
    link.href = href;
    link.download = 'chat-' + currentSessionId + '.md';
    document.body.appendChild(link);
    link.click();
    link.remove();
    // Freed on the next tick: revoking it while the click is still being
    // handled cancels the download in Firefox.
    setTimeout(function () { URL.revokeObjectURL(href); }, 0);
    addNote(container, 'Written: chat-' + currentSessionId + '.md');
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
    // Ask first: loadSession answers an unknown id with an error toast, which
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
        if (!text) return;
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
      // the New button: a running request is cancelled first
      await window.sessionManager.newConversation();
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
      rename: function () { return cmdRename(container, payload); },
      agent: function () { return cmdAgent(container, payload); },
      vars: function () { return cmdVars(container, payload); },
      tools: function () { return cmdTools(container, payload); },
      costs: function () { return cmdCosts(container); },
      history: function () { return cmdHistory(container, payload); },
      last: function () { return cmdLast(container); },
      undo: function () { return cmdUndo(container, payload, false); },
      retry: function () { return cmdUndo(container, payload, true); },
      export: function () { return cmdExport(container, payload); },
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

  /** A part the session kept without its data: named, not shown. */
  function missingAttachment(iconMarkup, name) {
    const placeholder = document.createElement('span');
    placeholder.className = 'pk-badge';
    placeholder.innerHTML = `${iconMarkup} ${escapeHtml(name)}`;
    return placeholder;
  }

  /**
   * Image, audio and text-file attachments under a user message -- the same
   * markup for a message just sent (object URLs) and one restored from the
   * session (data URLs). Images open full size through openAttachmentInNewTab.
   */
  function renderAttachments(msgDiv, { images = [], audio = [], textFiles = [] }) {
    if (images.length) {
      const box = document.createElement('div');
      box.className = 'user-image-previews';
      images.forEach(({ url, name }) => {
        if (!url) {
          box.appendChild(missingAttachment(kitIcon('image'), name || 'Image'));
          return;
        }
        const img = document.createElement('img');
        img.src = url;
        img.alt = name || 'Attached image';
        img.title = name || 'Open full size';
        img.onclick = () => openAttachmentInNewTab(url);
        box.appendChild(img);
      });
      msgDiv.appendChild(box);
    }
    if (audio.length) {
      const box = document.createElement('div');
      box.className = 'user-audio-previews';
      audio.forEach(({ url, name }) => {
        if (!url) {
          box.appendChild(missingAttachment(kitIcon('music'), name));
          return;
        }
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'pk-btn pk-btn--sm attachment-toggle';
        const player = document.createElement('audio');
        player.src = url;
        const label = (playing) => {
          button.innerHTML = `${kitIcon(playing ? 'square' : 'play')} ${escapeHtml(name)}`;
        };
        label(false);
        button.onclick = () => {
          if (player.paused) player.play(); else player.pause();
        };
        player.onplay = () => label(true);
        player.onpause = () => label(false);
        player.onended = () => label(false);
        box.appendChild(button);
        box.appendChild(player);
      });
      msgDiv.appendChild(box);
    }
    if (textFiles.length) {
      const box = document.createElement('div');
      box.className = 'user-text-file-previews';
      textFiles.forEach(({ name, file, content }) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'pk-btn pk-btn--sm attachment-toggle';
        button.innerHTML = `${kitIcon('file-text')} ${escapeHtml(name)}`;
        button.setAttribute('aria-expanded', 'false');
        const pre = document.createElement('pre');
        pre.className = 'pk-code attachment-text';
        pre.hidden = true;
        if (file) {
          const reader = new FileReader();
          reader.onload = (e) => { pre.textContent = e.target.result; };
          reader.readAsText(file);
        } else {
          pre.textContent = content || '';
        }
        button.onclick = () => {
          pre.hidden = !pre.hidden;
          button.setAttribute('aria-expanded', String(!pre.hidden));
        };
        box.appendChild(button);
        box.appendChild(pre);
      });
      msgDiv.appendChild(box);
    }
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
    renderAttachments(msgDiv, {
      images: (images || []).map((file) => ({ url: trackedObjectUrl(file), name: file.name })),
      audio: (audioFiles || []).map((file) => ({ url: trackedObjectUrl(file), name: file.name })),
      textFiles: (textFiles || []).map((file) => ({ name: file.name, file })),
    });

    row.appendChild(msgDiv);
    chatContainer.appendChild(row);
    // User just sent a message - always scroll so they see what they sent.
    scrollBottom(true);
    return row;
  }

  function addAssistantBlock(chatContainer) {
    const row = document.createElement('div');
    row.className = 'row';
    const box = document.createElement('div');
    box.className = 'msg assistant';
    box.style.position = 'relative'; // Enable absolute positioning for request ID
    box.innerHTML = `
      <div class="container-section" style="display: none;">
        <div class="container-header" data-toggle="thinking">
          <span class="toggle-arrow">${kitIcon('chevron-right')}</span>
          <span class="type-icon">${kitIcon('brain')}</span>
          <span class="container-label">Thinking</span>
        </div>
        <div class="container-body" id="thinking" style="display: none;">
          <pre id="thinkingContent"></pre>
        </div>
      </div>
      <div class="container-section" style="display: none;">
        <div class="container-header" data-toggle="status">
          <span class="toggle-arrow">${kitIcon('chevron-right')}</span>
          <span class="type-icon">${kitIcon('activity')}</span>
          <span class="container-label">Status</span>
        </div>
        <div class="container-body" id="statusBody" style="display: block;"></div>
      </div>
      <div class="container-section" style="display: none;">
        <div class="container-header" data-toggle="response">
          <span class="toggle-arrow">${kitIcon('chevron-right')}</span>
          <span class="type-icon">${kitIcon('message-square')}</span>
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
      // data-open drives the arrow (chat.css); the body's display stays the state
      header.parentElement.dataset.open = String(body.style.display !== 'none');
      header.addEventListener('click', () => {
        const isHidden = body.style.display === 'none';
        body.style.display = isHidden ? 'block' : 'none';
        header.parentElement.dataset.open = String(isHidden);
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

  // The EventSource of a run the chat follows again after a reload (followRun)
  let currentEventSource = null;
  // The fetch stream (POST /events or POST /run) the chat follows: one object per stream, null when none. Together
  // with currentEventSource it gates hasActiveRequest(). A stream the chat lets go of (letGoOfFinishedRun) reads on
  // to its end with its events ignored: closing it would cut the end of its request short (a run with files saves
  // there, a message's request saves once more and releases the run). `ended`: it was read to its end.
  let followedStream = null;
  // Block object the live stream consumer renders into (same object identity
  // as the blk passed to handleSSEEvent). Mid-run appends rebind its fields to
  // a fresh block so the agent's reaction renders below the injected message.
  let activeStreamBlk = null;

  // While init checks whether a run of this tab is still going, the composer is held: a
  // message would start a second run beside it (see followRun).
  let holding = false;
  // The session shown is a sub-agent's the selector cannot pick: the composer stays off.
  let readOnlyShown = false;

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
  // The message box, kept like the three above: /retry writes the question
  // back into it, and looking it up by id a second time is how one of the two
  // spellings goes stale without anything saying so.
  let taskInputEl = null;

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
  
  // The run the chat follows, or followed last -- before the first, one that never started. `requestId` and
  // `sessionId` once its start (after a reload: its reconnect) has named them; `over` once its own stream said it is
  // over (final, end, cancelled): it answers no more messages, and a connection that breaks afterwards is no lost
  // connection, though the run may still be saving its session. Nothing else says so -- a cancelled run ends in its
  // own time. Each run has its own: what a question about one learns never comes from another.
  let run = { requestId: null, sessionId: null, over: false };
  // The session the next message continues. The shell's session manager puts
  // one here (session:loaded / session:new) -- after a reload too, once it
  // has restored it; a run reattached after a reload continues the session it
  // was stored with, and a stream's start or reconnect event names its own.
  let currentSessionId = null;

  // The run of this tab a reload follows again: its request and the session it runs in.
  // sessionStorage, not localStorage, so each tab has its own.
  const RUN_KEY = 'activeRequestId';
  const RUN_SESSION_KEY = 'activeRequestSession';

  function storeRun(requestId, sessionId) {
    sessionStorage.setItem(RUN_KEY, requestId);
    sessionStorage.setItem(RUN_SESSION_KEY, sessionId);
  }

  function storedRun() {
    const requestId = sessionStorage.getItem(RUN_KEY);
    return requestId && { requestId, sessionId: sessionStorage.getItem(RUN_SESSION_KEY) };
  }

  function clearStoredRun() {
    const stored = storedRun();
    if (stored) unmarkStopping(stored.requestId);
    sessionStorage.removeItem(RUN_KEY);
    sessionStorage.removeItem(RUN_SESSION_KEY);
  }

  // A run the chat has asked the server to end -- by Stop, or by leaving its session. It ends in its own
  // time and takes no more messages, which it would save unanswered; an ask whose answer failed or went
  // missing may have been taken all the same. Kept per request for a reload of the tab, until the run
  // is forgotten.
  const STOPPING_KEY_PREFIX = 'stoppingRequest:';

  function markStopping(requestId) {
    sessionStorage.setItem(STOPPING_KEY_PREFIX + requestId, '1');
  }

  function isStopping(requestId) {
    return Boolean(requestId) && sessionStorage.getItem(STOPPING_KEY_PREFIX + requestId) !== null;
  }

  function unmarkStopping(requestId) {
    sessionStorage.removeItem(STOPPING_KEY_PREFIX + requestId);
  }

  // A reload no longer follows the chat's run. Only that run is forgotten: a run the server refused
  // never got an id, and a run with files is never stored -- the run stored for a reload is another one.
  function forgetRun() {
    if (!run.requestId) return;
    if (storedRun()?.requestId === run.requestId) clearStoredRun();
    unmarkStopping(run.requestId);
  }

  // The controls go back to idle: a message starts a run, and Stop is reset for it.
  function idleControls() {
    runActive = false; updateActionButton();
    stopBtn.setAttribute('title', 'Stop');
    stopBtn.setAttribute('aria-label', 'Stop');
    stopBtn.disabled = false;
    stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
  }

  // The chat lets go of its run: the controls go back to idle, and unless `keep` a reload
  // no longer follows it.
  function endRun(keep = false) {
    idleControls();
    if (!keep) forgetRun();
  }

  /**
   * Ask the server to cancel a run: true when it has cancelled it or no longer runs it. A cancelled run
   * still ends in its own time -- its stream tells when.
   */
  async function cancelRun(requestId, { force }) {
    const response = await fetch(`/api/requests/${encodeURIComponent(requestId)}/cancel${force ? '?force=true' : ''}`,
      { method: 'POST', signal: AbortSignal.timeout(60000) });
    const { status } = await response.json();
    return status === 'cancelled' || status === 'not_found';
  }

  // A session takes the chat from a run past its answer or its cancel: the chat follows its stream no more -- the
  // stream only waits for the run's save and session-end hooks, and would hold the messages of the session now shown
  // until then. It reads on to its end, its events ignored.
  function letGoOfFinishedRun() {
    if (!chatModule.hasActiveRequest() || !run.over) return;
    followedStream = null;
    currentEventSource = null;
    endRun();
  }

  // A lasting row about the run's connection in the block's status (a status without a
  // phase renders nothing; the synthetic request_id keeps it off the run's own row).
  function connectionNotice(blk, message) {
    addStatusEvent(blk.status, {
      type: 'status',
      phase: 'error',
      message,
      request_id: `${run.requestId}_connection`,
      timestamp: new Date().toISOString()
    });
  }

  const LOST = 'Connection lost -- the run may still be going; reload the page to follow it.';

  function updateRequestId() {
    if (!run.requestId) return;
    const latestAssistant = document.querySelector('.chat .row:last-child .msg.assistant');
    if (!latestAssistant) return;
    let element = latestAssistant.querySelector('.message-request-id');
    if (!element) {
      element = document.createElement('div');
      element.className = 'message-request-id';
      element.append('Request: ', document.createElement('span'));
      latestAssistant.appendChild(element);
    }
    element.querySelector('span').textContent = run.requestId;
    element.title = `Request ID: ${run.requestId} -- click to open it in a panel`;
  }

  // Shared SSE event handler for both EventSource and manual fetch() parsing
  // Module-level so it can be used by both normal requests and a run reattached after a reload
  function handleSSEEvent(data, blk) {
    switch (data.type) {
      case 'start':
        run.requestId = data.request_id;
        run.sessionId = data.session_id;
        currentSessionId = data.session_id;

        // Notify session manager about new/updated session
        if (window.sessionManager && typeof window.sessionManager.onSessionUpdated === 'function') {
          window.sessionManager.onSessionUpdated(currentSessionId);
        }
        
        updateRequestId();
        break;
      case 'reconnect':
        // Reconnected to existing running job (after browser refresh)
        run.requestId = data.request_id;
        run.sessionId = data.session_id;
        currentSessionId = data.session_id;

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
        
        updateRequestId();

        // Show reconnect info in response area
        showSection(blk.t);
        // Both escaped: last_status is a plugin's status line and carries
        // tool arguments the model chose ("Searching: <query>").
        blk.t.innerHTML = `<div class="response-text reconnect-info">${escapeHtml(data.message)}${data.last_status ? '<br><em>Last status: ' + escapeHtml(data.last_status) + '</em>' : ''}</div>`;
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
            blk.think.textContent += `Step ${data.step}: ${data.assistant.content}\n\n`;
          }
          if (data.assistant.tool_calls && data.assistant.tool_calls.length > 0) {
            blk.think.textContent += `Step ${data.step}: planning ${data.assistant.tool_calls.length} tool call(s):\n`;
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
        // e.g., if the run's request id is "abc123", also show "abc123_sub_001", "abc123_001_sub_002", etc.
        if (blk && blk.status) {
          const eventRequestId = data.request_id || '';
          // Check if this event belongs to current request hierarchy
          // Either exact match OR starts with current request_id followed by underscore (child operation)
          const matches = eventRequestId === run.requestId ||
              (eventRequestId && run.requestId && eventRequestId.startsWith(run.requestId + '_'));
          
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
            const matches = eventRequestId === run.requestId ||
                (eventRequestId && run.requestId && eventRequestId.startsWith(run.requestId + '_'));
            
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
          contMsg.innerHTML = `<div class="continuation-badge">${kitIcon('rotate-ccw')} Auto-continue #${escapeHtml(String(data.count || '?'))}</div><div class="continuation-reason">${escapeHtml(data.reason || '')}</div><div class="continuation-text">${formatTextWithLineBreaks(data.message || '')}</div>`;
          contRow.appendChild(contMsg);
          chatContainer.appendChild(contRow);
          // Create a new assistant block for the next response and update blk
          // in-place; this supersedes any deferred mid-run-append rebind.
          pendingAppendRebind = false;
          rebindLiveBlock(blk);
        }
        break;
      case 'final':
        run.over = true;
        if (pendingAppendRebind) {
          // Edge (e.g. max-steps): the run finalizes without another step. The
          // final would be suppressed against the old block's non-empty content
          // — render it into a fresh block below the injected message instead.
          pendingAppendRebind = false;
          rebindLiveBlock(blk);
        }
        // Its answer is here: nothing is left to stop -- a cancel would take its session-end hooks and background
        // sub-agents along -- and a reload shows the answer from the session the run saves, following the run no more.
        idleControls();
        forgetRun();

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
        run.over = true;
        pendingAppendRebind = false;
        // a reload no longer follows it -- a refused run's end leaves another one's alone
        forgetRun();

        // Close EventSource immediately to prevent auto-reconnect attempts
        // EventSource will try to reconnect if the server closes the connection,
        // which causes spurious "Connection failed" errors in the onerror handler
        if (currentEventSource) {
          currentEventSource.close();
          currentEventSource = null;
        }

        idleControls();

        // Reload sessions after conversation completes
        if (window.sessionManager && typeof window.sessionManager.loadSessions === 'function') {
          window.sessionManager.loadSessions();
        }
        break;
      case 'error':
        // Shown only: a run's error is followed by its end, the server's refusals (a busy session,
        // a request id in use) close the stream -- either one ends the run in the chat.
        showSection(blk.t);
        blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks(data.message || data.error)}</div>`;
        break;
      case 'cancelled':
        // Request was cancelled - clean up and reset UI
        console.log('Request cancelled:', data.request_id, 'at step', data.step);
        run.over = true;
        // a reload no longer follows it; its stream stays open -- the run saves its session before its end
        forgetRun();
        // Show cancelled status with step number
        showSection(blk.t);
        const stepInfo = data.step ? ` at step ${data.step}` : '';
        blk.t.innerHTML = `<div class="response-text" style="opacity: 0.6;">Request cancelled${stepInfo}</div>`;
        idleControls();
        break;
    }
    scrollBottom();
  }

  // Public init function that wires the chat form behavior
  chatModule.init = function () {
    const chatForm = document.getElementById('f');
    const taskInput = taskInputEl = document.getElementById('task');
    runBtn = document.getElementById('runBtn');
    stopBtn = document.getElementById('stopBtn');
    chatContainer = document.getElementById('chat');
    
    // Expose current session id for other modules (fallback for UI)
    chatModule.getCurrentSessionId = function() { return currentSessionId; };

    if (!chatForm || !taskInput || !runBtn || !stopBtn || !chatContainer) {
      console.warn('Chat form elements not found');
      return;
    }

    // Slash commands + skills: catalogue and parsing come from the server, so
    // the browser offers exactly what the terminal offers.
    if (window.slashCommands) {
      window.slashCommands.attach(taskInput);
    }

    // While a run is active the action button is Stop — but as soon as the user
    // types something it becomes Send, so the text can be injected into the
    // running agent. Clearing the input flips it back to Stop.
    taskInput.addEventListener('input', updateActionButton);
    updateActionButton();

    // Stop asks the server to cancel the run; the run's stream brings its end, as it would without.
    stopBtn.addEventListener('click', async function() {
      // the run clicked on: an answer that comes after another run has taken the chat leaves that one alone
      const clicked = run;
      const requestId = clicked.requestId;
      if (!requestId) return;
      markStopping(requestId);
      // immediate feedback: the icon stays, label and tooltip change
      stopBtn.setAttribute('title', 'Canceling');
      stopBtn.setAttribute('aria-label', 'Canceling');
      stopBtn.disabled = true;
      stopBtn.classList.add('cancelling');

      let over = false;
      try {
        over = await cancelRun(requestId, { force: false });
      } catch (error) {
        console.error('Failed to cancel request:', error);
      }
      if (run !== clicked || !chatModule.hasActiveRequest()) return;  // the run has ended meanwhile, and its controls with it
      const outcome = over ? 'Done' : 'Failed';
      stopBtn.setAttribute('title', outcome);
      stopBtn.setAttribute('aria-label', outcome);
      stopBtn.classList.remove('cancelling');
      stopBtn.classList.add(over ? 'cancelled' : 'cancel-failed');
      // Stop again after a moment, while the run's stream is still open
      setTimeout(() => {
        if (run !== clicked || !chatModule.hasActiveRequest()) return;
        stopBtn.setAttribute('title', 'Stop');
        stopBtn.setAttribute('aria-label', 'Stop');
        stopBtn.disabled = false;
        stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
      }, 2000);
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
    let submissions = 0;

    chatForm.addEventListener('submit', async function(e) {
      e.preventDefault();
      if (submitting) return;
      submitting = true;
      const submission = ++submissions;
      try {
        await handleSubmit();
      } finally {
        // safety net; the handler releases it far earlier -- and a submission started since holds its own
        if (submission === submissions) submitting = false;
      }
    });

    async function handleSubmit() {
      const written = taskInput.value;
      let task = written.trim();

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
          // Awaited, so the command's note is there when the submit is done. The guard
          // goes with the input: a command can wait long (/new asks about a running
          // request and stops it), and a message meanwhile must get its answer.
          clearInput(taskInput);
          submitting = false;
          await runChatCommand(resolved.name, resolved.payload);
          return;
        }
        if (resolved.kind === 'plugin') {
          clearInput(taskInput);
          submitting = false;
          await runPluginCommand(resolved.name, resolved.payload);
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

      // The session is being deleted -- shown again by a load that was on its way: the server keeps no run of it.
      if (window.sessionManager.isBeingDeleted(currentSessionId)) {
        addNote(chatContainer, 'The session is being deleted -- the message stays here.');
        return;
      }

      // A running request takes text only, and only once its start has named it: the
      // server refuses a second run in its session, and that refusal would end the
      // running one's stream in this chat. A run being stopped, or past its answer or
      // its cancel, takes nothing: it would save the message unanswered.
      if (chatModule.hasActiveRequest() && (hasFiles || !run.requestId || run.over || isStopping(run.requestId))) {
        addNote(chatContainer, hasFiles
          ? 'Files can be sent once the running request has finished -- they stay attached.'
          : !run.requestId
            ? 'The request is still starting -- the message stays here until it runs.'
            : run.over
              ? 'The request is finishing -- the message stays here; send it once it has.'
              : 'The request is being stopped -- the message stays here; send it once it has.');
        // still written into this session: a pick, New or a delete waiting for the run leaves it where it is
        window.sessionManager.messageWritten(currentSessionId);
        return;
      }

      // Add user message to chat. For a skill it is what the user TYPED --
      // pasting the expanded body back at them would bury the conversation.
      let displayText = typed || task || '';
      // Get file breakdown by type from file upload module
      const filesByType = window.fileUploadModule ? window.fileUploadModule.getFilesByType() : { images: [], audio: [], text: [] };
      
      // Pass images, audio and text files separately to addUser
      const sent = addUser(chatContainer, displayText, filesByType.images, filesByType.audio, filesByType.text);
      // The message belongs to this chat's session: a session still loading must not take its
      // place, and a delete waiting for the session's run keeps it.
      window.sessionManager.messageWritten(currentSessionId);
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

      // If there's an active request, append the user message to it (a fetch stream, or
      // a run reattached after a reload: hasActiveRequest covers both) -- its start has named it,
      // or the message would have been held above
      if (chatModule.hasActiveRequest()) {
        const requestId = run.requestId;
        let status = 0;  // no answer at all
        try {
          // fallback=none: a run that has just finished answers 404 instead of the
          // message being parked unanswered in its session
          const resp = await fetch(`/events/${encodeURIComponent(requestId)}/append?fallback=none`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: task })
          });
          status = resp.status;
        } catch (err) {
          console.error('Failed to append to active request:', err);
        }

        if (status < 200 || status >= 300) {
          // Not confirmed: the server did not take it, or did not answer. The message goes back
          // rather than being lost -- a run still going would refuse a second run in its session,
          // and one that has just finished leaves starting the next to the person.
          sent.remove();
          // after anything typed, or given back, meanwhile
          taskInput.value = [taskInput.value, written].filter((text) => text.trim()).join('\n\n');
          taskInput.dispatchEvent(new Event('input', { bubbles: true }));
          addNote(chatContainer, status === 404
            ? 'The run had just finished -- the message is back in the input; send it again to start a new run.'
            : status
              ? `The message did not reach the running request (HTTP ${status}) -- it is back in the input.`
              : 'No answer from the server -- the message may not have reached the running request; it is back in the input.');
          return;
        }

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
            request_id: `${requestId}_user_append_${Date.now()}`,
            timestamp: new Date().toISOString()
          });
          scrollBottom();
        }
        // back to Stop for the emptied input -- unless the run ended while the append was on its way
        updateActionButton();
        stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
        stopBtn.disabled = false;
        stopBtn.setAttribute('title', 'Stop');
        stopBtn.setAttribute('aria-label', 'Stop');
        return;
      }

      // No active request: start a new request
      const blk = addAssistantBlock(chatContainer);
      activeStreamBlk = blk;
      runActive = true; updateActionButton();  // -> Stop (empty input)
      stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
      stopBtn.disabled = false;
      stopBtn.setAttribute('title', 'Stop');
      stopBtn.setAttribute('aria-label', 'Stop');
      run = { requestId: null, sessionId: null, over: false };  // named by its start event
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
        if (currentSessionId) {
          formData.append('session_id', currentSessionId);
        }

        const stream = {};
        try {
          followedStream = stream;

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
            return;
          }

          // Response is SSE stream - parse it manually
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = '';
          let sseOk = false;

          while (true) {
            const {done, value} = await reader.read();
            if (done) {
              stream.ended = true;
              break;
            }
            if (followedStream !== stream) continue;  // let go of: read on to its end, and nothing more

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
                  // the server took the message: its files are sent (a refusal leaves them attached, and
                  // files attached since stay)
                  if (ev.type === 'start') window.fileUploadModule.removeFiles(files);
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
        } catch (err) {
          // a connection that breaks after the run's answer takes nothing from it
          if (followedStream === stream && !run.over) {
            showSection(blk.t);
            blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks('Request failed: ' + String(err))}</div>`;
          }
        } finally {
          // The run ran inline in this request, not as a job a reload could follow: it was never stored,
          // and the run stored for a reload is another one.
          if (followedStream === stream) {
            followedStream = null;
            endRun();
          } else if (stream.ended) {
            window.sessionManager.loadSessions();  // let go of and read to its end: the run has saved its session
          }
        }
        return;
      }

      // Text-only SSE-based request via POST fetch (avoids URL length limits)

      // Get current agent and LLM profile selections
      const selectedAgent = window.selectorModule && window.selectorModule.getCurrentAgent ? window.selectorModule.getCurrentAgent() : null;
      const selectedLLMProfile = window.selectorModule && window.selectorModule.getCurrentLLMProfile ? window.selectorModule.getCurrentLLMProfile() : null;
      
      // Build POST body
      const postBody = { task: task };
      if (currentSessionId) postBody.session_id = currentSessionId;
      if (selectedAgent) postBody.agent_name = selectedAgent;
      if (selectedLLMProfile) postBody.llm_profile = selectedLLMProfile;

      let lost = false;  // the connection broke before the run's end
      const stream = {};
      try {
        followedStream = stream;
        const response = await fetch('/events', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(postBody)
        });

        if (!response.ok) {
          const errorText = await response.text();
          let errorMsg = 'Request failed';
          try {
            const errorJson = JSON.parse(errorText);
            if (errorJson.status_code === 401 || response.status === 401) {
              errorMsg = (errorJson.detail || 'Authentication required') + ' -- please log in';
            } else if (errorJson.status_code === 403 || response.status === 403) {
              errorMsg = errorJson.detail || 'Access denied';
            } else {
              errorMsg = errorJson.detail || errorJson.error || errorMsg;
            }
          } catch (e) {
            errorMsg = errorText || errorMsg;
          }
          showSection(blk.t);
          blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks(errorMsg)}</div>`;
          return;
        }

        // Parse SSE stream manually (same approach as file upload path)
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const {done, value} = await reader.read();
          if (done) {
            stream.ended = true;
            break;
          }
          if (followedStream !== stream) continue;  // let go of: read on to its end, and nothing more

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
                // runs as a background job: a reload of this tab follows it again
                if (ev.type === 'start') storeRun(run.requestId, run.sessionId);
              } catch (e) {
                console.error('Failed to parse SSE data:', e);
              }
            }
          }
        }

      } catch (err) {
        if (followedStream !== stream) return;  // let go of before its connection broke
        // The connection was lost mid-run: the run may still be going, and a reload follows it.
        if (run.requestId && !run.over) {
          connectionNotice(blk, LOST);
          lost = true;
          return;
        }

        // No run to follow
        if (!run.over) {
          const errorMessage = 'Connection failed - server may be unreachable';
          showSection(blk.t);
          const currentContent = blk.t.textContent || '';
          if (!currentContent.trim()) {
            blk.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks(errorMessage)}</div>`;
          } else {
            const errorNotice = document.createElement('div');
            errorNotice.className = 'response-text error';
            errorNotice.innerHTML = formatTextWithLineBreaks('\n\n' + errorMessage);
            blk.t.appendChild(errorNotice);
          }
        }
      } finally {
        // The run is over for this chat; a lost one stays stored for a reload to follow.
        if (followedStream === stream) {
          followedStream = null;
          endRun(lost);
        } else if (stream.ended) {
          window.sessionManager.loadSessions();  // let go of and read to its end: the run has saved its session
        }
      }
    }

    /**
     * After a reload a run of this tab may still be going: follow it. The status is checked
     * first -- GET /events with an id the server no longer holds would start a new, empty run.
     * The composer is held meanwhile, so no message starts a second run beside it.
     */
    async function followRun() {
      const stored = storedRun();
      if (!stored) return;
      holding = true;
      updateComposer();
      try {
        let status = null;
        try {
          const response = await fetch(`/api/requests/${encodeURIComponent(stored.requestId)}/status`,
            { signal: AbortSignal.timeout(10000) });
          if (response.ok) status = await response.json();
        } catch (error) {
          console.warn('[chat_module] Failed to check active job status:', error);
        }
        if (!status) return;  // the server could not be asked: the run stays stored, and a later reload asks again
        if (status.status !== 'running') {
          clearStoredRun();
          return;
        }
        attachRun(stored);
      } finally {
        holding = false;
        updateComposer();
      }
    }

    function attachRun({ requestId, sessionId: session }) {
      // The live run takes the chat -- over a session picked meanwhile (a read-only one too:
      // the run's session takes messages). A chat that shows that session keeps it.
      if (currentSessionId !== session) {
        chatContainer.innerHTML = '';
        releasePreviewObjectUrls();
        readOnlyShown = false;
      }
      // stored again: a pick or New while the reload checked for the run has let it go
      storeRun(requestId, session);
      const blk = addAssistantBlock(chatContainer);
      activeStreamBlk = blk;
      runActive = true; updateActionButton();
      stopBtn.disabled = false;
      stopBtn.setAttribute('title', 'Stop');
      stopBtn.setAttribute('aria-label', 'Stop');
      stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');

      // The chat continues the run's session and tells the session manager,
      // so no pick or restore takes its place.
      window.sessionManager.setCurrentSession(session);
      currentSessionId = session;
      run = { requestId, sessionId: session, over: false };
      // Only request_id: the backend uses the job's agent. No token in the URL either:
      // the server does not accept one, and the access_token cookie goes along.
      const es = new EventSource(`/events?task=&request_id=${encodeURIComponent(requestId)}&session_id=${encodeURIComponent(session || '')}`,
        { withCredentials: true });
      currentEventSource = es;

      es.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data);
          if (currentEventSource !== es) {
            // let go of: read on to its end, and close it then rather than have it reconnect -- the run has
            // saved its session by then
            if (data.type === 'end') {
              es.close();
              window.sessionManager.loadSessions();
            }
            return;
          }
          handleSSEEvent(data, blk);
        } catch (err) {
          console.warn('[chat_module] SSE reconnect parse error:', err);
        }
      };

      // A stream that stops before the run's end -- closed, broken or refused, which EventSource does
      // not tell apart -- may leave the run going: a reload asks the server again.
      es.onerror = () => {
        es.close();
        if (currentEventSource !== es) return;  // let go of already
        currentEventSource = null;
        if (run.over) {
          endRun();
        } else {
          connectionNotice(blk, LOST);
          endRun(true);
        }
      };
    }

    return followRun();
  };
  
  function showSection(element) {
    const section = element.closest('.container-section');
    if (section && section.style.display === 'none') {
      section.style.display = 'block';
    }
  }

  // Expose functions for testing
  chatModule.addStatusEvent = addStatusEvent;
  chatModule.toggleTreeNode = toggleTreeNode;
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
  // No cleanup on beforeunload: the browser ends the streams of a page that unloads, and a page that stays -- a
  // link that turns into a download -- follows its run on.

  // Listen for new conversation events
  window.addEventListener('session:new', (event) => {
    letGoOfFinishedRun();
    currentSessionId = null;
    const chatEl = document.getElementById('chat');
    chatEl.innerHTML = '';
    releasePreviewObjectUrls();
    // not chosen, but all that is left when the stored session cannot be shown: a lost run of it stays
    if (event.detail.chosen) leaveLostRun(null);
    readOnlyShown = false;
    updateComposer();
  });

  // Listen for session load events
  window.addEventListener('session:loaded', (event) => {
    const { session, readOnly, reason } = event.detail;

    // A run the chat follows keeps the chat until its answer or its cancel: a load would overwrite its live output.
    // Past them a session picked meanwhile takes the chat.
    if (chatModule.activeRun()) {
      console.warn('[session:loaded] Ignoring session load - a run is still streaming');
      return;
    }
    letGoOfFinishedRun();
    leaveLostRun(session.session_id);

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
          
          renderAttachments(msgDiv, {
            images: images.map((item) => ({ url: item.image_url?.url || item.url, name: item.name })),
            audio: audioFiles.map((item, index) => ({
              url: item.audio_url || item.audio?.data || item.data,
              name: item.name || `Audio ${index + 1}`,
            })),
            textFiles: textFiles.map((item, index) => ({ name: item.name || `File ${index + 1}`, content: item.content })),
          });

          row.appendChild(msgDiv);
          chatEl.appendChild(row);
        } else if (msg.role === 'assistant' && msg.tool_calls && msg.tool_calls.length > 0) {
          // Only show placeholder for the LAST assistant message with tool_calls
          if (msg === lastAssistantMsg && !msg.content) {
            const blk = addAssistantBlock(chatEl);
            showSection(blk.t);
            blk.t.innerHTML = `<div class="response-text pk-muted"><em>Tool calls in progress…</em></div>`;
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
      
    }
    
    // Handle read-only mode AFTER rendering messages
    readOnlyShown = readOnly;
    if (readOnly) {
      showReadOnlyBanner(reason);
    } else {
      removeReadOnlyBanner();
    }
    updateComposer();
  });

  // A run whose connection was lost stays stored for a reload of its session; showing
  // another one lets it go.
  function leaveLostRun(sessionId) {
    const run = storedRun();
    if (run && run.sessionId !== sessionId) clearStoredRun();
  }
  
  // Helper functions for read-only mode
  function showReadOnlyBanner(reason) {
    removeReadOnlyBanner(); // Remove existing banner if any
    
    const banner = document.createElement('div');
    banner.id = 'readOnlyBanner';
    banner.className = 'read-only-banner';
    banner.innerHTML = `
      <div class="banner-content">
        <span class="banner-icon">${kitIcon('eye')}</span>
        <div class="banner-text">
          <strong>Read-only session</strong>
          <p>${escapeHtml(reason || 'This session cannot be edited.')}</p>
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
  
  // The composer takes messages unless the session shown is read-only or init is still
  // checking for a run of this tab.
  function updateComposer() {
    const enabled = !readOnlyShown && !holding;
    const taskInput = document.getElementById('task');
    taskInput.disabled = !enabled;
    taskInput.placeholder = holding ? 'Checking whether a run of this tab is still going…'
      : readOnlyShown ? 'This session is read-only' : 'Message the agent…';
    document.getElementById('runBtn').disabled = !enabled;
    document.getElementById('fileInput').disabled = !enabled;
    document.getElementById('attachButton').disabled = !enabled;
  }

  // The run of the session whose connection was lost, and so may still be going: its request id, or null.
  chatModule.lostRunIn = function(sessionId) {
    const stored = storedRun();
    const followed = chatModule.hasActiveRequest() && stored?.requestId === run.requestId;
    return stored?.sessionId === sessionId && !followed ? stored.requestId : null;
  };

  // Cancel a run the chat no longer follows: false when the server did not confirm the cancel. Deleting its
  // session cancels it -- the server does not write a deleted session again, but the run would go on for nothing.
  chatModule.cancelLostRun = async function(requestId) {
    try {
      if (!await cancelRun(requestId, { force: true })) return false;
    } catch (error) {
      console.error('Cancel request failed:', error);
      return false;
    }
    // a run stored meanwhile is another one
    if (storedRun()?.requestId === requestId) clearStoredRun();
    return true;
  };

  /**
   * Cancel `asked` -- the run activeRun() named when the viewer was asked -- as they confirmed, and resolve once
   * its stream has brought the cancel or ended: the old run must not keep writing into the chat that leaves its
   * session. A run whose stream brought its answer or its cancel while the viewer was asked is not cancelled --
   * that would take its background sub-agents and session-end hooks along -- and one whose stream was cut off
   * meanwhile is. Resolves 'stopped'; 'starting' when its start has not named it yet, so there is nothing to
   * cancel; 'unconfirmed' when the server did not confirm the cancel or the run's stream did not bring it in 15 s.
   */
  chatModule.cancelActiveRequest = async function(asked) {
    try {
      // brought its answer or its cancel while the viewer was asked: nothing is left to cancel
      if (asked.over) return 'stopped';
      if (!chatModule.hasActiveRequest()) {
        return !asked.requestId || await chatModule.cancelLostRun(asked.requestId) ? 'stopped' : 'unconfirmed';
      }
      if (!asked.requestId) return 'starting';
      markStopping(asked.requestId);
      // force: a run stuck in blocking I/O only ends when its task is cancelled
      if (!await cancelRun(asked.requestId, { force: true })) return 'unconfirmed';
      // its own stream, until the run's cancel -- a run started once that stream has ended is not this one
      for (let waited = 0; chatModule.hasActiveRequest() && run === asked && !asked.over; waited += 200) {
        if (waited >= 15000) return 'unconfirmed';
        await new Promise((resolve) => setTimeout(resolve, 200));
      }
      return 'stopped';
    } catch (error) {
      console.error('Cancel request failed:', error);
      return 'unconfirmed';
    }
  };

  // The run the chat follows while there is something to cancel -- until its stream has brought its answer or its
  // cancel -- for a question about it to hand back to cancelActiveRequest; null otherwise.
  chatModule.activeRun = function() {
    return chatModule.hasActiveRequest() && !run.over ? run : null;
  };

  // Whether the chat follows a run's stream: a fetch stream, or the EventSource of a run followed again after a reload.
  chatModule.hasActiveRequest = function() {
    return followedStream !== null || currentEventSource !== null;
  };

})(window);

