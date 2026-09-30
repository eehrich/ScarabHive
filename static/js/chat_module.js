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

  // The chat scrolls inside the shell's #chatScroll, not the page.
  function scroller() {
    return document.getElementById('chatScroll');
  }

  // Whether the chat keeps its end in view. A scroll up and away from the end turns it
  // off -- the viewer's, or a jump to something above; reaching the end turns it on again.
  // Asking after every event how far the end was (within 150 px) lost it whenever several
  // sub-agents streamed at once: one frame's growth outran the threshold, and from then
  // on the distance only grew.
  let following = true;

  // Where the chat was left standing: a scroll that comes back from further up is
  // somebody moving it, not content arriving (see watchFollow).
  let lastTop = 0;

  // Content growing never moves scrollTop, and content shrinking clamps it at the end:
  // a position that came back from further up is somebody moving the chat.
  function noteScroll() {
    const el = scroller();
    if (!el) return;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 4) following = true;
    else if (el.scrollTop < lastTop) following = false;
    lastTop = el.scrollTop;
  }

  // Opened by hand: the viewer said what they want to look at, and the end is not it.
  // Without this the chat pins itself over what they just unfolded -- the content grew,
  // and growth alone never moves the position, so nothing else says they left the end.
  function readingHere() {
    following = false;
  }

  // force: the end is what the viewer asked for (a message sent, a session opened), so
  // it is not weighed against where they are -- but a scroll of theirs in the meantime
  // still counts, as it does for everything else.
  function pinEnd(force = false) {
    const el = scroller();
    if (!el) return;
    // Asked here, not only on the event: a pin waiting for its frame would otherwise
    // undo a jump made in between before anything had seen it.
    if (!force) noteScroll();
    if (!following) return;
    // The chat is styled to scroll smoothly (shell.css), and it keeps that here: pinned
    // instantly instead, a session shown jumps straight past its stored sub-runs, and
    // what their IntersectionObserver never saw stays unread.
    el.scrollTop = el.scrollHeight;
    lastTop = el.scrollTop;
  }

  // force=true: the viewer expects the end (sent a message, opened a session) and
  // follows again. Otherwise only while following.
  function scrollBottom(force = false) {
    watchFollow();
    if (force) following = true;
    if (following) requestAnimationFrame(() => pinEnd(force));
  }

  let watching = false;

  function watchFollow() {
    const el = scroller();
    const content = document.getElementById('chat');
    if (watching || !el || !content) return;
    watching = true;
    lastTop = el.scrollTop;
    el.addEventListener('scroll', noteScroll, { passive: true });
    new ResizeObserver(() => pinEnd()).observe(content);
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
    runGoesOnBelow();
    // force: a note answers something the user just typed. Honouring
    // "only scroll when already at the bottom" would hide the reply to their
    // own keystroke whenever they had scrolled up.
    scrollBottom(true);
  }

  /**
   * A note written while a run is going: the run's next step goes below it, as it does
   * below a message appended mid-run. Without this the run kept writing into its block
   * ABOVE the note, so every command typed during a run stacked up at the bottom of the
   * chat until the run ended -- and its answer arrived above all of them.
   *
   * Not at once: the step that is streaming would be torn in two (see the append path).
   * A message appended mid-run outranks it: the server reads that one, a note it never sees.
   */
  function runGoesOnBelow() {
    if (chatModule.activeRun() && !pendingAppendRebind) pendingAppendRebind = 'note';
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
    runGoesOnBelow();
    scrollBottom(true);
  }

  // ---------------------------------------------------------------------
  // What the commands read.
  //
  // Every one of them answers from an endpoint the rest of the UI already
  // uses -- no second source of truth, and nothing here decides WHICH
  // commands exist: that is the shared catalogue in chat_commands.py.
  // ---------------------------------------------------------------------

  async function getJSON(url, options = {}) {
    const resp = await fetch(url, { credentials: 'include', ...options });
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

  function postJSON(url, body) {
    return getJSON(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
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
   * A message that OPENED a turn, with something in it. Every stored user
   * message went to the agent -- one that opens with a command word was sent
   * escaped ("//"), so it is shown like any other. A woken run opens its turn
   * with an unmarked `developer` message, which counts; the notes the run and
   * the hooks leave INSIDE a turn carry `injected_by`, which does not.
   * Mirrors chat._is_real_turn / message_roles.opens_a_turn -- the two are
   * compared case by case in tests/cli/test_chat_render_parity.py.
   */
  function isRealTurn(msg) {
    if (!msg || (msg.role !== 'user' && msg.role !== 'developer')) return false;
    if (msg.injected_by) return false;
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
    // Read, filtered and counted by the server with the terminal's own
    // functions (GET /api/sessions/listing): a count, 0 or `all`, the agents
    // meant for chat, this chat's agent and session always -- one rule for
    // both chats, not a second copy of it here.
    const raw = (payload || '').trim();
    let listing;
    try {
      listing = await getJSON('/api/sessions/listing?count=' + encodeURIComponent(raw) +
        '&agent=' + encodeURIComponent(currentAgentName() || '') +
        '&current=' + encodeURIComponent(currentSessionId || ''));
    } catch (error) {
      if (error.status !== 400) throw error;
      addNote(container, 'Usage: /sessions [count|all]   (got: ' + raw + ')');
      return;
    }
    const shown = listing.sessions;
    if (!shown.length && !listing.left_out) {
      addNote(container, 'No sessions yet.');
      return;
    }
    const lines = shown.map(function (s) {
      const marker = s.session_id === currentSessionId ? '*' : ' ';
      const count = String(s.message_count || 0).padStart(4);
      return ' ' + marker + ' ' + s.session_id + '  ' + count + ' msg  ' +
        (s.agent_name || '?') + '  ' + oneLine(s.title || 'Untitled', 48);
    });
    const rest = listing.total - shown.length;
    const most = listing.most_left_out;
    addNote(container, 'Sessions (' + shown.length + ' of ' + listing.total + '):\n' +
      lines.join('\n') +
      (rest > 0 ? '\n   ... ' + rest + ' more -- /sessions <count>, /sessions 0 for no limit' : '') +
      (most ? '\n   (' + listing.left_out + ' more on agents not meant for chat, most ' +
        most.agent + ' ' + most.count + ' -- /sessions all)' : '') +
      '\nUse /resume <id or title> to continue one.');
  }

  // A /title typed before the first message: there is no session to rename
  // yet, so the title goes out with that message (session_title) and the
  // run's first save writes it -- as agent-cli does. Dropped, with a word,
  // when the chat goes to another session first.
  // `firstMessageOut`: that message is out and its run has not started --
  // {title} it took along, null for none.
  // `unlistedTitles`: session id -> {title} its first save writes, for a bare
  // /title and the header while the session list does not have it -- kept for
  // this tab, so a reload during that run still knows it.
  const UNLISTED_TITLES_KEY = 'unlistedSessionTitles';
  let pendingTitle = null;
  let firstMessageOut = null;
  let unlistedTitles = (() => {
    try { return JSON.parse(sessionStorage.getItem(UNLISTED_TITLES_KEY)) || {}; } catch { return {}; }
  })();

  function nameUnlisted(sessionId, title) {
    if (listed(sessionId)) return;  // the list has its title
    unlistedTitles = { ...unlistedTitles, [sessionId]: { title } };
    sessionStorage.setItem(UNLISTED_TITLES_KEY, JSON.stringify(unlistedTitles));
  }

  function listed(sessionId) {
    const pane = window.sessionManager;
    return Boolean(pane && pane.byId && pane.byId.get(sessionId));
  }

  // The title the session list does not have yet -- let go of once the list
  // has the session: from then on its entry is what to show.
  function unlistedTitleOf(sessionId) {
    const unlisted = unlistedTitles[sessionId];
    if (!unlisted) return null;
    if (!listed(sessionId)) return unlisted.title;
    unlistedTitles = { ...unlistedTitles };
    delete unlistedTitles[sessionId];
    sessionStorage.setItem(UNLISTED_TITLES_KEY, JSON.stringify(unlistedTitles));
    return null;
  }

  // The header reads the session list, which has a new session only after its
  // first save: until then it shows the title that save writes.
  function headerTitle(sessionId) {
    const pane = window.sessionManager;
    const title = unlistedTitleOf(sessionId);
    if (title && pane && typeof pane.setCurrent === 'function') pane.setCurrent(sessionId, title);
  }

  // A message written into the session: the pane sets the header from its list.
  function messageWritten(sessionId) {
    window.sessionManager.messageWritten(sessionId);
    headerTitle(sessionId);
  }

  /**
   * `/title <text>` -- the title the session list shows.
   *
   * Through the session pane, not with a PATCH of its own: the pencil in
   * that list does the same write, and the list and the header have to end
   * up in the same state whichever one did it.
   */
  async function cmdTitle(container, payload) {
    const title = (payload || '').trim();
    if (!title) {
      // The stored title, as the session list has it -- not the header's,
      // which reads 'Untitled' for a session without one, a name /resume
      // would then not find.
      const pane = window.sessionManager;
      const entry = currentSessionId && pane && pane.byId && pane.byId.get(currentSessionId);
      const unlisted = currentSessionId ? unlistedTitleOf(currentSessionId) : null;
      const named = pendingTitle || (firstMessageOut && firstMessageOut.title) || (entry ? entry.title : unlisted);
      addNote(container, (named ? 'Title: ' + named : 'This session has no title yet.') +
        '\nUsage: /title <text>   (/resume takes it)');
      return;
    }
    if (!currentSessionId) {
      pendingTitle = title;
      addNote(container, 'Title: ' + title + '   (written with the first message)');
      return;
    }
    if (!window.sessionManager || typeof window.sessionManager.renameTo !== 'function') {
      addNote(container, 'Renaming is not available in this window.');
      return;
    }
    const sessionId = currentSessionId;
    const done = await window.sessionManager.renameTo(sessionId, title);
    if (done) nameUnlisted(sessionId, title);
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
   * `/model [profile]` -- the LLM this chat's next message runs on.
   *
   * Through the profile selector, as /agent goes through the agent one: the
   * button next to the input and the command must not disagree. The choice
   * goes out with the next message, whose run writes it into the session.
   */
  function cmdModel(container, payload) {
    const selector = window.selectorModule;
    if (!selector || typeof selector.setLLMProfile !== 'function') {
      addNote(container, 'The profile selector is not available in this window.');
      return;
    }
    const state = selector.listState('profile');
    if (state !== 'ready') {
      addNote(container, state === 'failed'
        ? 'The LLM profiles could not be read -- the selector has none.'
        : 'The LLM profiles are still loading -- try again in a moment.');
      return;
    }
    const wanted = (payload || '').trim();
    const names = (selector.profiles() || []).map(function (p) { return p.name; });
    const current = selector.getCurrentLLMProfile();

    if (!wanted) {
      // sorted by name, as the terminal lists them
      const profiles = (selector.profiles() || []).slice().sort(function (a, b) {
        return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
      });
      addNote(container, 'LLM: ' + (current || '?') + '\n' + (profiles.length
        ? profiles.map(function (p) {
            return (' ' + (p.name === current ? '*' : ' ') + ' ' + p.name.padEnd(32) + ' ' +
              oneLine(p.description || '', 60)).trimEnd();
          }).join('\n') + '\n  /model <profile> switches; it applies to the next ' +
            (chatModule.hasActiveRequest() ? 'run.' : 'message.')
        : '  (no profiles configured)'));
      return;
    }
    if (names.indexOf(wanted) === -1) {
      const close = closestName(wanted, names);
      addNote(container, 'Unknown LLM profile: ' + wanted +
        (close ? '   Did you mean ' + close + '?' : '') + '\n  /model lists them.');
      return;
    }
    selector.setLLMProfile(wanted);
    // a message to a running run joins it, on the model it started with
    addNote(container, 'LLM: ' + wanted + (chatModule.hasActiveRequest()
      ? '   (from the next run on -- the running one keeps its model)'
      : '   (from the next message on)'));
  }

  /**
   * The name in *names* nearest to *word*, or null: the terminal's "Did you
   * mean" (difflib.get_close_matches, n=1, cutoff 0.6), with its ratio -- a
   * tie goes to the larger name, as there.
   */
  function closestName(word, names) {
    let best = null;
    let bestScore = 0.6;
    names.forEach(function (name) {
      // (name, word): get_close_matches holds the candidate as seq1, the word as seq2
      const score = 2 * matchingChars(name, word) / (word.length + name.length);
      if (score > bestScore || (score === bestScore && (best === null || name > best))) {
        best = name;
        bestScore = score;
      }
    });
    return best;
  }

  /** difflib.SequenceMatcher's matches: the longest common block (earliest in a, then in b), then both sides of it. */
  function matchingChars(a, b) {
    let size = 0;
    let atA = 0;
    let atB = 0;
    for (let i = 0; i < a.length; i++) {
      for (let j = 0; j < b.length; j++) {
        let k = 0;
        while (i + k < a.length && j + k < b.length && a[i + k] === b[j + k]) k++;
        if (k > size) { size = k; atA = i; atB = j; }
      }
    }
    if (!size) return 0;
    return size + matchingChars(a.slice(0, atA), b.slice(0, atB)) +
      matchingChars(a.slice(atA + size), b.slice(atB + size));
  }

  /**
   * `/copy` -- the last answer onto the clipboard.
   *
   * The text as the model wrote it, not what the page rendered from it: the
   * server reads it from the record the way the terminal's /copy does
   * (GET /chat/last_answer).
   */
  async function cmdCopy(container) {
    // The record holds what a checkpoint wrote mid-run, and the answer shows
    // before its save: the terminal takes no command during a turn at all.
    // This tab's own run is known here; one elsewhere the server refuses (409) as far
    // as it sees it -- with session presence any process, without only its own runs.
    if (chatModule.hasActiveRequest()) {
      addNote(container, 'The request is still answering -- /copy once it has finished.');
      return;
    }
    let answer = null;
    if (currentSessionId) {
      try {
        answer = await getJSON('/chat/last_answer?session_id=' + encodeURIComponent(currentSessionId));
      } catch (e) {
        if (e && e.status === 409) {
          addNote(container, e.message);
          return;
        }
        throw e;
      }
    }
    const text = (answer && answer.text) || '';
    if (!text) {
      addNote(container, 'No answer to copy yet.');
      return;
    }
    let refused = '';
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      try {
        await navigator.clipboard.writeText(text);
      } catch (e) {
        refused = (e && e.message) || String(e);
      }
    } else {
      refused = 'no clipboard for this page (only over https or on localhost)';
    }
    if (refused && !copyBySelection(text)) {
      addNote(container, 'Could not copy: ' + refused.replace(/\.+$/, '') + '.');
      return;
    }
    addNote(container, 'Copied the last answer (' + Array.from(text).length + ' chars, ' +
      text.split('\n').length + ' line(s)).');
  }

  /**
   * The copy command browsers kept for pages without the Clipboard API -- a
   * page over plain http on another host, which is how the server's UI is
   * often reached. It copies a selection, so it makes one; true if copied.
   */
  function copyBySelection(text) {
    if (typeof document.execCommand !== 'function') return false;
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    let copied = false;
    try {
      copied = document.execCommand('copy');
    } catch (e) {
      copied = false;
    }
    area.remove();
    if (taskInputEl) taskInputEl.focus();  // the selection took the focus from the input
    return copied;
  }

  /**
   * `/attach [<path> | clear]` -- files for the next message.
   *
   * Listing and clearing as in the terminal. A path names the disk the
   * terminal runs on, which a page cannot read: the file picker opens
   * instead, the one behind the paperclip.
   */
  function cmdAttach(container, payload) {
    const upload = window.fileUploadModule;
    if (!upload) {
      addNote(container, 'Attaching files is not available in this window.');
      return;
    }
    const wanted = (payload || '').trim();
    if (!wanted) {
      const files = upload.getFiles();
      addNote(container, files.length
        ? files.map(function (file) {
            return '  ' + file.name + ' [' + upload.getFileType(file) + ']';
          }).join('\n')
        : 'No attachments queued. Usage: /attach <path> -- here it opens the file picker.');
      return;
    }
    if (wanted.toLowerCase() === 'clear') {
      upload.clear();
      addNote(container, 'Attachments cleared.');
      return;
    }
    const picker = document.getElementById('fileInput');
    // Opened without a fresh keypress, the picker is dropped with no word
    // (the command ran after an awaited request): then only the paperclip is left.
    const activation = navigator.userActivation;
    if (picker && !(activation && !activation.isActive)) {
      picker.click();
      addNote(container, 'A path on this machine is out of the page\'s reach -- ' +
        'choose the file in the picker (or with the paperclip).');
      return;
    }
    addNote(container, 'A path on this machine is out of the page\'s reach -- ' +
      'attach the file with the paperclip.');
  }

  /**
   * `/edit [text]` -- the terminal writes the next message in $EDITOR, since
   * its prompt is one line. Here the input already is that editor: the text
   * goes into it, unsent.
   */
  function cmdEdit(container, payload) {
    if (!taskInputEl) return;
    if (payload) {
      taskInputEl.value = payload;
      // what the textarea grows on, and what turns the action button into Send
      taskInputEl.dispatchEvent(new Event('input', { bubbles: true }));
    } else {
      addNote(container, 'The input is the editor here -- Enter starts a new line, Ctrl+Enter sends.');
    }
    taskInputEl.focus();
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
  /**
   * The words after /undo, /retry and /rewind -- chat_commands.parse_undo, the
   * terminal's reading: "files", "overwrite", "force", for /rewind a number;
   * a leading "--" is allowed. Unknown words come back in `errors`.
   */
  function parseUndoWords(payload, rewind) {
    const request = { files: false, overwrite: false, force: false, checkpoint: null, errors: [] };
    const allowed = rewind ? ['overwrite', 'force'] : ['files', 'overwrite', 'force'];
    (payload || '').trim().split(/\s+/).filter(Boolean).forEach(function (token) {
      let word = token.toLowerCase();
      // "--files" is a word with dashes; "-1" is not checkpoint 1.
      if (/^-{1,2}[a-z]+$/.test(word)) word = word.replace(/^-+/, '');
      if (rewind && /^[0-9]+$/.test(word) && request.checkpoint === null) {
        request.checkpoint = parseInt(word, 10);
      } else if (allowed.indexOf(word) !== -1) {
        request[word] = true;
      } else {
        request.errors.push(token);
      }
    });
    if (rewind && request.checkpoint === null && request.overwrite) {
      request.errors.push('overwrite needs a checkpoint number');
    }
    if (!rewind && request.overwrite && !request.files) request.errors.push('overwrite goes with files');
    return request;
  }

  async function cmdUndo(container, payload, retry) {
    const name = retry ? '/retry' : '/undo';
    if (!currentSessionId) {
      addNote(container, 'Nothing to take back -- this chat has no session yet.');
      return;
    }
    const words = parseUndoWords(payload, false);
    if (words.errors.length) {
      addNote(container, name + ' takes files, overwrite (with files) and force: ' +
        name + ' files puts back the files the exchange changed too.');
      return;
    }
    // "force" the way agent-cli's --force means it: for a lock a crashed
    // process left behind. The server refuses a session that is running, and
    // without this there would be no way past a leftover.
    const forced = words.force;
    // The session cut is this one, whatever the chat opens while the request runs.
    const cut = currentSessionId;
    let answer;
    try {
      answer = await postJSON('/chat/undo' + (forced ? '?force=true' : ''), {
        session_id: cut,
        // The session's own agent wins on the server; this is the fallback
        // for one that has no record yet.
        agent_name: currentAgentName() || null,
        // The files the exchange changed are put back first; a rewind that is
        // refused keeps the exchange, and the note says why.
        files: words.files,
        overwrite: words.overwrite,
      });
    } catch (e) {
      if (e && e.status === 409 && words.files) {
        addNote(container, e.message + '\n  The exchange stays.');
        return;
      }
      if (e && e.status === 409 && !forced) {
        addNote(container, e.message +
          '\n  ' + name + ' force takes it anyway -- only for a lock a crashed process left behind: ' +
          'a live run writes the exchange back.');
        return;
      }
      throw e;
    }
    if (!answer.dropped) {
      addNote(container, 'Nothing to take back in this session yet.');
      return;
    }
    // The reload puts the record's LLM profile back into the selector; one
    // chosen since (the button, /model) goes with the next message -- and
    // "ask again on a stronger model" is what a /retry is for.
    const selector = window.selectorModule;
    const chosen = selector && selector.getCurrentLLMProfile();
    const asked = answer.dropped.text || '';
    // What the file rewind reports (files put back, or why not), under either note.
    const filesNote = answer.files && answer.files.text ? '\n' + answer.files.text : '';
    // null: another click overtook this reload -- a session opened, or one still
    // loading -- and the chat is no longer this one's to fill.
    const shown = currentSessionId === cut ? await window.sessionManager.loadSession(cut) : null;
    if (shown === null || currentSessionId !== cut) {
      // Its profile stands, and a question put back into its input would be asked there.
      addNote(container, 'Dropped from ' + cut + ': ' + oneLine(asked, 70) + filesNote +
        (retry ? '\n  Not put back into the input -- the chat has moved on meanwhile.' : ''));
      return;
    }
    if (chosen && selector.getCurrentLLMProfile() !== chosen) selector.setLLMProfile(chosen);
    addNote(container, 'Dropped: ' + oneLine(asked, 70) + filesNote);
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
      ? 'Ask it again with Ctrl+Enter -- the file it carried has to be attached again.'
      : 'Ask it again with Ctrl+Enter.');
  }

  /**
   * `/rewind [n] [overwrite]` -- the files only, the conversation stays.
   *
   * Bare lists the checkpoints the server numbers (one per turn that changed
   * files); a number puts the files back as they were before it. The text is
   * the server's, the same lines agent-cli prints.
   */
  async function cmdRewind(container, payload) {
    if (!currentSessionId) {
      addNote(container, 'No session yet -- nothing has been recorded.');
      return;
    }
    const words = parseUndoWords(payload, true);
    if (words.errors.length) {
      addNote(container, 'Usage: /rewind lists the checkpoints, /rewind <n> puts the files back as ' +
        'they were before checkpoint n, /rewind <n> overwrite also the files changed outside the agent ' +
        '(force: past a lock a crashed process left).');
      return;
    }
    const agent = currentAgentName();
    if (words.checkpoint === null) {
      const listing = await getJSON('/chat/checkpoints?session_id=' +
        encodeURIComponent(currentSessionId) +
        (agent ? '&agent_name=' + encodeURIComponent(agent) : ''));
      addNote(container, listing.text || 'No file changes are recorded for this session.');
      return;
    }
    let answer;
    try {
      answer = await postJSON('/chat/rewind' + (words.force ? '?force=true' : ''), {
        session_id: currentSessionId,
        agent_name: agent || null,
        checkpoint: words.checkpoint,
        overwrite: words.overwrite,
      });
    } catch (e) {
      if (e && (e.status === 409 || e.status === 404)) {
        addNote(container, e.message);
        return;
      }
      throw e;
    }
    addNote(container, answer.text || 'Files rewound.');
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

  /**
   * `/context` -- what fills the window of this session.
   *
   * Two blocks the server keeps apart and so does this: what the provider
   * COUNTED on the last call, and what the conversation holds now, estimated
   * per kind. The split is the point -- "42k of 200k" says the window is
   * filling, only the split says the tool results are doing it.
   */
  async function cmdContext(container) {
    if (!currentSessionId) {
      addNote(container, 'No session yet -- the window fills with the first message.');
      return;
    }
    const agent = currentAgentName();
    const data = await getJSON('/chat/context?session_id=' +
      encodeURIComponent(currentSessionId) +
      (agent ? '&agent_name=' + encodeURIComponent(agent) : ''));

    const labels = {
      tool_results: 'tool results', answers: 'answers', questions: 'your messages',
      system_prompt: 'system prompt', tools: 'tool schemas', other: 'other messages',
    };
    const last = data.last_call || {};
    // The window the NEXT call runs against. The measured line brings its own:
    // a /model switch changes the model and with it the size, and one share
    // against the other would state a fill that is not true.
    const window_ = data.window || 0;
    const measuredWindow = last.window || 0;
    const lines = ['Context of ' + data.session_id +
      (data.agent_name ? ' (' + data.agent_name + ')' : '') + ':'];
    if (last.prompt_tokens) {
      lines.push('  last call     ' + last.prompt_tokens.toLocaleString() +
        (measuredWindow ? ' of ' + measuredWindow.toLocaleString() +
          '  (' + Math.round(last.prompt_tokens / measuredWindow * 100) + '%)' : '') +
        (last.cached ? ', ' + last.cached.toLocaleString() + ' of them cached' : '') +
        (last.is_stale ? '   [stale: the context was rewritten since]' : ''));
    }
    lines.push('  ---- and what the conversation holds now, estimated ----');
    const parts = Object.entries((data.estimated || {}).parts || {})
      .sort((a, b) => b[1].tokens - a[1].tokens);
    const width = parts.reduce((w, [name]) => Math.max(w, (labels[name] || name).length), 0);
    parts.forEach(([name, part]) => {
      if (!part.tokens) return;   // no line for a part with nothing in it
      const unit = name === 'tools' ? ' tools' : ' messages';
      lines.push('  ' + (labels[name] || name).padEnd(width) + '  ' +
        String(part.tokens.toLocaleString()).padStart(8) +
        (name === 'system_prompt' ? '' : '   ' + part.count + unit));
    });
    const total = (data.estimated || {}).total || 0;
    lines.push('  ' + 'together'.padEnd(width) + '  ' +
      String(total.toLocaleString()).padStart(8) +
      (window_ ? '   of ' + window_.toLocaleString() +
        '  (' + Math.round(total / window_ * 100) + '%)' : ''));
    addNote(container, lines.join('\n'));
  }

  async function cmdResume(container, payload) {
    const typed = (payload || '').trim();
    let id;
    let chose = '';
    if (!typed) {
      // Bare: the newest session /sessions lists that is not this one. Any
      // chat agent's -- the browser switches the agent with the session,
      // where the terminal is bound to its own.
      const listing = await getJSON('/api/sessions/listing?count=2' +
        '&agent=' + encodeURIComponent(currentAgentName() || '') +
        '&current=' + encodeURIComponent(currentSessionId || ''));
      const last = listing.sessions.find(function (s) { return s.session_id !== currentSessionId; });
      if (!last) {
        addNote(container, 'No earlier session to continue. /sessions lists them.');
        return;
      }
      id = last.session_id;
      addNote(container, 'Resuming ' + id + ' -- ' + oneLine(last.title || 'Untitled', 60));
    } else {
      // Ids are machine-made and cannot be renamed, so a person may type the
      // title they gave the session instead -- resolved by the server, by the
      // same rule the terminal follows (SessionManager.resolve_session_ref).
      const found = await getJSON('/api/sessions/resolve?ref=' + encodeURIComponent(typed));
      id = found.session_id;
      // A title several sessions share names the newest, and says it chose.
      const others = found.others || [];
      chose = others.length ? '   (the newest of ' + (others.length + 1) +
        ' with this title -- /sessions all lists every id)' : '';
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
      addNote(container, 'Resumed session: ' + id + chose);
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
      } else if (msg.role === 'developer') {
        // Not a turn anybody took -- the run putting something in front of the
        // model. Same line the terminal prints (tests/cli/test_chat_render_parity.py).
        if (!text) return;
        parts.push(plain('\n[note] ' + text));
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
   * the shared catalogue. One this surface does not offer (there is no
   * terminal to leave, so no /exit in its /help) can still be typed:
   * /chat/resolve knows every built-in, whichever surface asks. /exit gets
   * its own answer; anything else without a handler says so out loud rather
   * than doing nothing, because silence would read as a broken command.
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
      title: function () { return cmdTitle(container, payload); },
      agent: function () { return cmdAgent(container, payload); },
      vars: function () { return cmdVars(container, payload); },
      tools: function () { return cmdTools(container, payload); },
      costs: function () { return cmdCosts(container); },
      context: function () { return cmdContext(container); },
      history: function () { return cmdHistory(container, payload); },
      last: function () { return cmdLast(container); },
      undo: function () { return cmdUndo(container, payload, false); },
      retry: function () { return cmdUndo(container, payload, true); },
      rewind: function () { return cmdRewind(container, payload); },
      export: function () { return cmdExport(container, payload); },
      model: function () { return cmdModel(container, payload); },
      copy: function () { return cmdCopy(container); },
      attach: function () { return cmdAttach(container, payload); },
      edit: function () { return cmdEdit(container, payload); },
      // not in the browser's catalogue, but typed anyway: a tab has no terminal to leave
      exit: function () {
        addNote(container, 'Nothing to end in the browser -- the session is saved as it stands. ' +
          'Close the tab, or /new for a fresh session.');
      },
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
    taskInput.dispatchEvent(new Event('input', { bubbles: true }));
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
    row.appendChild(box);
    chatContainer.appendChild(row);
    const blk = runView(box);
    blk.row = row;
    scrollBottom();
    return blk;
  }

  /**
   * The parts one run is shown in, built into `host`: a section per LLM call, and its answer.
   *
   * The same for the run the chat follows and for every run started under it. A
   * sub-agent's steps are read the way its caller's are, one level in -- and what a
   * run is in the middle of (the call in flight, the answer streaming) belongs to that
   * run: kept in one place for the whole page, a sub-agent's first step would close
   * its caller's.
   *
   * One section per LLM call is built as the run goes (see stepOf); an answer with no
   * run behind it -- a session read back from disk -- has none and shows its text only.
   */
  function runView(host, runId = null, depthLevel = 0) {
    host.insertAdjacentHTML('beforeend', `
      <div class="steps"></div>
      <div class="container-section" style="display: none;">
        <div class="container-header" data-toggle="response">
          <span class="toggle-arrow">${kitIcon('chevron-right')}</span>
          <span class="type-icon">${kitIcon('message-square')}</span>
          <span class="container-label">Response</span>
        </div>
        <div class="container-body assistant-text" style="display: block;"></div>
      </div>`);
    const response = host.querySelector(':scope > .container-section');
    // A sub-agent's answer is its caller's material, not the conversation: folded or
    // not as the viewer chose (chat.sub_agent_output), under a header of its own.
    if (host.classList.contains('sub-run-body') && chatPrefs.sub_agent_output === 'collapsed') {
      response.querySelector('.assistant-text').style.display = 'none';
    }
    foldOnClick(response.querySelector('[data-toggle="response"]'));
    return {
      box: host,
      t: response.querySelector('.assistant-text'),
      steps: host.querySelector(':scope > .steps'),
      // The step containers this run filled before a message appended mid-run moved
      // it on to a fresh block (rebindLiveBlock): the rows of its earlier calls are
      // there (rowOf), and the answer folds them with the rest.
      pastSteps: [],
      runId,
      // The server's depth of this run's id; its rows are indented from here.
      depthLevel,
      // The call in flight, for the status events that carry no step of their own
      // (every tool scope, which knows its tool and not the loop around it). Null
      // between the run's answer and its end, so the run's own closing lines do not
      // land in the last call that happened to be open.
      openStep: null,
      // The step whose answer is streaming.
      streamStep: null,
      // run id -> view, for the runs started under this one (see subRunView).
      subRuns: new Map(),
    };
  }

  /**
   * A container-section header that folds its body away, by mouse and by keyboard.
   *
   * The header is a div: it is given what a button brings along (see foldOnActivate).
   * A run's steps all fold at its answer, and a step that only a mouse could open
   * again would take everything inside it out of reach of the keyboard.
   */
  function foldOnClick(header) {
    const body = header.nextElementSibling;
    const section = header.parentElement;
    header.setAttribute('role', 'button');
    header.setAttribute('tabindex', '0');
    // the body's display stays the state
    markOpen(section, body.style.display !== 'none');
    const flip = () => {
      const open = body.style.display === 'none';
      body.style.display = open ? 'block' : 'none';
      markOpen(section, open);
      if (open) readingHere();
      // Touched by hand: from here on this section is the viewer's, and the next step
      // starting must not fold it away under them.
      section.dataset.touched = 'true';
    };
    header.addEventListener('click', flip);
    header.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      e.preventDefault();  // Space scrolls the chat away otherwise
      flip();
    });
  }

  /** Say whether a section is open: data-open to chat.css (the arrow), aria-expanded to assistive technology. */
  function markOpen(section, open) {
    section.dataset.open = String(open);
    section.querySelector(':scope > .container-header').setAttribute('aria-expanded', String(open));
  }

  /** The section that belongs to no single call: the run's own start and end. */
  const RUN_SECTION = 'run';

  /**
   * The section for one LLM call, created on first sight.
   *
   * A message used to have ONE Thinking box and ONE Status box for all of its steps,
   * so a run of five calls piled five lots of reasoning and every tool line into the
   * same two boxes with nothing saying which call a line came from. Measured on a real
   * three-step run, the stream already answers that: `thinking` with a step and no
   * assistant opens the call, everything of that call follows it, and the next one
   * closes it. Tool status events carry no step of their own (they come from
   * status_scope, which knows the tool and not the loop) -- they are placed by the call
   * that is open when they arrive, which is what the ORDER of the stream says.
   */
  function stepOf(view, step) {
    if (!view || !view.steps) return null;
    const key = String(step);
    // Direct children only: a sub-agent's run sits INSIDE one of these sections, and
    // its step 1 is not this run's.
    const existing = view.steps.querySelector(`:scope > [data-step="${CSS.escape(key)}"]`);
    if (existing) return existing;
    const section = document.createElement('div');
    section.className = 'container-section step-section';
    section.dataset.step = key;
    section.innerHTML = `
      <div class="container-header" data-toggle="step">
        <span class="toggle-arrow">${kitIcon('chevron-right')}</span>
        <span class="type-icon">${kitIcon(key === RUN_SECTION ? 'activity' : 'brain')}</span>
        <span class="container-label">${key === RUN_SECTION ? 'Run' : `Step ${escapeHtml(key)}`}</span>
        <span class="step-note pk-muted"></span>
      </div>
      <div class="container-body">
        <div class="step-thinking" hidden>
          <span class="thinking-toggle">${kitIcon('chevron-right')}<span>Thinking</span></span>
          <pre class="thinking-content"></pre>
        </div>
        <div class="status-body"></div>
      </div>`;
    // In step order: a live step the gate held back (waitingFor) can be older than the
    // stored steps read before it -- an empty answer the loop dropped, or a text-only one a
    // continuation followed, has no stored section, and its marker comes after them.
    const later = key === RUN_SECTION ? null
      : [...view.steps.querySelectorAll(':scope > .step-section')].find((other) => Number(other.dataset.step) > Number(key));
    view.steps.insertBefore(section, later || null);
    foldOnClick(section.querySelector('.container-header'));
    const thinking = section.querySelector('.step-thinking');
    const toggle = thinking.querySelector('.thinking-toggle');
    const text = thinking.querySelector('.thinking-content');
    foldOnActivate(toggle, text);
    setFolded(toggle, text, chatPrefs.thinking === 'collapsed');
    // Opened or closed by hand, it is the viewer's: a changed setting leaves it alone.
    const touch = () => { thinking.dataset.touched = 'true'; };
    toggle.addEventListener('click', touch);
    toggle.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') touch(); });
    return section;
  }

  /** Fold or unfold what foldOnActivate made foldable, saying so to assistive technology. */
  function setFolded(trigger, body, folded) {
    body.hidden = folded;
    trigger.setAttribute('aria-expanded', folded ? 'false' : 'true');
  }

  /**
   * The box a call's thinking goes into, shown from its first word on.
   *
   * A call that does not reason shows no box at all; one that does shows its header,
   * and the text itself folded or not as the viewer chose (chatPrefs.thinking).
   */
  function thinkingOf(section) {
    const wrapper = section.querySelector(':scope > .container-body > .step-thinking');
    wrapper.hidden = false;
    return wrapper.querySelector('.thinking-content');
  }

  /** Fold a step section away or open it, unless the viewer took it in hand. */
  function setStepFolded(section, folded) {
    if (section.dataset.touched === 'true') return;
    section.querySelector(':scope > .container-body').style.display = folded ? 'none' : '';
    markOpen(section, !folded);
  }

  /**
   * Open the section for a call -- and, if the viewer asked for it, fold the one before.
   *
   * Folding as the run goes keeps a long run from being a wall, but it takes away what
   * was just read while the next call is still thinking; which of the two a viewer
   * wants is theirs to say (chatPrefs.fold_steps). By default the steps stay open while
   * the run works and fold when it has answered (settleView). A section the viewer
   * opened or closed by hand is left alone either way -- it is theirs from that moment.
   */
  function openStep(view, step) {
    const section = stepOf(view, step);
    if (!section) return null;
    if (chatPrefs.fold_steps === 'at_next_step') {
      for (const other of view.steps.querySelectorAll(':scope > .step-section')) {
        if (other !== section) setStepFolded(other, true);
      }
    }
    return section;
  }

  /** A run has answered: its steps are done, and shown the way the viewer wants a finished run's. */
  function settleView(view) {
    if (!view || !view.steps) return;
    view.steps.dataset.settled = 'true';
    foldSettled(view.steps);
  }

  /**
   * The steps of a run that has answered, as the viewer wants them now: folded
   * (at_end), open (never), or all but the last folded (at_next_step -- where such a
   * run leaves them). Marked, so a setting changed later is applied to them again.
   */
  function foldSettled(steps) {
    const sections = [...steps.querySelectorAll(':scope > .step-section')];
    sections.forEach((section, index) => setStepFolded(section, chatPrefs.fold_steps === 'at_end'
      || (chatPrefs.fold_steps === 'at_next_step' && index < sections.length - 1)));
  }

  /**
   * Where a status event belongs: the call in flight, or the run if none is.
   *
   * Deliberately NOT `meta.step`, although status events carry one. Measured on a real
   * run: it never decides anything, because addStatusEvent places an operation once, at
   * the event that creates it, and updates it where it is from then on. The coordinator
   * and the worker are both created by their `started`, which carries no step, so their
   * later "step 2/30" only rewrites a row that already sits in the run's section; and a
   * tool scope carries no step at all.
   *
   * The one event that IS new and does carry a step is a sub-agent's -- and there the
   * number is a step of the SUB-run. Taken at face value it files a sub-agent working
   * for call 5 under call 1. So the only case the field would ever have decided is the
   * one case where it lies, and the call in flight is the right answer for all of them.
   *
   * "The call in flight" of the run the line is FROM, though: a sub-agent's lines go
   * into its own steps (statusViewFor), not into whatever call its caller is on when
   * they arrive -- an async sub-agent's are delivered while the caller waits for it,
   * steps later.
   */
  function statusBodyFor(view) {
    const section = stepOf(view, (view && view.openStep) || RUN_SECTION);
    return section ? section.querySelector(':scope > .container-body > .status-body') : null;
  }

  // A request id's levels as the server counts them (utils/tree_hierarchy.py,
  // SUFFIX_PATTERN), peeled off its right edge: `_nnn` a tool call, `_sub_x`,
  // `_sub_cont_x`, `_async_x`, `_minlen_n` the runs sub_agent_manager starts. A run's
  // depth (a sub_run envelope's depth_level) is its number of levels.
  const ID_LEVEL = /_((?:sub_cont|sub|async|minlen)_[0-9a-zA-Z]+|\d{3})$/;
  function parentOf(id) {
    const level = ID_LEVEL.exec(id || '');
    return level && level.index > 0 ? id.slice(0, level.index) : null;
  }
  function depthOf(id) {
    let depth = 0;
    for (let up = parentOf(id); up; up = parentOf(up)) depth += 1;
    return depth;
  }

  /**
   * The run a status line is from: the deepest run started under `blk` whose id it
   * carries, or `blk` itself.
   *
   * By id, not by arrival: a sub-agent's id starts with the id of the call that started
   * it, and so does every line it produces (`…_003_async_r2a527_013`).
   */
  function statusViewFor(blk, requestId) {
    let found = blk;
    let length = -1;
    for (const view of allSubRuns(blk)) {
      const id = view.runId;
      if ((requestId === id || requestId.startsWith(`${id}_`)) && id.length > length) {
        found = view;
        length = id.length;
      }
    }
    return found;
  }

  /**
   * A status line of the followed run or of a run under it, into the run it is from.
   *
   * A line for an operation already shown updates its row where that row is, so no
   * section is made for it: after a message appended mid-run the run's closing lines
   * update rows of the block before, and asking for the section to put them in stood
   * up an empty "Run" section in the new one.
   */
  function placeStatus(blk, ev) {
    takeOverStoredPath(blk, ev.request_id || '');
    const waiting = waitingFor(blk, ev.request_id || '');
    if (waiting) {
      waiting.push(() => placeStatus(blk, ev));
      return;
    }
    const view = statusViewFor(blk, ev.request_id || '');
    const key = ev.request_id && ev.request_id !== 'default' ? ev.request_id : ev.server;
    const existing = activeOperations.get(key);
    // A call the read brought back keeps its step: the read can be steps ahead of the
    // lines that waited for it, and the step it ended on is not the one this call ran in.
    // Wherever the read put it: the followed run's call in the session's own blocks, the
    // call of an agent called as a tool (its run's id is its call's) in its caller's steps,
    // around the agent's box -- the row then belongs to the caller.
    const storedCall = !existing && ev.request_id && chatContainer.querySelector(
      `.step-section > .container-body > .status-body > .tool-detail[data-key="${CSS.escape(ev.request_id)}"]`);
    const owner = storedCall && view.runId === ev.request_id ? statusViewFor(blk, parentOf(ev.request_id) || '') : view;
    addStatusEvent(existing ? existing.parentElement : (storedCall ? storedCall.parentElement : statusBodyFor(view)), ev, owner);
    // released after its stream ended: a row it leaves open is not updated any more (release)
    if (blk.streamEnded && activeOperations.has(key)) lateRows.add(key);
  }

  /**
   * The queue a live event of `requestId` waits in, or null when it can go now.
   *
   * It waits for what the page still reads about its run: its own session and the
   * listing of the runs under it (loadStoredSubRun), or -- for a run under the followed
   * one with no box yet -- the listing of the session on screen (sessionGate). Applied
   * before, it lands where the page has no step for it yet, and a run under it gets a
   * box beside the one the read makes; applied after, it lands as in a run followed
   * from its start. The followed run's own lines never wait.
   */
  function waitingFor(blk, requestId) {
    const view = statusViewFor(blk, requestId);
    // A stored run on its way the page has not read -- one an `_async_` or `_minlen_`
    // level kept from being taken over, or one under a run taken over (then the nearest
    // view itself): its read lists the run the event is from, so the event waits for it,
    // started now if need be.
    for (let id = requestId; id; id = id === view.runId ? null : parentOf(id)) {
      const stored = storedRunViews.get(id);
      if (!stored || !stored.element.isConnected) continue;
      if (!stored.waiting && stored.element.dataset.stored === 'pending') {
        storedWatch.unobserve(stored.element);
        loadStoredSubRun(storedSubRuns.get(stored.element));
      }
      if (stored.waiting) return stored.waiting;
      break;   // read (or its read failed): its listing is done
    }
    if (view.waiting) return view.waiting;
    // A run under the followed one with no box yet may be one the session's listing
    // brings -- also under a live box that is never read itself (an agent called as a
    // tool). While that listing is on its way there is no stored box to wait for.
    const below = run.requestId && requestId.startsWith(run.requestId) ? requestId.slice(run.requestId.length) : requestId;
    const underARun = /_(?:sub_cont|sub|async|minlen)_[0-9a-zA-Z]+(?:_|$)/.test(below);
    return underARun && sessionGate && sessionGate.waiting ? sessionGate.waiting : null;
  }

  /** What waited for `gate` goes now, in the order it came. */
  function release(gate) {
    const waiting = gate.waiting || [];
    gate.waiting = null;
    waiting.forEach((go) => go());
    // their stream ended while they waited: what they left open is not updated any more
    // -- after the whole batch (an end in it closes its row first), and only theirs
    if (lateRows.size) {
      markOpenScopesUnfinished([...lateRows]);
      lateRows.clear();
    }
  }

  /** Every run started under `blk`, at any depth. */
  function* allSubRuns(blk) {
    for (const view of blk.subRuns.values()) {
      yield view;
      yield* allSubRuns(view);
    }
  }

  // How much of one tool's arguments or result is put into the page. A read of a big
  // file comes back whole, and a run that reads twenty would otherwise carry megabytes
  // of text in the DOM for a panel almost nobody opens.
  const TOOL_DETAIL_CHARS = 4000;

  /**
   * Make `trigger` fold `body`, by mouse and by keyboard.
   *
   * The trigger is a span, not a button: the thing to click is the status line's own
   * text, and a button there would be a second control on a line that already has one.
   * A span has to be given what a button brings along -- the role, a tab stop, the
   * state, and Enter/Space -- or the detail exists for mouse users only.
   */
  function foldOnActivate(trigger, body) {
    trigger.setAttribute('role', 'button');
    trigger.setAttribute('tabindex', '0');
    trigger.setAttribute('aria-expanded', 'false');
    const flip = () => {
      body.hidden = !body.hidden;
      trigger.setAttribute('aria-expanded', body.hidden ? 'false' : 'true');
      if (!body.hidden) readingHere();
    };
    trigger.addEventListener('click', flip);
    trigger.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      e.preventDefault();  // Space scrolls the chat away otherwise
      flip();
    });
  }

  /**
   * The one folded block a tool's row carries, created on the first half that arrives.
   *
   * Arguments and result are two events and ONE thing to read: what this call was and
   * what came of it. Two controls for that put a choice on the line that nobody wants
   * to make, and a line of status text plus two link labels is mostly labels.
   *
   * The status text itself is the control. It costs no width, and a row without a
   * detail keeps looking exactly as it did -- the hover is the only thing that gives
   * it away, which is the point: a run is read by its lines, not by its widgets.
   */
  function detailBlockFor(row) {
    const existing = row.querySelector(':scope > .tool-detail-body');
    if (existing) return existing;
    const line = row.querySelector('.progress-line');
    const trigger = line && line.querySelector('.progress-message');
    if (!trigger) return null;
    const body = document.createElement('div');
    body.className = 'tool-detail-body';
    body.hidden = true;
    // The line carries its own indent, the row does not -- so the block would start at
    // the left edge while the line it belongs to sits three levels in.
    body.style.marginLeft = line.style.paddingLeft || '';
    row.appendChild(body);
    // Quiet on purpose, and without a tooltip to explain itself either: the hover
    // says the line can be pressed, and what comes out is named in the block.
    row.classList.add('has-detail');
    foldOnActivate(trigger, body);
    return body;
  }

  /**
   * The arguments a tool call was made with, and what came back, ON the tool's own row.
   *
   * `tool_call` and `tool_result` carry the same `request_id` as the status scope of
   * that call (measured: `_003`, `_004`, `_005` in a real run) and arrive after its
   * lines, so the row is already there to hang them on. Neither event had a branch in
   * this switch at all until now: the status lines say what a tool did, and what it was
   * asked and what it answered went nowhere.
   */
  function toolDetail(blk, data, kind, payload, live = false) {
    if (!blk || !blk.steps) return;
    const text = JSON.stringify(payload, null, 2) || '';
    const shown = text.length > TOOL_DETAIL_CHARS
      ? `${text.slice(0, TOOL_DETAIL_CHARS)}\n… ${text.length - TOOL_DETAIL_CHARS} more characters`
      : text;
    const action = data.action || 'tool';
    const key = data.request_id ? CSS.escape(data.request_id) : null;
    // Where this call's parts already are: a stored run taken over by its live events
    // gets a call's arguments and result from the session read AND from the stream, and
    // a second block made for them would stand empty, or split the call in two.
    // A live part by its request id, anywhere in the chat: the followed run's call read
    // back sits in the session's own block, not in the live one (placeStatus finds it the
    // same way). A replayed part stays in its block -- a session older than the stamps keys
    // it by the provider's id, which another run may use too.
    const scope = live ? chatContainer : blk.steps;
    const known = key && scope.querySelector(`.tool-detail[data-key="${key}"] > .tool-detail-body, `
      + `.operation-progress[data-request-id="${key}"] > .tool-detail-body`);
    const row = key && !known ? scope.querySelector(`.operation-progress[data-request-id="${key}"]`) : null;
    // No row means the call opened no status scope -- an unknown tool, say. What it was
    // asked still belongs to the call that asked, rather than nowhere; with no line to
    // click it brings a line of its own, naming the tool.
    const body = known || (row ? detailBlockFor(row) : looseDetailBlock(blk, action, data.request_id));
    if (!body) return;
    // Once per call and kind. Only by id: without one, calls to the same tool share a block.
    if (key && body.querySelector(`:scope > [data-kind="${kind}"]`)) return;

    const part = document.createElement('div');
    part.className = 'tool-detail-part';
    part.dataset.kind = kind;
    const label = document.createElement('span');
    label.className = 'tool-detail-label';
    label.textContent = kind;
    const pre = document.createElement('pre');
    pre.textContent = shown;  // a tool's answer is data, never markup
    part.append(label, pre);
    body.appendChild(part);
  }

  /**
   * The same block for a call with no status line of its own.
   *
   * Keyed by the call, not by the tool: two calls to the same tool in one step are two
   * things to read, and the `request_id` that separates them is right there. Only where
   * there is none does the tool's name have to do -- then both halves of one call have
   * nothing else in common.
   */
  function looseDetailBlock(blk, action, requestId) {
    const host = statusBodyFor(blk);
    if (!host) return null;
    const key = requestId || action;
    const existing = host.querySelector(`:scope > .tool-detail[data-key="${CSS.escape(key)}"]`);
    if (existing) return existing.querySelector('.tool-detail-body');
    const loose = document.createElement('div');
    loose.className = 'tool-detail';
    loose.dataset.key = key;
    const trigger = document.createElement('span');
    trigger.className = 'tool-detail-name';
    trigger.textContent = action;
    const body = document.createElement('div');
    body.className = 'tool-detail-body';
    body.hidden = true;
    loose.append(trigger, body);
    host.appendChild(loose);
    foldOnActivate(trigger, body);
    return body;
  }

  /** A stored JSON string as the object it is, or the string when it is not one. */
  function maybeJson(text) {
    if (typeof text !== 'string') return text;
    try {
      return JSON.parse(text);
    } catch (e) {
      return text;
    }
  }

  /**
   * A stored message's thinking, wherever the server kept it: on the message as
   * reasoning_content, or -- when the provider's artifact is kept verbatim anyway --
   * only inside reasoning_details, as flat text blocks or as the reasoning items of a
   * verbatim replay block. The rule of utils/reasoning_artifacts.thinking_text; read
   * from reasoning_content alone, a third of the stored runs came back without their
   * thinking. `data` is the encrypted round-trip payload, never text.
   */
  function storedThinking(msg) {
    const own = msg.reasoning_content;
    if (typeof own === 'string' && own.trim()) return own;
    const texts = [];
    const items = [];
    const text = (v) => typeof v === 'string' && v.trim();
    for (const block of Array.isArray(msg.reasoning_details) ? msg.reasoning_details : []) {
      if (!block || typeof block !== 'object') continue;
      for (const item of Array.isArray(block.items) ? block.items : []) {
        if (!item || item.type !== 'reasoning') continue;
        for (const field of ['content', 'summary']) {
          for (const part of Array.isArray(item[field]) ? item[field] : []) {
            if (part && text(part.text)) items.push(part.text);
          }
        }
      }
      const value = block.text || block.summary;
      if (text(value)) texts.push(value);
    }
    return [...texts, ...items].join('\n\n');
  }

  /**
   * One LLM call of a session read back from disk, in the containers the live
   * run builds: its thinking and what it asked each tool, foldable as ever.
   *
   * Everything here was already on disk and simply never shown again -- which
   * is why switching sessions looked like the run had been erased. NOT
   * rebuilt, because the session does not carry it: the status LINE a plugin
   * reported ("Read README.md"). A replayed call is named by its tool instead,
   * which is the same shape a live call with no status scope gets.
   *
   * Goes through toolDetail, not past it: a second renderer beside the live one
   * drifts, and then the page shows two different truths about one run.
   */
  function replayStep(blk, msg, stepNo, resultFor) {
    const calls = msg.tool_calls || [];
    const thinking = storedThinking(msg);
    if (!calls.length && !thinking) return;   // a plain answer needs no step of its own
    const section = stepOf(blk, stepNo);
    if (!section) return;
    if (thinking) thinkingOf(section).textContent = thinking;
    // toolDetail files its block under the step the run is ON. Set here and put
    // back, because replaying is the one case where that is not "now" -- the
    // loop around this is synchronous, so nothing else reads it meanwhile.
    const was = blk.openStep;
    blk.openStep = stepNo;
    try {
      calls.forEach((tc) => {
        const fn = tc.function || {};
        const answer = resultFor.get(tc.id);
        // Keyed by the id the tool ran under, where the session kept it
        // (ChatMessage.tool_request_ids, stamped as the tool started): the runs the
        // call started carry it as their prefix, and hang from this block
        // (attachStoredSubRuns) -- also while the call still waits for them.
        const ranUnder = msg.tool_request_ids && msg.tool_request_ids[tc.id];
        const data = {action: fn.name || 'tool', request_id: ranUnder || tc.id || ''};
        toolDetail(blk, data, 'arguments', maybeJson(fn.arguments));
        if (answer) toolDetail(blk, data, 'result', maybeJson(answer.content));
      });
    } finally {
      blk.openStep = was;
    }
  }

  const activeOperations = new Map();
  const treeNodes = new Map(); // requestId -> { element, parentId, depth, children:Set }
  const lateRows = new Set();   // rows opened by live events released after their stream ended

  /**
   * The rows of a chat that was cleared for a session read back. Kept, a run followed
   * again after the viewer left its session and came back found its operations here,
   * updated the rows of the chat it left -- no longer in the page -- and its lines were
   * never seen.
   */
  function forgetRows() {
    activeOperations.clear();
    lateRows.clear();
    treeNodes.clear();
  }

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

  // A parent is always there by now: addStatusEvent puts up a virtual one for a line
  // whose parent has not been heard from yet.
  function registerNode(requestId, parentId, element, depthLevel) {
    treeNodes.set(requestId, { element, parentId, depthLevel, children: new Set() });
    const parentNode = parentId && treeNodes.get(parentId);
    if (parentNode) {
      parentNode.children.add(requestId);
      updateParentExpandButton(parentId);
      // Check full ancestor chain to determine visibility
      element.style.display = isAllAncestorsExpanded(requestId) ? 'block' : 'none';
    }
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

  function insertOperationHierarchically(container, operationDiv, requestId, parentId) {
    if (!parentId) {
      // A row with no parent goes at the end, which is to say in the order it arrived.
      //
      // There were thirty lines here that sorted root rows by the last segment of their
      // request id, and they never ran: the filter asked for `nodeInfo.depth` and
      // `nodeInfo.parent`, while registerNode stores `depthLevel` and `parentId`. Both
      // comparisons were against undefined, so the list of roots was always empty and
      // every row was appended anyway.
      //
      // Removed rather than repaired, because repairing it would START sorting, and
      // sorting is wrong here: these are status lines, and their order is the order
      // things happened. It would also sort by a segment that carries nothing to sort
      // by -- since the tree is forwarded (6b6a1348) the only rows reaching this branch
      // are the ones with no parent at all, such as the connection notice, whose id
      // ends in `_connection`, and a sub-run's own lines, whose run is their container.
      container.appendChild(operationDiv);
      return;
    }
    
    // Find parent element -- in THIS container. Out of it, insertAfter.nextSibling is a
    // node of another container and insertBefore throws NotFoundError, which takes the
    // stream down with it.
    //
    // That is the everyday case now, not an edge one: every operation of a run is a
    // child of the run (6b6a1348 forwards the tree), and the run's own node stands in
    // the section of whatever arrived first -- its `started`, in the run's section --
    // while its children are spread across the section of each call.
    const parentNode = treeNodes.get(parentId);
    if (parentNode && parentNode.element && container.contains(parentNode.element)) {
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

  function addStatusEvent(container, ev, view = null) {
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
    // One level off what the server counts, because the server counts from the RUN and
    // every operation of a run is a child of it: taken literally, the coordinator, the
    // worker and every tool scope would be indented by one and wear a connector, which
    // says "this ran under something else" about every line in the chat. Shifted down,
    // the indent means what it looks like it means -- the run's own work sits flat, and
    // only what a sub-agent does sits under the call that spawned it.
    //
    // Counted from the run whose view the line is shown in: a sub-agent's run is
    // already indented where it hangs, and its own work sits flat inside it.
    const base = view ? view.depthLevel : 0;
    const depthLevel = Math.max(0, (treeInfo.depth_level || 0) - 1 - base);
    // A line whose tree parent is the run it is shown in is one of that run's own: the
    // run's view is its container. Registered under the run's id instead, it would be a
    // child of the CALL's row where the run's id is the call's (an agent called as a
    // tool), and that row's arrow would hide the run's lines and leave the rest of it.
    const parentId = view && view.runId && treeInfo.parent_id === view.runId ? null : (treeInfo.parent_id || null);
    
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
    
    let row = null;  // the row this line went to, for a question's buttons (syncQuestionActions)
    if (ev.phase === 'start') {
      if (activeOperations.has(operationKey)) {
        const existing = activeOperations.get(operationKey);
        const msg = existing.querySelector('.progress-message');
        const time = existing.querySelector('.progress-time');
        if (msg) msg.textContent = ev.message || 'Starting...';
        if (time) time.textContent = formatTime(ev.timestamp);
        row = existing;
      } else {
        const operationDiv = createTreeOperationDiv(operationKey, ev, depthLevel, parentId);
        
        // Insert at correct hierarchical position
        insertOperationHierarchically(container, operationDiv, requestId, parentId);
        
        activeOperations.set(operationKey, operationDiv);
        // Register node with requestId (if available) for tree structure
        if (requestId) {
          registerNode(requestId, parentId, operationDiv, depthLevel);
        }
        row = operationDiv;
      }
    } else if (ev.phase === 'progress') {
      let operationDiv = activeOperations.get(operationKey);
      // If progress arrives before start, create a row from this progress event
      if (!operationDiv) {
        operationDiv = createTreeOperationDiv(operationKey, ev, depthLevel, parentId);
        
        // Insert at correct hierarchical position
        insertOperationHierarchically(container, operationDiv, requestId, parentId);
        
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
      }
      row = operationDiv;
    } else if (ev.phase === 'end') {
      let operationDiv = activeOperations.get(operationKey);
      // If END arrives before START was processed, create the operation now
      if (!operationDiv) {
        operationDiv = createTreeOperationDiv(operationKey, ev, depthLevel, parentId);
        insertOperationHierarchically(container, operationDiv, requestId, parentId);
        if (requestId) {
          registerNode(requestId, parentId, operationDiv, depthLevel);
        }
      }
      
      const iconSpan = operationDiv.querySelector('.progress-icon');
      const messageSpan = operationDiv.querySelector('.progress-message');
      const timeSpan = operationDiv.querySelector('.progress-time');
      if (iconSpan) iconSpan.innerHTML = '<div class="checkmark">✓</div>';
      if (messageSpan) messageSpan.textContent = ev.message || 'Completed';
      if (timeSpan) timeSpan.textContent = formatTime(ev.timestamp);
      operationDiv.classList.add('completed');
      activeOperations.delete(operationKey);
      // Keep tree structure intact for folding - don't clean up completed operations
      row = operationDiv;
    } else if (ev.phase === 'error') {
      let operationDiv = activeOperations.get(operationKey);
      // If ERROR arrives before START was processed, create the operation now
      if (!operationDiv) {
        operationDiv = createTreeOperationDiv(operationKey, ev, depthLevel, parentId);
        insertOperationHierarchically(container, operationDiv, requestId, parentId);
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
      row = operationDiv;
    }
    syncQuestionActions(row, ev);
  }

  /**
   * The answer a question needs, on the row that asks it.
   *
   * A run asks the person watching it with a status line whose meta names the question
   * and where the answer goes: `meta.tool_approval` (a pre_tool_call hook asks whether a
   * call may run) or `meta.ask_user` (the model asks something). The box stands while the
   * row asks; the row's last line (end or error: answered, denied, timed out) takes it
   * down, in every tab that shows the run. The asker sends the question again now and
   * then, so a page reloaded mid-question gets its box back with the next line.
   *
   * The answer goes to the URL the line names -- a plugin's answer route on this server,
   * nothing else -- with the page's own sign-in, as every other request of the chat.
   */
  function syncQuestionActions(row, ev) {
    if (!row) return;
    const open = row.querySelector(':scope > .question-actions');
    if (ev.phase === 'end' || ev.phase === 'error') {
      if (open) open.remove();
      return;
    }
    if (open || !ev.meta) return;
    for (const [key, build] of [['tool_approval', approvalBox], ['ask_user', askUserBox]]) {
      const ask = ev.meta[key];
      // A plugin's answer route and nothing else: `/plugins/../api/…` would reach any route.
      if (!ask || typeof ask.id !== 'string' || typeof ask.answer_url !== 'string'
          || !/^\/plugins\/[A-Za-z0-9_-]+\/answer$/.test(ask.answer_url)) continue;
      row.appendChild(build(ask));
      return;
    }
  }

  /**
   * Post the answer to a question. The controls stay off once it was taken, or once
   * nothing waits for it any more (404); any other refusal leaves them for a second try.
   */
  function sendAnswer(ask, body, controls, note, label) {
    controls.forEach((c) => { c.disabled = true; });
    note.textContent = 'Sending…';
    return Promise.resolve()
      .then(() => postJSON(ask.answer_url, Object.assign({ question_id: ask.id }, body)))
      .then(() => { note.textContent = `Answered: ${label}`; }, (err) => {
        note.textContent = `Not taken: ${(err && err.message) || String(err)}`;
        if (!err || err.status !== 404) controls.forEach((c) => { c.disabled = false; });
      });
  }

  /** tool_approval's box: the call's arguments, a reason for Deny, the decisions offered. */
  function approvalBox(ask) {
    const box = document.createElement('div');
    box.className = 'question-actions approval-actions';
    if (typeof ask.warning === 'string' && ask.warning) {
      // what allowing gives up: a spawn whose calls no approval reaches
      const warning = document.createElement('div');
      warning.className = 'approval-warning';
      warning.textContent = ask.warning;
      box.appendChild(warning);
    }
    if (ask.arguments_cut) {
      // every argument is there by name; only long values lost their middle
      const warn = document.createElement('div');
      warn.className = 'approval-cut';
      warn.textContent = 'Long values are shortened in the middle -- check what the call writes before you allow it.';
      box.appendChild(warn);
    }
    if (ask.arguments) {
      const args = document.createElement('pre');
      args.className = 'approval-arguments';
      args.textContent = ask.arguments;  // what the model chose: data, never markup
      box.appendChild(args);
    }
    const bar = document.createElement('div');
    bar.className = 'approval-bar';
    const reason = document.createElement('input');
    reason.type = 'text';
    reason.className = 'pk-input approval-reason';
    reason.maxLength = 1000;
    reason.placeholder = 'Why not (sent to the agent with Deny)';
    const note = document.createElement('span');
    note.className = 'approval-note';
    // the answers the question offers: a script is allowed call by call, never for the session
    const offered = Array.isArray(ask.decisions) ? ask.decisions : ['allow_once', 'allow_session', 'deny'];
    const choices = [['allow_once', 'Allow once'], ['allow_session', 'Allow for this session'], ['deny', 'Deny']]
      .filter(([decision]) => offered.includes(decision));
    const buttons = choices.map(([decision, label]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `pk-btn pk-btn--sm${decision === 'deny' ? ' pk-btn--danger' : (decision === 'allow_once' ? ' pk-btn--primary' : '')} approval-${decision}`;
      button.textContent = label;
      button.addEventListener('click', () => sendAnswer(
        ask, { decision, reason: reason.value || '' }, [...buttons, reason], note, label));
      bar.appendChild(button);
      return button;
    });
    bar.append(reason, note);
    box.appendChild(bar);
    return box;
  }

  /**
   * ask_user's box: the model's question, its options -- a click sends one, boxes to tick
   * where several may be picked -- and a field for an answer in one's own words, which
   * goes along with a picked option too.
   */
  function askUserBox(ask) {
    const box = document.createElement('div');
    box.className = 'question-actions ask-user-actions';
    const question = document.createElement('div');
    question.className = 'ask-user-question';
    question.textContent = typeof ask.question === 'string' ? ask.question : '';  // the model's text: data, never markup
    box.appendChild(question);
    const options = Array.isArray(ask.options) ? ask.options.filter((o) => typeof o === 'string') : [];
    const multi = ask.multi_select === true && options.length > 0;
    const text = document.createElement('input');
    text.type = 'text';
    text.className = 'pk-input ask-user-text';
    text.maxLength = 4000;
    text.placeholder = options.length ? 'Or answer in your own words' : 'Your answer';
    const note = document.createElement('span');
    note.className = 'approval-note';
    const controls = [text];
    const ticks = [];
    const send = (choices, label) => sendAnswer(ask, { choices, text: text.value.trim() }, controls, note, label);
    if (options.length) {
      const list = document.createElement('div');
      list.className = 'ask-user-options';
      options.forEach((option) => {
        if (multi) {
          const label = document.createElement('label');
          label.className = 'ask-user-option';
          const tick = document.createElement('input');
          tick.type = 'checkbox';
          tick.value = option;
          const caption = document.createElement('span');
          caption.textContent = option;
          label.append(tick, caption);
          ticks.push(tick);
          controls.push(tick);
          list.appendChild(label);
        } else {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'pk-btn pk-btn--sm ask-user-option';
          button.textContent = option;
          button.addEventListener('click', () => {
            const typed = text.value.trim();   // it goes along; the note says so, as for Send
            return send([option], [option].concat(typed ? [typed] : []).join(', '));
          });
          controls.push(button);
          list.appendChild(button);
        }
      });
      box.appendChild(list);
    }
    const bar = document.createElement('div');
    bar.className = 'approval-bar';
    const submit = document.createElement('button');
    submit.type = 'button';
    submit.className = 'pk-btn pk-btn--sm pk-btn--primary ask-user-send';
    submit.textContent = 'Send';
    submit.addEventListener('click', () => {
      const picked = ticks.filter((t) => t.checked).map((t) => t.value);
      const typed = text.value.trim();
      if (!picked.length && !typed) {
        note.textContent = multi ? 'Tick an option or write an answer.' : 'Write an answer first.';
        return undefined;
      }
      return send(picked, picked.concat(typed ? [typed] : []).join(', '));
    });
    // Enter sends what is typed (and ticked), as the Send button does
    text.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' || e.isComposing) return;
      e.preventDefault();
      if (!submit.disabled) submit.click();
    });
    controls.push(submit);
    bar.append(text, submit, note);
    box.appendChild(bar);
    return box;
  }

  /**
   * A tool call that failed without a line of its own: a pre_tool_call hook blocked it,
   * or the framework refused it (unknown tool, arguments that are no JSON). Such a call
   * opens no status scope and sends no tool_call event, so without this line it was
   * nowhere on the page while the run went on -- only a reload showed it, as a stored
   * result. A call that ran and raised has its row already (its scope ended in an
   * error, and the event names the row's request id): nothing is added to it.
   */
  function toolErrorLine(view, data) {
    if (data.request_id && chatContainer
        && chatContainer.querySelector(`.operation-progress[data-request-id="${CSS.escape(data.request_id)}"]`)) return;
    const host = statusBodyFor(view);
    if (!host) return;
    const line = createTreeOperationDiv('', {
      server: data.tool || 'tool', message: `${data.blocked ? 'blocked' : 'failed'}: ${data.error || ''}`,
      timestamp: new Date().toISOString(),
    }, 0, null);
    const icon = line.querySelector('.progress-icon');
    if (icon) icon.innerHTML = '<div class="error-mark">✕</div>';
    line.classList.add('error');
    // why a hook stopped the call is what the reader needs: shown whole, not cut to a row
    if (data.blocked) line.classList.add('blocked');
    host.appendChild(line);
  }

  function formatTime(ts) {
    try {
      return new Date(ts).toLocaleTimeString();
    } catch (e) {
      return ts;
    }
  }

  //: How long a run that ends is watched for a successor, and how patiently.
  //: A woken run is a process that has to come up first -- measured on a real
  //: wake: its first tool result 23 s after the run that woke it ended. The
  //: steps get further apart so the common case, no successor at all, costs
  //: seven requests rather than thirty.
  const SUCCESSOR_WAITS = [1000, 2000, 3000, 5000, 8000, 15000, 25000];
  //: How often a session held by ANOTHER process is asked about. No cap on
  //: the number of those: a woken run is a whole turn, and while the lock
  //: file says it is working, waiting is the right thing to do.
  const ELSEWHERE_WAIT = 4000;
  let successorWatch = 0;

  /**
   * A run ended; its SESSION may keep working. Watch for the run that follows.
   *
   * The page follows a RUN, not a session: one EventSource on one request_id.
   * A wake starts a NEW run, in a process of its own, under an id this page
   * never hears about -- so the turn lands in the session file and is seen
   * only after a reload. Measured on session jrbqugnco7: 13 messages on disk,
   * six of them on screen, including the whole final answer.
   *
   * followRunOfOpenSession already knows how to find and join the run a
   * session has. It was simply never asked again once a run was over.
   *
   * ponytail: a bounded poll, not a subscription. The ceiling is honest -- a
   * successor that takes longer than the waits above to register is missed,
   * and its turn is then seen on the next load, as before. A push would mean
   * the run's end carrying "a wake was started for this session", which is a
   * change across app.py, the event payload and session_presence.
   */
  function watchForASuccessorRun() {
    const session = currentSessionId;
    if (!session) return;
    const token = ++successorWatch;   // a newer watch, a new run or a new session wins
    let step = 0;
    let sawItWorking = false;
    // A run followed again ends the watch -- not the stream of the run that ended: it is
    // still open while that run saves, and asking about it would end the watch on the
    // very first tick. (followRunOfOpenSession does not attach while it is open.)
    const tick = async () => {
      if (token !== successorWatch || currentSessionId !== session) return;
      if (chatModule.activeRun()) return;   // something is being followed again
      const elsewhere = await sessionIsWorkingElsewhere(session);
      // Asked again after the answer: a run of this page's own may have started
      // while it was on its way, and an answer from before it would put the mark
      // back under that run, where nothing takes it down again.
      if (token !== successorWatch || currentSessionId !== session) return;
      if (chatModule.activeRun()) return;
      if (elsewhere || (elsewhere === null && sawItWorking)) {
        // null is "could not ask", which is not "not working". Read as an answer
        // it would end the wait on one bad request and lose the very turn this
        // watch exists for; the mark stands and the next tick asks again.
        sawItWorking = true;
        showWorkingElsewhere(true);
        setTimeout(tick, ELSEWHERE_WAIT);          // no cap while it IS working
        return;
      }
      if (sawItWorking) {
        // It let go. Its whole turn is in the session file and in no stream
        // this process can reach, so the only way to show it is to read the
        // session again -- which now brings the run with it, tool calls and
        // thinking included.
        showWorkingElsewhere(false);
        window.sessionManager?.loadSession?.(session);
        return;
      }
      chatModule.followRunOfOpenSession?.({ session_id: session }, {});
      if (step < SUCCESSOR_WAITS.length) setTimeout(tick, SUCCESSOR_WAITS[step++]);
    };
    setTimeout(tick, SUCCESSOR_WAITS[step++]);
  }

  /**
   * Is this session being worked on by a process this one cannot reach?
   *
   * A woken run is `agent-cli run --woken`, started by presence as a process of
   * its own. Its events never reach the API: they go to its own status bus, and
   * GET /events does not know its request id. /api/sessions/active reports it
   * from the lock files with `elsewhere`, deliberately without a request id --
   * there is nothing to attach to, only something to wait for.
   *
   * Three answers, not two: null is "could not ask". A run woken into another
   * process is followed by nothing else, so a poll that fails must not be read
   * as "it is done" -- that would read the session back mid-turn and then stop
   * asking, which looks exactly like the bug this is here to fix.
   */
  async function sessionIsWorkingElsewhere(sessionId) {
    try {
      const response = await fetch(`/api/sessions/active?ids=${encodeURIComponent(sessionId)}`,
        { credentials: 'include', signal: AbortSignal.timeout(10000) });
      // A 502 and a dropped connection are the same thing here -- the question
      // went unanswered -- so they leave by the same door. Two returns would be
      // two decisions, and a probe caught the second one going unmeasured.
      if (!response.ok) throw new Error(`the poll answered ${response.status}`);
      return !!(await response.json()).active?.[sessionId]?.elsewhere;
    } catch (error) {
      console.warn('[chat_module] Could not ask whether the session works elsewhere:', error);
      return null;
    }
  }

  /** Say that the session is busy somewhere this page cannot follow. */
  function showWorkingElsewhere(on) {
    const bar = document.getElementById('chat');
    if (!bar) return;
    let note = bar.querySelector(':scope > .working-elsewhere');
    if (!on) {
      note?.remove();
      return;
    }
    if (note) return;
    note = document.createElement('div');
    note.className = 'working-elsewhere';
    // Not "was woken": a wake is the usual reason, but an agent-cli started by
    // hand holds the session the same way, and the lock does not say which.
    note.textContent = 'This session is being worked on in another process — '
      + 'its answer appears here when it is done.';
    bar.appendChild(note);
    scrollBottom();
  }

  /**
   * Every scope still open when the run ends, said so on its row.
   *
   * A scope is closed by its OWN `end` phase, and some never get one: a
   * sub-agent spawned by this run keeps working in a process of its own, and
   * the run that spawned it goes to sleep meanwhile. Its row then kept the
   * last `progress` message forever, which reads exactly like a hang.
   *
   * NOT marked completed -- that would be a lie about work that may still be
   * running, and the checkmark is the one thing on this line a reader trusts.
   * The row says what it last said, plus that nobody is watching it any more.
   */
  function markOpenScopesUnfinished(keys = [...activeOperations.keys()]) {
    keys.forEach((key) => {
      const operationDiv = activeOperations.get(key);
      if (!operationDiv) return;
      activeOperations.delete(key);
      // a question whose last line never came waits for nobody any more
      const asking = operationDiv.querySelector(':scope > .question-actions');
      if (asking) asking.remove();
      const iconSpan = operationDiv.querySelector('.progress-icon');
      const line = operationDiv.querySelector('.progress-line');
      if (iconSpan) iconSpan.innerHTML = '<div class="open-mark">⋯</div>';
      operationDiv.classList.add('unfinished');
      if (line && !line.querySelector('.progress-open-note')) {
        const note = document.createElement('span');
        note.className = 'progress-open-note';
        note.textContent = 'still running when the run ended';
        line.appendChild(note);
      }
    });
  }

  // The EventSource of a run the chat follows again after a reload (followRun)
  let currentEventSource = null;
  // The fetch stream (POST /events or POST /run) the chat follows: one object per stream, null when none. Together
  // with currentEventSource it gates hasActiveRequest(). A stream the chat lets go of (letGoOfFinishedRun) reads on
  // to its end with its events ignored: closing it would cut the end of its request short (a run with files saves
  // there, a message's request saves once more and releases the run). `ended`: it was read to its end.
  let followedStream = null;

  // While init checks whether a run of this tab is still going, the composer is held: a
  // message would start a second run beside it (see followRun).
  let holding = false;
  // The session shown is a sub-agent's the selector cannot pick: the composer stays off.
  let readOnlyShown = false;

  // How the viewer wants runs shown (Settings -> Chat), kept on their account and read
  // from GET /auth/me/preferences, which always answers with every key. These are what
  // the page does without an answer: a server without accounts has nowhere to keep them.
  const CHAT_PREFS_DEFAULTS = Object.freeze({
    fold_steps: 'at_end',     // at_end | at_next_step | never
    thinking: 'collapsed',    // collapsed | expanded
    sub_agents: 'expanded',   // expanded | collapsed
    sub_agent_output: 'collapsed',   // collapsed | expanded
  });
  let chatPrefs = { ...CHAT_PREFS_DEFAULTS };

  /**
   * Take the viewer's preferences, and show what is on the page their way.
   *
   * What the viewer opened or closed by hand stays as it is. A run still working
   * goes on by the new rule from its next step.
   */
  function usePreferences(preferences) {
    chatPrefs = { ...CHAT_PREFS_DEFAULTS, ...((preferences && preferences.chat) || {}) };
    if (!chatContainer) return;
    // Finished runs, read back or live: the answer the preferences arrive after -- a
    // session restored before they were loaded -- must not keep the default's folding.
    chatContainer.querySelectorAll('.steps[data-settled]').forEach(foldSettled);
    chatContainer.querySelectorAll('.step-thinking:not([data-touched])').forEach((thinking) => {
      setFolded(thinking.querySelector('.thinking-toggle'), thinking.querySelector('.thinking-content'),
        chatPrefs.thinking === 'collapsed');
    });
    chatContainer.querySelectorAll('.sub-run:not([data-touched])').forEach((element) => {
      setFolded(element.querySelector(':scope > .sub-run-header'), element.querySelector(':scope > .sub-run-body'),
        chatPrefs.sub_agents === 'collapsed');
    });
    chatContainer.querySelectorAll(
        '.sub-run-body > .container-section:not([data-touched]):not([data-reason])').forEach((section) => {
      const body = section.querySelector(':scope > .assistant-text');
      if (!body) return;
      const open = chatPrefs.sub_agent_output !== 'collapsed';
      body.style.display = open ? 'block' : 'none';
      markOpen(section, open);
    });
  }

  /** The viewer's preferences from their account; without one the defaults stand. */
  async function loadPreferences() {
    try {
      const response = await fetch('/auth/me/preferences', { credentials: 'include' });
      // 404: a server without accounts; 401: nobody signed in. Neither is an error here.
      if (response.ok) usePreferences(await response.json());
    } catch (error) {
      console.warn('[chat_module] Could not load the display preferences:', error);
    }
  }

  // Set when a user message was appended to the RUNNING request. The stream's
  // block is rebound to a fresh one only when the NEXT step actually starts —
  // rebinding at append time would hijack the still-streaming current step
  // (thinking_delta re-renders the full accumulated text into whatever block
  // blk points at), teleporting the in-flight answer below the injected
  // message and letting the post-drain step overwrite it.
  // 'message' for a message appended to the run, 'note' for a note written meanwhile
  // (runGoesOnBelow): the server answers the one and never sees the other.
  let pendingAppendRebind = false;
  // A message the run took after its last step had begun: its answer is not the reply to it.
  const LATE_MESSAGE = 'The run had already answered when your message arrived -- '
    + 'it is kept in the session, and the next run answers it.';

  // Move the live stream to a fresh assistant block (appended at the end of the
  // chat, i.e. below any injected user message) by mutating the SAME blk object
  // the stream handlers hold — object identity is what makes the in-place
  // rebind work (see the 'continuation' handler and the mid-run append flow).
  function rebindLiveBlock(blk) {
    const newBlk = addAssistantBlock(chatContainer);
    blk.pastSteps.push(blk.steps);
    blk.row = newBlk.row;
    blk.box = newBlk.box;
    blk.t = newBlk.t;
    blk.steps = newBlk.steps;
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
  // Sessions loaded into the chat so far: what the server says about a session asked for
  // before the latest load is not acted on (attachRunOfOpenSession).
  let sessionLoads = 0;
  // The session loaded last, as the server sent it (live_events_seen for a run followed from it).
  let shownSession = null;
  // The load whose run a reload has joined itself (followRun): that load's own join stays out.
  let joinedLoad = null;

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

  // A reload of this tab follows the stored run no more. Only that: whether the run itself is
  // over is a different question, and the "asked to stop" mark answers it for as long as the
  // run may still take a message. Unmarking here would tie the mark to the viewer's navigation
  // -- leave the session and come back, and a run being cancelled would take a message again.
  function clearStoredRun() {
    sessionStorage.removeItem(RUN_KEY);
    sessionStorage.removeItem(RUN_SESSION_KEY);
  }

  // A run the chat has asked the server to end -- by Stop, or by deleting its session. It ends in its own
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

  // Stop, ready to be clicked -- whatever an earlier stop left on it.
  function readyStop() {
    stopBtn.disabled = false;
    stopBtn.setAttribute('title', 'Stop');
    stopBtn.setAttribute('aria-label', 'Stop');
    stopBtn.classList.remove('cancelling', 'cancelled', 'cancel-failed');
  }

  // The controls go back to idle: a message starts a run, and Stop is reset for it.
  function idleControls() {
    runActive = false; updateActionButton();
    readyStop();
  }

  /**
   * Stop the run the chat follows: ask the server to cancel it. Its stream brings its end, as
   * it would without. Before its start the run has no id to cancel it by: the stop waits for
   * the start (handleSSEEvent), and the button says it is on its way meanwhile.
   */
  async function stopRun(clicked) {
    // immediate feedback: the icon stays, label and tooltip change
    stopBtn.setAttribute('title', 'Canceling');
    stopBtn.setAttribute('aria-label', 'Canceling');
    stopBtn.disabled = true;
    stopBtn.classList.add('cancelling');
    const requestId = clicked.requestId;
    if (!requestId) {
      clicked.stopWhenStarted = true;
      return;
    }
    markStopping(requestId);
    let over = false;
    try {
      over = await cancelRun(requestId, { force: false });
    } catch (error) {
      console.error('Failed to cancel request:', error);
    }
    // the run clicked on: an answer that comes after another run has taken the chat leaves that one alone
    if (run !== clicked || !chatModule.hasActiveRequest()) return;  // the run has ended meanwhile, and its controls with it
    const outcome = over ? 'Done' : 'Failed';
    stopBtn.setAttribute('title', outcome);
    stopBtn.setAttribute('aria-label', outcome);
    stopBtn.classList.remove('cancelling');
    stopBtn.classList.add(over ? 'cancelled' : 'cancel-failed');
    // Stop again after a moment, while the run's stream is still open
    setTimeout(() => {
      if (run === clicked && chatModule.hasActiveRequest()) readyStop();
    }, 2000);
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
  // until then. The fetch stream reads on to its end, its events ignored; a /run streams its agent inline, and a
  // reader that stops reading cuts the run short.
  //
  // Its stream is NOT closed -- neither the EventSource of a run followed again nor the fetch stream of one this
  // chat started: it still brings the run's `end`, and with it the refresh that puts the run's save in the session
  // list. Coming back to the session meanwhile joins the run again from where that load stands, as the answered
  // run it is (attachRunOfOpenSession); every reader of a run gets every event.
  function letGoOfFinishedRun() {
    if (!chatModule.hasActiveRequest() || !run.over) return;
    followedStream = null;
    currentEventSource = null;
    endRun();
  }

  /**
   * The viewer opens another session while this run is STILL GOING: the chat stops
   * following it. Nothing is cancelled -- the run owns its session on the server and
   * works on; only this chat stops watching.
   *
   * Unlike letting go of a finished run, the connection is CLOSED, not merely
   * ignored: a run can go on for hours, and a stream nobody looks at would hold one
   * of the few connections a browser keeps to a server all that time. Coming back to
   * the session loads it again and joins the run from there.
   *
   * A new chat's run too: its session reaches the list (which is read from disk)
   * only with the run's first save, so the session pane is told to look for a session
   * it does not know (awaitListing) -- read to its end instead, it held the connection
   * all the same. Not before the server has named a run (no start so far): its stream
   * is read on for the start (namedAfterLettingGo), which a Stop clicked before it
   * waits for, and closed there.
   *
   * A reload of this tab does NOT follow it: showing another session lets the stored
   * run go (leaveLostRun), or the reload would open the session left behind rather
   * than the one on screen. Coming back to it asks the server instead.
   */
  function letGoOfRunningRun() {
    if (!chatModule.hasActiveRequest() || run.over) return;
    const stream = followedStream;
    followedStream = null;
    const source = currentEventSource;
    currentEventSource = null;
    if (run.requestId) {
      try { stream?.stop?.(); } catch { /* a stream already finishing needs no stopping */ }
      window.sessionManager.awaitListing(run.sessionId);   // if the list does not have it yet
    } else if (stream) {
      // Read on for the start that names it (readEvents): a Stop clicked before it is sent then.
      stream.untilStart = { cancel: Boolean(run.stopWhenStarted) };
    }
    try { source?.close(); } catch { /* same */ }
    endRun(true);
  }

  // A run let go of before its start, now named by it: a Stop clicked meanwhile is sent, and
  // the stream closed -- coming back to its session joins the run; a session the list does
  // not have yet (a new chat's) is looked for until it has.
  function namedAfterLettingGo(stream, start) {
    const { cancel } = stream.untilStart;
    stream.untilStart = null;
    if (cancel && start.request_id) {
      markStopping(start.request_id);
      cancelRun(start.request_id, { force: false }).catch((error) => console.error('Failed to cancel request:', error));
    }
    try { stream.stop?.(); } catch { /* a stream already finishing needs no stopping */ }
    window.sessionManager.awaitListing(start.session_id);
  }

  /**
   * Read a run's SSE response to its end, handing each event to `onEvent` while the chat
   * follows `stream`; a stream let go of is read on with its events dropped. Resolves
   * whether the server said anything at all -- its first line is a comment -- which is
   * how an answer of 200 with nothing in it shows. `stream.ended` once the server closed it.
   */
  async function readEvents(response, stream, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let heard = false;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        stream.ended = true;
        return heard;
      }
      if (followedStream !== stream && !stream.untilStart) continue;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();  // an incomplete line waits for the rest
      for (const line of lines) {
        if (line.startsWith(':')) {
          heard = true;  // a comment: the connection is up, or still up (keepalive)
          continue;
        }
        if (!line.startsWith('data:')) continue;  // `event:` lines and the blank one between events
        const json = line.substring(5).trim();
        if (!json) continue;
        try {
          const data = JSON.parse(json);
          if (followedStream === stream) onEvent(data);
          else if (stream.untilStart && data.type === 'start') namedAfterLettingGo(stream, data);
        } catch (e) {
          console.error('Failed to parse SSE data:', e);
        }
      }
    }
  }

  // A lasting row about the run's connection in the block's status (a status without a
  // phase renders nothing; the synthetic request_id keeps it off the run's own row).
  function connectionNotice(blk, message) {
    // The run's section, not a call's: a lost connection is the run's business, and the
    // call that happened to be open when the line dropped did not cause it.
    const section = stepOf(blk, RUN_SECTION);
    addStatusEvent(section && section.querySelector('.status-body'), {
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

  /**
   * What a run says about its own work, drawn into its own view.
   *
   * One renderer for the run the chat follows and for every run started under it:
   * a second one beside it would drift, and the page would then tell two different
   * stories about the same kind of step. What only the followed run has -- the
   * controls, the request id, a message appended mid-run -- stays in handleSSEEvent.
   */
  function renderRunEvent(view, data) {
    // A page that joined mid-call (a reload, a stored run taken over) saw no marker open
    // the call in flight: an event that names its step opens it -- forward only, so a
    // stream behind what was read changes nothing, and a run followed from its start
    // has had its marker open it already.
    if (data.step > (view.openStep || 0)) view.openStep = data.step;
    switch (data.type) {
      case 'reasoning_delta': {
        // The model's actual reasoning, and the only place it is shown at all:
        // neither the Response box nor Status carries a word of it. Mind the
        // names -- `thinking_delta` below is the ANSWER stream; this one is the
        // thinking, and it went unhandled here, so every token of it was dropped.
        //
        // Appended as a text node rather than `textContent +=`, which re-reads
        // and rewrites the whole box per delta -- a run reasons in hundreds of
        // them.
        // A step the session read back already holds whole (a stored run its live
        // events took over): its tail must not come twice.
        if (view.storedStep && data.step <= view.storedStep) break;
        const section = stepOf(view, data.step);
        if (section) thinkingOf(section).appendChild(document.createTextNode(data.delta || ''));
        break;
      }
      case 'thinking_delta':
        // The answer, streaming: the whole of it so far, with a cursor. One without it
        // was superseded in the server's buffer by the next, which follows.
        if (data.accumulated === undefined) break;
        view.streamStep = data.step;
        showSection(view.t);
        view.t.innerHTML = `<div class="response-text streaming">${formatTextWithLineBreaks(data.accumulated || '')}<span class="typing-cursor">|</span></div>`;
        break;
      case 'thinking_complete':
        // Final thinking event from streaming - remove cursor, keep content
        view.streamStep = null;
        if (data.assistant && data.assistant.content) {
          // Content was already displayed via thinking_delta
          // Now show final formatted content (HTML from format_output hook)
          showAnswer(view, data.assistant.content, data.content_format || 'text');
        }
        // Tool calls are not listed here: Status carries every one of them.
        break;
      case 'thinking':
        if (data.assistant) {
          // The step's result. Its `assistant.content` is NOT written into the call's
          // section: it is the step's answer, which the Response box already shows --
          // and it arrives here past the format_output hook, so a `<pre>` rendered it
          // as literal `<p>…</p>` markup beside the rendered copy.
          //
          // Its tool_calls only NAME the call in the header. The lines themselves are
          // Status's, with the arguments and the outcome this listing dropped.
          view.streamStep = null;
          const chose = (data.assistant.tool_calls || [])
            .map((tc) => (tc.function || {}).name).filter(Boolean);
          const section = data.step ? stepOf(view, data.step) : null;
          if (section && chose.length) {
            section.querySelector('.step-note').textContent =
              `· ${chose.slice(0, 3).join(', ')}${chose.length > 3 ? ` +${chose.length - 3}` : ''}`;
          }
        } else if (data.step && !data.content) {
          // This marker, and only this one, opens a call's section: it is sent before
          // the LLM call. The simplified event at the END of a text-only step carries
          // `content` and NO step -- opening on that one would add an empty section
          // after the answer.
          view.openStep = data.step;
          openStep(view, data.step);
        }
        break;
      case 'tool_call':
        toolDetail(view, data, 'arguments', data.params, true);
        break;
      case 'tool_result':
        toolDetail(view, data, 'result', data.result, true);
        break;
      case 'tool_error':
        toolErrorLine(view, data);
        break;
      case 'final': {
        // The answer is here, so no call is in flight any more: what the run says while
        // it saves and runs its end hooks belongs to the run, not to its last call.
        view.openStep = null;
        // Only when nothing streamed: a streamed answer is already in the box, and
        // this is the same text -- as is one the session load showed (answerLoaded: a run
        // joined past its answer's stream). A reconnect's note in the box is no answer.
        if (!view.answerLoaded
            && (!view.t.innerHTML.trim() || view.t.querySelector(':scope > .reconnect-info'))) {
          showAnswer(view, data.summary || data.content || '', data.content_format || 'text');
        }
        settleView(view);
        break;
      }
    }
  }

  /** A run's answer in its Response box, rendered as the server formatted it. */
  function showAnswer(view, content, contentFormat) {
    showSection(view.t);
    view.t.innerHTML = `<div class="response-text">${formatContent(content, contentFormat)}</div>`;
    // Apply Prism.js syntax highlighting if available and content is HTML
    if (contentFormat === 'html' && typeof Prism !== 'undefined') {
      Prism.highlightAllUnder(view.t);
    }
  }

  /**
   * The row of an operation anywhere in this block, by its request id.
   *
   * Not a virtual parent: addStatusEvent stands one up, empty and unseen, for a parent id
   * no line has come from -- a sub-run's id, for lines of it that arrive before the run's
   * first event. Hung there, a run would sit inside the step of its predecessor instead
   * of beside it.
   */
  function rowOf(blk, requestId) {
    if (!requestId || !blk.box) return null;
    const selector = `.operation-progress:not(.virtual-parent)[data-request-id="${CSS.escape(requestId)}"]`;
    // A message appended mid-run moved the run on to a new block; the calls of its
    // earlier steps are in the steps it left behind (rebindLiveBlock).
    for (const within of [blk.box, ...blk.pastSteps]) {
      const row = within.querySelector(selector);
      if (row) return row;
    }
    return null;
  }

  /**
   * The view of a run started under the one the chat follows, made on its first event.
   *
   * Hung from the row of the call that started it (`spawned_by`), so it stays in the
   * step it was started from however late its events arrive: an async sub-agent is
   * waited for steps later, and its work used to be filed under whichever call was
   * open when it came in. A run whose id IS its call's (an agent called as a tool)
   * hangs from that call's own row; a retry of a sub-run, whose id extends the run it
   * retries, goes beside it.
   */
  function subRunView(blk, envelope) {
    // A stored box of it is in the tree already: taken over (handleSubRunEvent).
    for (const view of allSubRuns(blk)) {
      if (view.runId === envelope.run_id) return view;
    }
    const owner = statusViewFor(blk, envelope.spawned_by || envelope.run_id);
    const retried = /_minlen_[0-9a-zA-Z]+$/.test(envelope.run_id) && envelope.spawned_by
      && chatContainer.querySelector(`.sub-run[data-run-id="${CSS.escape(envelope.spawned_by)}"]`);
    // Its call: read back (a session opened mid-run), the call's block holds the runs it
    // started, and one it starts after the page read it goes there too; else the call's
    // row, in the followed block or in a stored run taken over. No row of its call to
    // hang from (its status line never came): the step of the run that started it, not
    // whichever step the followed run is on.
    const callAt = (id) => (id && chatContainer.querySelector(`.tool-detail[data-key="${CSS.escape(id)}"]`))
      || rowOf(blk, id) || rowOf(owner, id);
    const anchor = callAt(envelope.run_id) || callAt(envelope.spawned_by)
      || (retried && retried.parentElement) || statusBodyFor(owner);
    if (!anchor) return null;
    return makeSubRun(owner, anchor, envelope.run_id, envelope.depth_level || 0, envelope.agent);
  }

  /** A sub-run's box in `anchor`, with its own view, registered with the run that started it. */
  function makeSubRun(owner, anchor, runId, depthLevel, agent) {
    const element = document.createElement('div');
    element.className = 'sub-run';
    element.dataset.runId = runId;
    element.dataset.state = 'running';
    element.innerHTML = `
      <div class="sub-run-header">
        <span class="toggle-arrow">${kitIcon('chevron-right')}</span>
        <span class="sub-run-icon"><div class="spinner"></div></span>
        <span class="sub-run-agent"></span>
        <span class="sub-run-task pk-muted"></span>
      </div>
      <div class="sub-run-body"></div>`;
    element.querySelector('.sub-run-agent').textContent = agent || 'sub-agent';
    anchor.appendChild(element);

    const header = element.querySelector('.sub-run-header');
    const body = element.querySelector('.sub-run-body');
    foldOnActivate(header, body);
    setFolded(header, body, chatPrefs.sub_agents === 'collapsed');
    // Opened or closed by hand, it is the viewer's: a changed setting leaves it alone.
    const touch = () => { element.dataset.touched = 'true'; };
    header.addEventListener('click', touch);
    header.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') touch(); });

    const view = runView(body, runId, depthLevel);
    view.element = element;
    owner.subRuns.set(runId, view);
    subRunOf.set(element, view);
    gatherEarlyRows(view);
    return view;
  }

  /**
   * A run's lines that came before its box -- its start was before the page joined, so
   * its first word was a line, placed in the deepest run the page had then
   * (statusViewFor) -- into its box now, with the virtual parent they hang from. Not
   * those already in its box or in the box of a run under it.
   */
  function gatherEarlyRows(view) {
    const id = CSS.escape(view.runId);
    chatContainer.querySelectorAll(`.operation-progress.virtual-parent[data-request-id="${id}"], `
      + `.operation-progress[data-request-id^="${id}_"]`).forEach((row) => {
      const box = row.closest('.sub-run');
      if (box && (box === view.element || box.dataset.runId.startsWith(`${view.runId}_`))) return;
      const from = row.parentElement;
      statusBodyFor(view).appendChild(row);
      const left = from.closest('.step-section');
      if (left && left.dataset.step === RUN_SECTION && !from.children.length && !thinkingOf(left).textContent) left.remove();
    });
  }

  /** What a sub-run was asked, in its header. */
  function showSubRunTask(view, text) {
    const task = view.element.querySelector('.sub-run-task');
    task.textContent = text.length > 120 ? `${text.slice(0, 120)}…` : text;
    task.title = text;
  }

  // The stored sub-runs of the session on screen, each read from its sub-session once
  // its box comes near the chat's viewport (a pipeline's one call starts hundreds):
  // box -> {view, sessionId, runId}; run id -> its view, for a live run's events; each
  // sub-session read, and each session's listing of its sub-sessions, once per session
  // shown (a promise each).
  const subRunOf = new WeakMap();   // every sub-run box -> its view
  const storedSubRuns = new WeakMap();
  const storedRunViews = new Map();
  const storedSessions = new Map();
  const storedChildren = new Map();
  let storedWatch = null;
  let sessionGate = null;   // {waiting} while the session on screen lists its sub-sessions
  // A stored read live events may wait for gives up, or a stalled one would hold them for
  // good; given up, it has failed like any other. Generous: the browser queues what comes
  // into view together, and a big sub-session is formatted message by message.
  // The session left takes its reads with it: the browser's queue for this host is the
  // page's too.
  let storedReadsCut = new AbortController();
  const storedRead = (url) => getJSON(url, { signal: AbortSignal.any([storedReadsCut.signal, AbortSignal.timeout(120000)]) });

  /** Forget the stored sub-runs of the session that was on screen, and what waited for them. */
  function forgetStoredSubRuns() {
    storedReadsCut.abort();
    storedReadsCut = new AbortController();
    sessionGate = null;
    storedRunViews.clear();
    storedSessions.clear();
    storedChildren.clear();
    if (storedWatch) storedWatch.disconnect();
  }

  /** Read a stored sub-run once its box comes near the viewport -- never while folded away. */
  function watchStoredSubRun(element) {
    if (!storedWatch) {
      storedWatch = new IntersectionObserver((entries) => entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        storedWatch.unobserve(entry.target);
        loadStoredSubRun(storedSubRuns.get(entry.target));
      }), { root: scroller(), rootMargin: '200px 0px' });
    }
    storedWatch.observe(element);
  }

  /**
   * A stored run that is still working, as its live events arrive: its box, under the
   * call that started it, goes on with them -- registered with the run the chat
   * follows, so its status lines and its end find it -- instead of a second box.
   *
   * `live` says the stream has its ending now; a stored box it never reached is ended
   * by its own read. One the session already shows answered stays done: its end may
   * come live after the answer did.
   */
  function takeOverStoredRun(owner, view) {
    storedRunViews.delete(view.runId);
    view.live = true;
    if (view.element.dataset.state !== 'done') {
      view.element.dataset.state = 'running';
      view.element.querySelector('.sub-run-icon').innerHTML = '<div class="spinner"></div>';
    }
    owner.subRuns.set(view.runId, view);
    // Not read yet, or its read failed: what it did so far comes now, into the steps
    // before the live ones -- and the read's error is not its answer.
    const stored = view.element.dataset.stored;
    if (stored === 'pending' || stored === 'failed') {
      if (stored === 'failed') view.t.innerHTML = '';
      storedWatch.unobserve(view.element);
      loadStoredSubRun(storedSubRuns.get(view.element));
    }
    return view;
  }

  /**
   * The stored runs on the way to `requestId`, taken over by a live event of it --
   * outermost first, so each registers with the run above it. A run's first live word
   * may be a status line (a call's line comes before the call's own events), and a
   * helper at work means the run that waits for it is at work too: not across an
   * `_async_` level (its caller may be over) or a `_minlen_` one (a retry, beside the
   * run it retries).
   */
  function takeOverStoredPath(blk, requestId) {
    const path = [];
    for (let id = requestId; id; id = parentOf(id)) {
      path.unshift(id);
      if (/_(?:async|minlen)_[0-9a-zA-Z]+$/.test(id)) break;
    }
    path.forEach((id) => {
      const stored = storedRunViews.get(id);
      if (stored && stored.element.isConnected) takeOverStoredRun(statusViewFor(blk, parentOf(id) || id), stored);
    });
  }

  /**
   * The runs a session read back started, each under the call that started it.
   *
   * A sub-agent's run lives in a session of its own. The run's first message carries
   * its request id, the message with the call that started it the id that call's tool
   * ran under -- the prefix of the run's (ChatMessage.request_id, .tool_request_ids) --
   * and /children lists each sub-session's runs by that id. The header is made now;
   * what the run did is read from its session once it comes into view
   * (watchStoredSubRun).
   *
   * A run hangs under the nearest call read back whose id its own extends: the run of
   * an agent called as a tool is not kept, so what that agent started hangs under the
   * call to it. A run with no such call (compacted away, or older than the stamps) is
   * not shown. The live events of these runs wait for the listing (waitingFor).
   */
  async function attachStoredSubRuns(session, views) {
    if (!session || !Object.keys((session.metadata && session.metadata.sub_agents) || {}).length) return;
    const id = session.session_id;
    let listed;
    if (!storedChildren.has(id)) {
      storedChildren.set(id, storedRead(`/api/sessions/${encodeURIComponent(id)}/children`));
    }
    const listing = storedChildren.get(id);
    try {
      listed = await listing;
    } catch (error) {
      // Not kept: the next run of this sub-session to be read lists again.
      if (storedChildren.get(id) === listing) storedChildren.delete(id);
      console.warn('The sub-agents of this session could not be listed:', error);
      return;
    }
    const calls = new Map();
    views.forEach((view) => {
      if (!view.box.isConnected) return;   // the chat has moved on meanwhile
      view.box.querySelectorAll('.tool-detail[data-key]').forEach((el) => {
        if (!calls.has(el.dataset.key)) calls.set(el.dataset.key, { view, el });
      });
    });
    // The call whose id the run's extends: `X_003_sub_a` was started by X_003, and
    // `X_003_sub_a_minlen_1`, a retry of it, goes beside it under the same call.
    const callOf = (runId) => {
      for (let end = runId.lastIndexOf('_'); end > 0; end = runId.lastIndexOf('_', end - 1)) {
        const call = calls.get(runId.slice(0, end));
        if (call) return call;
      }
      return null;
    };
    const nodes = [...((listed && listed.sessions) || [])]
      .sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')));
    nodes.forEach((node) => (node.runs || []).forEach((runId) => {
      const call = callOf(runId);
      if (!call) return;
      if (chatContainer.querySelector(`.sub-run[data-run-id="${CSS.escape(runId)}"]`)) return;
      const view = makeSubRun(call.view, call.el, runId, depthOf(runId), node.agent_name);
      view.element.dataset.stored = 'pending';
      storedSubRuns.set(view.element, { view, sessionId: node.session_id, runId });
      storedRunViews.set(runId, view);
      watchStoredSubRun(view.element);
    }));
  }

  /**
   * One stored sub-run, read from its sub-session into its box: its steps, its answer,
   * its own sub-runs. Its live events wait until then (waitingFor).
   */
  async function loadStoredSubRun(stored) {
    stored.view.waiting = stored.view.waiting || [];
    try {
      await readStoredSubRun(stored);
    } finally {
      // the chat moved on meanwhile: what waited belongs to a block no longer shown
      if (stored.view.element.isConnected) release(stored.view);
      else stored.view.waiting = null;
    }
  }

  async function readStoredSubRun({ view, sessionId, runId }) {
    view.element.dataset.stored = 'loading';
    if (!storedSessions.has(sessionId)) {
      storedSessions.set(sessionId, storedRead(`/api/sessions/${encodeURIComponent(sessionId)}`));
    }
    const read = storedSessions.get(sessionId);
    let session;
    try {
      session = await read;
    } catch (error) {
      // Not kept: the next run of this sub-session to come into view reads it again.
      if (storedSessions.get(sessionId) === read) storedSessions.delete(sessionId);
      view.element.dataset.stored = 'failed';   // read again if its live events take it over
      if (!view.element.isConnected || view.live) return;   // the live events show it
      showReason(view.t);
      view.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks(`Could not be read: ${error.message}`)}</div>`;
      markSubRun(view, 'error');
      return;
    }
    if (!view.element.isConnected) return;   // the chat has moved on meanwhile
    view.element.dataset.stored = 'loaded';
    // Its messages: from the one that opened it to the one that opened the next run of
    // the same sub-agent.
    const messages = session.messages || [];
    const opens = (m) => !!m.request_id;
    const start = messages.findIndex((m) => opens(m) && m.request_id === runId);
    if (start < 0) {
      if (!view.live) markSubRun(view, 'unfinished');
      return;
    }
    const next = messages.findIndex((m, index) => index > start && opens(m));
    const own = messages.slice(start, next < 0 ? messages.length : next);
    showSubRunTask(view, messageText(own[0]));
    const answered = replayRun(view, own.slice(1));
    if (answered) {
      settleView(view);
      markSubRun(view, 'done');
    } else if (!view.live) {
      markSubRun(view, 'unfinished');
    }
    await attachStoredSubRuns(session, [view]);
  }

  /** Whether a conversation ends in an answer: an assistant turn with words and no tool calls. */
  function endsInAnswer(messages) {
    const last = messages?.[messages.length - 1];
    return last?.role === 'assistant' && Boolean(last.content) && !last.tool_calls?.length;
  }

  /**
   * What the tools answered to the calls of `messages[index]`, by call id: the answers that
   * follow it, up to the next LLM call. Not by id alone across the session -- a provider
   * may give every run's call the same id, and the first run then showed the last's answer.
   */
  function answersTo(messages, index) {
    const answers = new Map();
    for (let at = index + 1; at < messages.length && messages[at].role !== 'assistant'; at += 1) {
      if (messages[at].role === 'tool' && messages[at].tool_call_id) answers.set(messages[at].tool_call_id, messages[at]);
    }
    return answers;
  }

  /** A run's messages after its first, read back into its view: a step per LLM call, the answer in its box. */
  function replayRun(view, messages) {
    let stepNo = 0;
    let answered = false;
    messages.forEach((msg, index) => {
      // a hook's continuation after its last word: that word was an interim, the run goes on
      if (msg.role === 'user' && msg.injected_by) answered = false;
      if (msg.role !== 'assistant') return;
      // numbered as the server numbered it (ChatMessage.step), which counts a step that
      // stored nothing too; counted, in a session older than that
      stepNo = msg.step || stepNo + 1;
      replayStep(view, msg, stepNo, answersTo(messages, index));
      answered = !!msg.content && !(msg.tool_calls && msg.tool_calls.length);
      if (msg.content) showAnswer(view, msg.content, msg.content_format);
    });
    view.storedStep = stepNo;   // what the stream still sends of these steps is here already
    // Still working: its last step is the call in flight, open as a live run leaves it
    // until its answer -- the lines its tools still send go there, not to a "Run" section.
    if (!answered && !view.openStep && view.element.dataset.state === 'running') view.openStep = stepNo || null;
    return answered;
  }

  /**
   * The followed run is over, and so is every run under it the stream led -- once what
   * waits for a read has gone (waitingFor). A stored box the stream never took over is
   * ended by its own read.
   */
  function endSubRuns(owner) {
    if (!owner.element) {
      // the followed run: after the session's listing, and after the read of a stored
      // run outside its tree that holds live events (waitingFor) -- both bring runs it leads
      const reading = sessionGate && sessionGate.waiting ? sessionGate
        : [...storedRunViews.values()].find((stored) => stored.waiting && stored.waiting.length);
      if (reading) {
        reading.waiting.push(() => endSubRuns(owner));
        return;
      }
    }
    for (const view of owner.subRuns.values()) {
      if (view.waiting) {
        view.waiting.push(() => endSubRun(view));
        continue;
      }
      endSubRun(view);
    }
  }

  function endSubRun(view) {
    if (view.live || !view.element.dataset.stored) markSubRun(view, 'unfinished');
    endSubRuns(view);
  }

  /** Say on a sub-run's header how it ended. */
  function markSubRun(view, state) {
    if (view.element.dataset.state !== 'running') return;   // the first ending wins
    view.element.dataset.state = state;
    const marks = { done: '<div class="checkmark">✓</div>', error: '<div class="error-mark">✕</div>',
      unfinished: '<div class="open-mark">⋯</div>' };
    view.element.querySelector('.sub-run-icon').innerHTML = marks[state] || '';
  }

  /**
   * One event of a run started under the followed one (`sub_run`, relayed by the server).
   *
   * The envelope says which run and which call started it; the event inside is exactly
   * what that run would have streamed had the chat followed it directly.
   */
  function handleSubRunEvent(blk, envelope) {
    takeOverStoredPath(blk, envelope.run_id);
    const waiting = waitingFor(blk, envelope.run_id);
    if (waiting) {
      waiting.push(() => handleSubRunEvent(blk, envelope));
      return;
    }
    const ev = envelope.event || {};
    const view = subRunView(blk, envelope);
    if (!view) return;
    switch (ev.type) {
      case 'start':
        showSubRunTask(view, String(ev.task || ''));
        break;
      case 'final':
        renderRunEvent(view, ev);
        markSubRun(view, 'done');
        break;
      case 'error':
        showReason(view.t);
        view.t.innerHTML = `<div class="response-text error">${formatTextWithLineBreaks(ev.message || ev.error || 'Error')}</div>`;
        markSubRun(view, 'error');
        break;
      case 'cancelled':
        markSubRun(view, 'error');
        break;
      case 'end':
        // Its stream is over. Without an answer before it, it did not finish its work.
        view.openStep = null;
        markSubRun(view, 'unfinished');
        break;
      default:
        renderRunEvent(view, ev);
    }
  }

  // Shared SSE event handler for both EventSource and manual fetch() parsing
  // Module-level so it can be used by both normal requests and a run reattached after a reload
  function handleSSEEvent(data, blk) {
    // A note came while nothing of this run was on the page yet (it was still starting):
    // the block itself goes below the note before anything is drawn into it. Waiting for
    // the next step, as for a run already under way, would leave the run's first status
    // lines above the note -- and a new block below them. It also stays the chat's last
    // row, which is where its request id is put once it is known.
    // Empty by structure, not by innerText: that lays the page out on every event.
    if (pendingAppendRebind && blk.row && !blk.steps.hasChildNodes() && !blk.t.hasChildNodes()) {
      pendingAppendRebind = false;
      chatContainer.appendChild(blk.row);
    }
    switch (data.type) {
      case 'start':
        run.requestId = data.request_id;
        run.sessionId = data.session_id;
        currentSessionId = data.session_id;
        settlePendingTitle(currentSessionId);
        // Stop was clicked before the run had an id to be cancelled by
        if (run.stopWhenStarted) {
          run.stopWhenStarted = false;
          stopRun(run);
        }
        // Steps count from 1 again, so a leftover from the previous run would put this
        // one's first tool lines into the last one's call.
        blk.openStep = null;

        // Notify session manager about new/updated session
        if (window.sessionManager && typeof window.sessionManager.onSessionUpdated === 'function') {
          window.sessionManager.onSessionUpdated(currentSessionId);
        }
        headerTitle(currentSessionId);

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

        // Show reconnect info in response area (taken down at the run's end if nothing took its place)
        showSection(blk.t);
        // Both escaped: last_status is a plugin's status line and carries
        // tool arguments the model chose ("Searching: <query>").
        blk.t.innerHTML = `<div class="response-text reconnect-info">${escapeHtml(data.message)}${data.last_status ? '<br><em>Last status: ' + escapeHtml(data.last_status) + '</em>' : ''}</div>`;
        break;
      case 'reasoning_delta':
      case 'tool_call':
      case 'tool_result':
      case 'tool_error':
        renderRunEvent(blk, data);
        break;
      case 'thinking_delta':
        // A message was appended mid-run: the step AFTER it is the agent's reaction
        // to it -- stream that into a fresh block below the injected message.
        if (data.step !== blk.streamStep && pendingAppendRebind) {
          pendingAppendRebind = false;
          rebindLiveBlock(blk);
        }
        renderRunEvent(blk, data);
        scrollBottom();
        break;
      case 'thinking_complete':
        // A model that does not stream hands over the whole answer here: nothing of it
        // is above a note written while it worked, so it goes below. (Not for an
        // appended message: this answer is not the reply to it.)
        if (pendingAppendRebind === 'note' && data.step !== blk.streamStep && data.assistant && data.assistant.content) {
          pendingAppendRebind = false;
          rebindLiveBlock(blk);
        }
        renderRunEvent(blk, data);
        break;
      case 'thinking':
        // Step marker event (before LLM call): apply a deferred mid-run-append rebind
        // so the step renders below the injected user message. Guard on !content:
        // the pure pre-LLM marker is {type, step}, while the "simplified thinking"
        // event emitted at the END of a text-only step carries `content` --
        // rebinding on that one would strand an empty block when a continuation
        // hook fires right after.
        if (!data.assistant && pendingAppendRebind && !data.content) {
          pendingAppendRebind = false;
          rebindLiveBlock(blk);
        }
        renderRunEvent(blk, data);
        break;
      case 'sub_run':
        // A run started under this one, relayed by the server with the call that
        // started it (see handleSubRunEvent). Only this run's own come on its stream.
        if (blk && blk.box && data.run_id) handleSubRunEvent(blk, data);
        break;
      case 'status':
        // Status events are now delivered through /events stream
        // Show status events for this request AND all hierarchical children (sub-agents)
        // e.g., if the run's request id is "abc123", also show "abc123_sub_001", "abc123_001_sub_002", etc.
        if (blk && blk.steps) {
          const eventRequestId = data.request_id || '';
          // Check if this event belongs to current request hierarchy
          // Either exact match OR starts with current request_id followed by underscore (child operation)
          const matches = eventRequestId === run.requestId ||
              (eventRequestId && run.requestId && eventRequestId.startsWith(run.requestId + '_'));

          if (matches) placeStatus(blk, data);
          // Otherwise silently ignore status from other requests/sessions
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
        if (pendingAppendRebind === 'message') {
          // The run answered without another step, so the message came after the last
          // one had begun: this answer is not the reply to it. The server keeps it in the
          // session, and the next run answers it -- which the viewer has to be told, or the
          // answer below reads as that reply. (Moved below the message, the answer showed
          // twice: it was in the block already.)
          pendingAppendRebind = false;
          addNote(chatContainer, LATE_MESSAGE);
        }
        // Its answer is here: nothing is left to stop -- a cancel would take its session-end hooks and background
        // sub-agents along -- and a reload shows the answer from the session the run saves, following the run no more.
        idleControls();
        forgetRun();
        renderRunEvent(blk, data);
        // A reload mid-run read the run's first steps back into a block of their own
        // and followed the rest in this one: they are the same run, done now.
        chatContainer.querySelectorAll('.msg.assistant > .steps:not([data-settled])').forEach((steps) => {
          steps.dataset.settled = 'true';
          foldSettled(steps);
        });
        break;
      case 'end':
        run.over = true;
        blk.streamEnded = true;   // this block's stream, not the run followed since (placeStatus)
        pendingAppendRebind = false;
        // A reconnect's note nothing took the place of -- a run joined past its answer, which
        // the load shows: it said "running", and would stand under that answer for good.
        if (blk.t.children.length === 1 && blk.t.firstElementChild.classList.contains('reconnect-info')) {
          blk.t.replaceChildren();
          blk.t.closest('.container-section').style.display = 'none';
        }
        // Before the stream goes: whatever is still open stops being updated
        // the moment it closes, so the row has to say so rather than freeze.
        markOpenScopesUnfinished();
        // A sub-agent's run can outlive its caller's (an async one it never waited
        // for): nothing of it reaches this page any more, and its header says so.
        endSubRuns(blk);
        // ... and the session may work on without this run. Watch for that.
        watchForASuccessorRun();
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

  // Public init function that wires the chat form behavior. `agentsLoaded`: the selector's
  // lists, which a session opened after a reload is checked against (read-only or not).
  chatModule.init = function (agentsLoaded) {
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

    loadPreferences();
    // Changed in the Settings panel: the shell passes them on, and they apply at once.
    window.addEventListener('preferences:changed', (event) => usePreferences(event.detail));

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

    stopBtn.addEventListener('click', () => stopRun(run));

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
        messageWritten(currentSessionId);
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
      messageWritten(currentSessionId);
      clearInput(taskInput);
      // The input is consumed -- everything below is the run itself, during
      // which the user must be able to type the next message.
      submitting = false;

      // If there's an active request, append the user message to it (a fetch stream, or
      // a run reattached after a reload: hasActiveRequest covers both) -- its start has named it,
      // or the message would have been held above
      if (chatModule.hasActiveRequest()) {
        const requestId = run.requestId;
        const shown = { load: sessionLoads, session: currentSessionId };  // what the answer may still write into
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
        // Not in a session opened, or loaded again, while the message was on its way: the run
        // the chat follows there is another one, or joined past the message.
        if (shown.load !== sessionLoads || shown.session !== currentSessionId) return;
        if (run.requestId === requestId && run.over) {
          // its answer came while the message was on its way: no step is left to rebind at
          addNote(chatContainer, LATE_MESSAGE);
          return;
        }
        pendingAppendRebind = 'message';
        // back to Stop for the emptied input -- unless the run ended while the append was on its way
        updateActionButton();
        return;
      }

      // No active request: start a new request
      showWorkingElsewhere(false);   // as in attachRun: a run of this page's own takes the chat
      const blk = addAssistantBlock(chatContainer);
      runActive = true; updateActionButton();  // -> Stop (empty input)
      readyStop();
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
        // A person reads this run and can answer what it asks (syncQuestionActions).
        formData.append('attended', 'true');
        
        // Add current session ID if exists (to continue existing session)
        if (currentSessionId) {
          formData.append('session_id', currentSessionId);
        } else {
          const title = titleForFirstMessage();
          if (title) formData.append('session_title', title);  // names the session this message starts
        }

        // NO abort handle on purpose. POST /run runs the agent INLINE in its SSE
        // response (app.py: `async for event in selected_agent.run_events(...)`, no
        // create_job) -- closing this connection kills the run mid-step. Letting go
        // therefore only stops READING it, as it does for a finished run. That is
        // also harmless here: without a job there is no buffer to eat and nothing to
        // reconnect to later.
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

          const sseOk = await readEvents(response, stream, (ev) => {
            handleSSEEvent(ev, blk);
            // the server took the message: its files are sent (a refusal leaves them attached, and
            // files attached since stay)
            if (ev.type === 'start') window.fileUploadModule.removeFiles(files);
          });

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
            firstMessageOver();
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
      else {
        const title = titleForFirstMessage();
        if (title) postBody.session_title = title;  // names the session this message starts
      }
      if (selectedAgent) postBody.agent_name = selectedAgent;
      if (selectedLLMProfile) postBody.llm_profile = selectedLLMProfile;
      // A person reads this run and can answer what it asks (syncQuestionActions).
      postBody.attended = true;

      let lost = false;  // the connection broke before the run's end
      // `stop` ENDS the connection, where letting go of a finished run only stops
      // reading it (see letGoOfRunningRun).
      const runAbort = new AbortController();
      const stream = { stop: () => runAbort.abort() };
      try {
        followedStream = stream;
        const response = await fetch('/events', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(postBody),
          signal: runAbort.signal
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

        await readEvents(response, stream, (ev) => {
          handleSSEEvent(ev, blk);
          // runs as a background job: a reload of this tab follows it again
          if (ev.type === 'start') storeRun(run.requestId, run.sessionId);
        });
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
          firstMessageOver();
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
     *
     * Its session is opened first and the run followed from where that load stands: the
     * run's buffer holds the run alone, and nothing of the turns before it -- the chat came
     * back with the run and none of the conversation. That load's own join
     * (attachRunOfOpenSession) then stays out -- or a run that ended before it had its
     * answer was joined a second time. Opened only once the agent list is in: a sub-agent's
     * session is read-only when its agent is not in it, and read before the list, every
     * one was. A session whose first step is still going is not on disk yet: then the run
     * is all there is, and its buffer replays it.
     */
    async function followRun(agentsLoaded) {
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
          unmarkStopping(stored.requestId);  // over: it takes no message, mark or no mark
          return;
        }
        await agentsLoaded;
        const shown = await window.sessionManager.loadSession(stored.sessionId, { quiet: true });
        if (shown) {
          if (!chatModule.hasActiveRequest()) {
            joinedLoad = sessionLoads;
            attachRun(stored, { catchUp: 'skip', seen: shownSession?.live_events_seen,
              answered: Boolean(shownSession?.live_run_answered),
              answerLoaded: endsInAnswer(shownSession?.messages) });
          }
        } else if (shown === false) {
          attachRun(stored);   // not there to load; null is a session picked meanwhile, which wins
        }
      } finally {
        holding = false;
        updateComposer();
      }
    }

    function attachRun({ requestId, sessionId: session },
                       { catchUp = 'replay', seen = null, answered = false, answerLoaded = false } = {}) {
      // The live run takes the chat -- after a reload, over a session that was not there to
      // load (a read-only one too: the run's session takes messages). A chat that shows that
      // session keeps it.
      if (currentSessionId !== session) {
        chatContainer.innerHTML = '';
        releasePreviewObjectUrls();
        readOnlyShown = false;
      }
      // stored again: a pick or New while the reload checked for the run has let it go
      storeRun(requestId, session);
      // A run takes the chat, so the "working in another process" mark goes with
      // it -- or it stands under a conversation the viewer is watching happen.
      // Here and at the start of a run this page sends, which are the two places
      // a run begins; not in the watch's own tick, which is on a timer, so the
      // mark would linger for seconds and a check on it would measure the timer.
      showWorkingElsewhere(false);
      const blk = addAssistantBlock(chatContainer);
      // The session load shows the run's answer already: its final is not shown a second time.
      blk.answerLoaded = answerLoaded;
      runActive = true; updateActionButton();
      readyStop();

      // The chat continues the run's session and tells the session manager,
      // so no pick or restore takes its place.
      window.sessionManager.setCurrentSession(session);
      currentSessionId = session;
      headerTitle(session);
      run = { requestId, sessionId: session, over: false };
      pendingAppendRebind = false;  // set for the run followed before, not this one
      // Past its answer (the server says so), only finishing: as after its final. Joined from
      // where a load stands, its final is behind what the chat is sent -- nothing would say it
      // has answered, and Stop would cancel its background sub-agents along with it.
      if (answered) {
        run.over = true;
        idleControls();
        forgetRun();
      }
      // Only request_id: the backend uses the job's agent. No token in the URL either:
      // the server does not accept one, and the access_token cookie goes along.
      // catch_up=skip&seen=N: the session was just loaded and already carries the run's
      // live messages -- everything the run's first N events said. Replaying those would
      // show the same turns twice; anything the run sent SINCE still comes, which is how
      // an answer that landed between the load and this connect reaches the screen. A run
      // that started after the page looked (a successor) has been seen by nobody: replayed.
      const es = new EventSource(`/events?task=&request_id=${encodeURIComponent(requestId)}&session_id=${encodeURIComponent(session || '')}`
        + (catchUp === 'skip' ? `&catch_up=skip&seen=${encodeURIComponent(Number(seen) || 0)}` : ''),
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
          // The run was already over when this connection reached it: the server says so
          // in the reconnect, and its stream now carries whatever is left in the buffer
          // and then closes. Without noting it here, that close reads as a connection
          // lost on a run that finished cleanly -- and the session, which HAS the answer
          // on disk by then, is never asked again.
          if (data.type === 'reconnect' && data.status && data.status !== 'running') {
            run.over = true;
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

    /**
     * Follow the run of the session just opened, if one is going.
     *
     * The SERVER is asked, not a note this tab kept. A run started in another tab or
     * through the API is then picked up just the same, and there is no bookkeeping in
     * the browser that could fall out of step with what is actually running.
     *
     * What the viewer missed while away comes from the session load, not from the
     * stream: the load hands out the run's live messages for a session in flight, and
     * a run's event buffer drops its oldest events when it fills. The stream is joined
     * for what happens from here on -- `live_events_seen` is how far "from here on"
     * starts, the number of events the run had sent when that load was taken.
     */
    async function attachRunOfOpenSession(session, join = null) {
      const sessionId = session && session.session_id;
      if (!sessionId || chatModule.hasActiveRequest()) return;
      const load = sessionLoads;
      let active = null;
      try {
        const response = await fetch(`/api/sessions/active?ids=${encodeURIComponent(sessionId)}`,
          { credentials: 'include', signal: AbortSignal.timeout(10000) });
        if (!response.ok) return;
        active = (await response.json()).active?.[sessionId];
      } catch (error) {
        console.warn('[chat_module] Could not ask whether the session has a run:', error);
        return;
      }
      // Another session was opened while the server answered, or a run started here
      // meanwhile: that one is the chat's, not this answer.
      //
      // `requested` as well as the current one: a pick sets it BEFORE its load, while
      // the current session only changes once that load answers. Asked about the
      // current one alone, an answer arriving between the two would attach here and
      // take the header -- which bumps the session pane's loading count and drops the
      // load already on its way. The click would vanish without a word.
      if (window.sessionManager.getCurrentSessionId() !== sessionId) return;
      if (window.sessionManager.requested !== sessionId) return;
      if (currentSessionId !== sessionId || chatModule.hasActiveRequest()) return;
      // The same session loaded again meanwhile: its answer, with the later count of
      // events already on screen, is the one to join by. This one would replay the
      // events in between a second time.
      if (load !== sessionLoads) return;
      // The reload that made this load has joined its run itself (followRun): ended since,
      // the run would be joined twice.
      if (!join && load === joinedLoad) return;
      // Held by another process (a woken run): nothing to attach to, only to wait for.
      // A session just opened is watched as one whose run has ended is, or the turn
      // shows only on the next load; the watch itself asks with a `join`.
      if (active?.elsewhere) {
        if (join === null) watchForASuccessorRun();
        return;
      }
      // `attachable` is false for a run with no background job (a /run with files, a
      // sub-agent's run): GET /events answers 409 for those, which the chat would show
      // as a lost connection on a session that is working perfectly well.
      if (!active || !active.request_id || !active.attachable) return;
      // The run the chat has just followed to its answer, still saving (a mirrored /run's
      // job runs until its caller's save is done): the watch for its successor would
      // replay it whole. (A session opened meanwhile joins it from where its load stands.)
      if (join && active.request_id === run.requestId && run.over) return;
      // `join` is how the caller says what it has already seen of that run. A
      // session just loaded carries the run's live messages, so it skips; a run
      // that started AFTER this page was watching has been seen by nobody, and
      // replaying it is the only way its turn reaches the screen at all.
      //
      // Whether the load shows the run's answer is the load's to say: the run's live
      // conversation takes its answer only after the post-LLM hooks, the stream sends it
      // before them -- so a load in between counts the answer as seen without showing it.
      attachRun({ requestId: active.request_id, sessionId },
        { ...(join || { catchUp: 'skip', seen: session.live_events_seen }), answered: Boolean(active.answered),
          answerLoaded: !join && endsInAnswer(session.messages) });
    }

    chatModule.followRunOfOpenSession = attachRunOfOpenSession;

    return followRun(agentsLoaded);
  };
  
  // Where a failure puts its reason, the answer's own place: shown AND open. The setting
  // that folds a sub-agent's answer away (chat.sub_agent_output) is about answers; a
  // reason nobody sees is the same as none, and the header says only that it went wrong.
  function showReason(element) {
    showSection(element);
    const section = element.closest('.container-section');
    if (!section || !section.querySelector(':scope > [data-toggle="response"]')) return;
    element.style.display = 'block';
    markOpen(section, true);
    // Not the setting's business from here on: it folds answers, and this box holds a
    // reason -- a later change of the setting would take it back out of sight.
    section.dataset.reason = 'true';
  }

  function showSection(element) {
    const section = element.closest('.container-section');
    if (section && section.style.display === 'none') {
      section.style.display = 'block';
    }
  }

  // The command grammar is shared with the terminal (chat_commands.py), and
  // this surface renders what that catalogue advertises -- so what the browser
  // DOES with an argument has to be measurable from outside. Without this the
  // only way in is a submit event, which needs the whole page.
  chatModule.runCommand = runChatCommand;
  
  // attach to global
  global.chatModule = chatModule;

  // No cleanup on beforeunload: the browser ends the streams of a page that unloads, and a page that stays -- a
  // link that turns into a download -- follows its run on.

  // Listen for new conversation events
  window.addEventListener('session:new', (event) => {
    // Starting a new session leaves a run going, like switching to another one does.
    letGoOfRunningRun();
    letGoOfFinishedRun();
    currentSessionId = null;
    const chatEl = document.getElementById('chat');
    chatEl.innerHTML = '';
    forgetStoredSubRuns();
    releasePreviewObjectUrls();
    // not chosen, but all that is left when the stored session cannot be shown: a lost run of it stays
    if (event.detail.chosen) leaveLostRun(null);
    readOnlyShown = false;
    updateComposer();
  });

  // Listen for session load events
  window.addEventListener('session:loaded', (event) => {
    const { session, readOnly, reason } = event.detail;

    // The run of the session being LEFT goes on -- the chat just stops watching it.
    // This used to refuse the load outright ("a run is still streaming"), which is
    // why no session could be opened while any run was going: the one thing a run
    // must not do is pin the viewer to the session it runs in.
    letGoOfRunningRun();
    letGoOfFinishedRun();
    leaveLostRun(session.session_id);

    if (session && session.messages) {
      // Clear current chat
      const chatEl = document.getElementById('chat');
      if (chatEl) {
        chatEl.innerHTML = '';
        forgetRows();
        releasePreviewObjectUrls();
      }
      
      // Restore messages. What a tool answered, by the call it answered (answersTo): the
      // session has carried this all along -- measured over 58 sessions: tool_calls on
      // 29 % of the messages, tool_call_id on 37 %, reasoning on 32 %. None of it was ever
      // shown again, so switching sessions looked like the run had been erased.
      // One assistant block per RUN, with a step per LLM call inside it -- the
      // shape the live view builds. A message that opens a turn ends the run
      // before it, so the next assistant message starts a new block.
      let runBlk = null;
      let stepNo = 0;
      // A run followed by a new turn is over, and its steps fold as a live run's do at
      // its answer. The LAST run may still be working -- a session read back while its
      // run goes on (live_events_seen) -- and stays open unless it has answered.
      let answered = false;
      // A run can span several blocks: after a continuation it goes on in a fresh
      // one below the injected message, as the live view rebinds it (rebindLiveBlock).
      let runBlocks = [];
      const endRun = () => { runBlocks.forEach(settleView); runBlocks = []; runBlk = null; stepNo = 0; };
      const replayed = [];   // every run block, for the sub-runs their calls started
      forgetStoredSubRuns();

      session.messages.forEach((msg, index) => {
        // Skip system messages and tool-related messages
        if (msg.role === 'system' || msg.role === 'tool') {
          return;
        }

        if (msg.role === 'user' && msg.injected_by) {
          // Put there by a hook, not typed: a continuation (agent_continuation's follow-ups,
          // a missing required spawn) or what a hook hands the run (debate_forum's posts).
          // No turn anybody took, so the run goes on -- shown in the shape of the live
          // 'continuation' event, the run's next steps in a fresh block below. That event's
          // count and reason are not stored, and not every marker is a continuation: the
          // badge names what is known, the marker who sent it.
          const contRow = document.createElement('div');
          contRow.className = 'row';
          const contMsg = document.createElement('div');
          contMsg.className = 'msg user continuation-msg';
          contMsg.innerHTML = `<div class="continuation-badge">${kitIcon('info')} Injected</div><div class="continuation-reason">${escapeHtml(msg.injected_by)}</div><div class="continuation-text">${formatTextWithLineBreaks(messageText(msg))}</div>`;
          contRow.appendChild(contMsg);
          chatEl.appendChild(contRow);
          runBlk = null;
          answered = false;   // what the run said before it was an interim, not its answer
        } else if (msg.role === 'user') {
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
          endRun();  // what follows belongs to a new run, in a block of its own
        } else if (msg.role === 'assistant') {
          if (!runBlk) {
            runBlk = addAssistantBlock(chatEl);
            replayed.push(runBlk);
            runBlocks.push(runBlk);
          }
          stepNo = msg.step || stepNo + 1;   // as the server numbered it (ChatMessage.step)
          replayStep(runBlk, msg, stepNo, answersTo(session.messages, index));
          answered = !!msg.content && !(msg.tool_calls && msg.tool_calls.length);
          if (msg.content) showAnswer(runBlk, msg.content, msg.content_format);
        } else if (msg.role === 'developer') {
          // What the run told the model, at the point it told it. Without this
          // branch the note fell through every else-if and the restored chat
          // showed an agent acting on something nobody could see.
          const row = document.createElement('div');
          row.className = 'row';
          const note = document.createElement('div');
          note.className = 'msg note';
          const pre = document.createElement('pre');
          pre.className = 'note-text';  // the shape every other note in this chat has
          pre.textContent = `[note] ${messageText(msg)}`;
          note.appendChild(pre);
          row.appendChild(note);
          chatEl.appendChild(row);
          // A wake opens a turn just as a typed line does (message_roles.
          // opens_a_turn), so what follows is a run of its own.
          if (!msg.injected_by) endRun();
        }
      });
      if (answered) runBlocks.forEach(settleView);
      // Not awaited: the session is shown either way, its sub-runs join it when listed --
      // and the live events of runs under the followed one wait until then (waitingFor).
      const gate = { waiting: [] };
      sessionGate = gate;
      attachStoredSubRuns(session, replayed).finally(() => {
        if (sessionGate === gate) release(gate);   // a session shown since has a gate of its own
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
    // The session shown may have an agent working in it. Not awaited: the session is
    // rendered either way, and the run joins the view when the server has answered.
    sessionLoads++;
    shownSession = session;
    chatModule.followRunOfOpenSession?.(session);
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
    unmarkStopping(requestId);  // cancelled: it is ending and takes no message
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

  // A title waiting for the first message goes when the chat goes to another
  // session first -- said, as agent-cli says it. Registered after the chat's
  // own session:new / session:loaded handlers, which clear the conversation:
  // the note belongs to the one the chat goes to. A session:new nobody chose
  // (the stored session could not be shown) leaves the chat the new one it was:
  // a title typed there waits on -- unless a first message was on its way, which
  // is let go of all the same, and the title with it.
  function dropPendingTitle(event) {
    const unchosen = Boolean(event && event.type === 'session:new' && event.detail && event.detail.chosen === false);
    const keep = unchosen && !firstMessageOut;
    const chat = document.getElementById('chat');
    if (pendingTitle && !keep) {
      addNote(chat, '(the title \'' + pendingTitle + '\' was not written -- ' + (firstMessageOut
        ? 'the chat left before the session your message is starting was there)'
        : 'no message went out)'));
    } else if (firstMessageOut && firstMessageOut.title) {
      // before the run's start: the server may still refuse the message
      addNote(chat, '(the title \'' + firstMessageOut.title
        + '\' went out with your message: a session the server starts for it is named so)');
    }
    if (!keep) pendingTitle = null;
    firstMessageOut = null;
  }

  // A first message goes out: the waiting title with it (session_title).
  function titleForFirstMessage() {
    firstMessageOut = { title: pendingTitle };
    pendingTitle = null;
    return firstMessageOut.title;
  }

  // The chat is done with its first message's stream: a run that never started
  // (refused, unreachable) leaves its title waiting for the next message.
  function firstMessageOver() {
    if (!firstMessageOut) return;
    pendingTitle = pendingTitle || firstMessageOut.title;
    firstMessageOut = null;
  }

  // The start of the run a first message began: the title that went out with
  // it is the run's now; one typed since goes to the session by name (the
  // server keeps it for the run's first save -- there is no record yet).
  function settlePendingTitle(sessionId) {
    if (!firstMessageOut) return;  // not the start of this chat's first message
    const typedSince = pendingTitle;
    const sent = firstMessageOut.title;
    pendingTitle = null;
    firstMessageOut = null;
    if (sent) nameUnlisted(sessionId, sent);
    if (!typedSince || !window.sessionManager) return;
    // named only once it is written: a refused rename leaves the one that went out
    const shown = unlistedTitles[sessionId];
    window.sessionManager.renameTo(sessionId, typedSince).then((done) => {
      const now = listed(sessionId) ? null : unlistedTitles[sessionId];  // none: nothing named, or the list has the session since
      if (now && now !== shown) return;  // named again since: that one stands
      if (done) {
        nameUnlisted(sessionId, typedSince);
      } else if (currentSessionId === sessionId) {
        // the title it keeps is the list's once the list has it -- maybe renamed since
        addNote(document.getElementById('chat'), '(the title \'' + typedSince + '\' was not written'
          + (now ? ' -- the session keeps \'' + now.title + '\')' : ')'));
      }
    });
  }
  window.addEventListener('session:new', dropPendingTitle);
  window.addEventListener('session:loaded', dropPendingTitle);

})(window);

