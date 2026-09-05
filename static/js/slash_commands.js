/**
 * Slash commands and skills for the web chat.
 *
 * Deliberately thin: the catalogue, the parsing and the skill expansion all
 * come from the server (`/chat/commands`, `/chat/resolve`), which is the same
 * `agent_system.chat_commands` module the terminal chat uses. A second parser
 * living in the browser is a second parser that drifts -- and the browser
 * cannot read the skill folders anyway.
 */
(function (global) {
  'use strict';

  const slash = {};

  let catalogue = null;      // {commands: [...], skills: [...], plugin_commands: [...]}
  let cataloguePromise = null;
  let catalogueAgent = null; // the agent the cached catalogue was fetched for
  let box = null;            // the suggestion dropdown
  let entries = [];          // what is currently offered
  let active = -1;           // highlighted entry
  let input = null;

  const EMPTY = { commands: [], skills: [], plugin_commands: [] };

  /** Which agent the chat is talking to, '' while none is chosen. */
  function currentAgent() {
    const selector = global.selectorModule;
    return (selector && typeof selector.getCurrentAgent === 'function'
      ? selector.getCurrentAgent() : '') || '';
  }

  function normalise(data) {
    const merged = Object.assign({}, EMPTY, data || {});
    merged.plugin_commands = merged.plugin_commands || [];
    return merged;
  }

  /**
   * Load the catalogue for the CURRENT agent. A failure is not fatal --
   * typing still works.
   *
   * Keyed by agent, because plugin commands are: the server lists only what
   * that agent's allowlist lets it dispatch. Switching the selector therefore
   * has to fetch again, or the browser offers a /compact the new agent cannot
   * run (and hides one it can).
   */
  slash.load = function () {
    const agent = currentAgent();
    if (catalogue && catalogueAgent === agent) return Promise.resolve(catalogue);
    if (cataloguePromise && catalogueAgent === agent) return cataloguePromise;

    catalogueAgent = agent;
    const url = '/chat/commands?surface=web'
      + (agent ? '&agent=' + encodeURIComponent(agent) : '');
    cataloguePromise = fetch(url)
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null)
      .then((data) => {
        // Two answers that must not be cached. A LATE one: switching A -> B -> A
        // leaves B's reply in flight, and storing it would offer B's commands
        // under A. And a FAILED one: keeping the empty list would hide every
        // command until the page is reloaded, so the next keystroke retries.
        if (catalogueAgent !== agent) return catalogue || normalise(null);
        if (!data) {
          // Forget everything, including WHICH agent was asked: keeping the
          // previous agent's list under this one's key is the very mix-up the
          // key exists to prevent. The next keystroke asks again.
          catalogue = null;
          catalogueAgent = null;
          cataloguePromise = null;
          slash.catalogue = normalise(null);
          return slash.catalogue;
        }
        catalogue = normalise(data);
        slash.catalogue = catalogue;
        return catalogue;
      });
    return cataloguePromise;
  };

  /** Ask the server what a typed line is. Null when the call fails. */
  slash.resolve = function (line) {
    return fetch('/chat/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // The agent decides which plugin commands exist, so the parser needs it
      // as well -- without it "/compact" comes back as an unknown command.
      body: JSON.stringify({ line: line, agent_name: currentAgent() || null }),
    })
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);
  };

  /** Help text, rendered from the same catalogue the CLI prints. */
  slash.helpLines = function () {
    const data = catalogue || EMPTY;
    const lines = ['Commands:'];
    const width = data.commands.reduce((w, c) => Math.max(w, c.display.length), 0);
    data.commands.forEach((c) => {
      lines.push('  ' + c.display.padEnd(width) + '   ' + c.summary);
    });
    if (data.plugin_commands.length) {
      lines.push('', 'Plugin commands (they run a tool, no LLM turn):');
      const pluginWidth = data.plugin_commands.reduce(
        (w, c) => Math.max(w, c.display.length), 0);
      data.plugin_commands.forEach((c) => {
        lines.push('  ' + c.display.padEnd(pluginWidth) + '   ' + (c.summary || ''));
      });
    }
    if (data.skills.length) {
      lines.push('', 'Skills (run one directly, arguments are passed to it):');
      data.skills.forEach((s) => {
        lines.push('  /' + s.name + (s.summary ? '   ' + s.summary : ''));
      });
    }
    return lines;
  };

  // ------------------------------------------------------------ suggestions

  function candidates(word) {
    const data = catalogue || EMPTY;
    const needle = word.toLowerCase();
    const out = [];
    data.commands.forEach((c) => {
      // Match any spelling, but always offer the canonical one.
      if (c.aliases.some((a) => a.toLowerCase().indexOf(needle) === 0)) {
        out.push({ insert: c.aliases[0], label: c.display, hint: c.summary, kind: 'command' });
      }
    });
    // Between the built-ins and the skills, which is the order they win in:
    // a plugin cannot shadow /help, and a skill folder cannot shadow a plugin.
    data.plugin_commands.forEach((c) => {
      if (('/' + c.spelling).toLowerCase().indexOf(needle) === 0) {
        out.push({ insert: '/' + c.spelling, label: c.display,
                   hint: c.summary || '', kind: 'plugin' });
      }
    });
    data.skills.forEach((s) => {
      if (('/' + s.name).toLowerCase().indexOf(needle) === 0) {
        out.push({ insert: '/' + s.name, label: '/' + s.name, hint: s.summary || '', kind: 'skill' });
      }
    });
    return out;
  }

  function ensureBox() {
    if (box) return box;
    box = document.createElement('div');
    box.className = 'slash-suggestions';
    box.setAttribute('role', 'listbox');
    box.style.display = 'none';
    const container = input.closest('.input-container') || input.parentNode;
    container.style.position = container.style.position || 'relative';
    container.appendChild(box);
    return box;
  }

  function render() {
    const el = ensureBox();
    if (!entries.length) {
      el.style.display = 'none';
      return;
    }
    el.innerHTML = '';
    entries.forEach((entry, index) => {
      const row = document.createElement('div');
      row.className = 'slash-suggestion' + (index === active ? ' is-active' : '');
      row.setAttribute('role', 'option');
      const name = document.createElement('span');
      name.className = 'slash-name';
      name.textContent = entry.label;
      row.appendChild(name);
      if (entry.hint) {
        const hint = document.createElement('span');
        hint.className = 'slash-hint';
        hint.textContent = entry.hint;
        row.appendChild(hint);
      }
      // mousedown, not click: the textarea must not lose focus first.
      row.addEventListener('mousedown', function (e) {
        e.preventDefault();
        accept(index);
      });
      el.appendChild(row);
    });
    el.style.display = 'block';
  }

  function close() {
    entries = [];
    active = -1;
    if (box) box.style.display = 'none';
  }

  function accept(index) {
    const entry = entries[index];
    if (!entry) return;
    const rest = input.value.trim().split(' ').slice(1).join(' ');
    input.value = entry.insert + (rest ? ' ' + rest : ' ');
    close();
    input.focus();
    try {
      input.dispatchEvent(new Event('input', { bubbles: true }));
    } catch (e) { /* older browsers */ }
  }

  /** True while the dropdown is open — the caller must not submit then. */
  slash.isOpen = function () {
    return entries.length > 0;
  };

  slash.attach = function (taskInput) {
    if (!taskInput) return;
    input = taskInput;
    slash.load();

    // Plugin commands are per agent, so the catalogue has to follow the
    // selector. The change event covers a person picking one; load() on every
    // input covers setAgent(), which the session restore calls WITHOUT firing
    // change -- it returns the cached promise unless the agent really moved.
    const agentSelector = document.getElementById('agentSelector');
    if (agentSelector) {
      agentSelector.addEventListener('change', function () { slash.load(); });
    }

    input.addEventListener('input', function () {
      const value = input.value;
      slash.load();
      // Only the FIRST word, and only when it is the whole line so far:
      // "/etc/hosts lesen" is a message, not a half-typed command.
      const firstSpace = value.indexOf(' ');
      const word = firstSpace === -1 ? value : value.slice(0, firstSpace);
      if (!value.startsWith('/') || value.startsWith('//') || firstSpace !== -1) {
        close();
        return;
      }
      entries = candidates(word);
      active = entries.length ? 0 : -1;
      render();
    });

    input.addEventListener('keydown', function (e) {
      if (!slash.isOpen()) return;
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        active = (active + 1) % entries.length;
        render();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        active = (active - 1 + entries.length) % entries.length;
        render();
      } else if (e.key === 'Tab' || (e.key === 'Enter' && !e.ctrlKey && !e.metaKey)) {
        // Plain Enter completes the highlighted entry. Ctrl/Cmd+Enter is this
        // UI's send gesture and stays untouched -- stealing it would leave the
        // user pressing it twice.
        e.preventDefault();
        e.stopPropagation();
        accept(active);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        close();
      }
    }, true);

    input.addEventListener('blur', function () {
      window.setTimeout(close, 120);
    });
  };

  slash.close = close;
  global.slashCommands = slash;
})(window);
