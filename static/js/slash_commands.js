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

  let catalogue = null;      // {commands: [...], skills: [...]}
  let cataloguePromise = null;
  let box = null;            // the suggestion dropdown
  let entries = [];          // what is currently offered
  let active = -1;           // highlighted entry
  let input = null;

  /** Load the catalogue once. A failure is not fatal -- typing still works. */
  slash.load = function () {
    if (catalogue) return Promise.resolve(catalogue);
    if (!cataloguePromise) {
      cataloguePromise = fetch('/chat/commands?surface=web')
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          catalogue = data || { commands: [], skills: [] };
          slash.catalogue = catalogue;
          return catalogue;
        })
        .catch(() => {
          catalogue = { commands: [], skills: [] };
          slash.catalogue = catalogue;
          return catalogue;
        });
    }
    return cataloguePromise;
  };

  /** Ask the server what a typed line is. Null when the call fails. */
  slash.resolve = function (line) {
    return fetch('/chat/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ line: line }),
    })
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);
  };

  /** Help text, rendered from the same catalogue the CLI prints. */
  slash.helpLines = function () {
    const data = catalogue || { commands: [], skills: [] };
    const lines = ['Commands:'];
    const width = data.commands.reduce((w, c) => Math.max(w, c.display.length), 0);
    data.commands.forEach((c) => {
      lines.push('  ' + c.display.padEnd(width) + '   ' + c.summary);
    });
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
    const data = catalogue || { commands: [], skills: [] };
    const needle = word.toLowerCase();
    const out = [];
    data.commands.forEach((c) => {
      // Match any spelling, but always offer the canonical one.
      if (c.aliases.some((a) => a.toLowerCase().indexOf(needle) === 0)) {
        out.push({ insert: c.aliases[0], label: c.display, hint: c.summary, kind: 'command' });
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

    input.addEventListener('input', function () {
      const value = input.value;
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
